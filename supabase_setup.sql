-- =====================================================================
-- Pet Art Filter - database setup for Supabase
-- Paste this whole file into Supabase -> SQL Editor -> New query -> Run.
-- Safe to run again (it only creates what's missing / replaces functions).
-- Then also run supabase_sellers.sql, supabase_hardening.sql and supabase_pictures.sql (in that order).
-- Note: Supabase's Security Advisor will list "market" as a "Security Definer View".
-- That is intended (players read listings only through it). Do NOT change it.
-- =====================================================================
create extension if not exists pgcrypto with schema extensions;

-- ---------------------------------------------------------------- market listings
create table if not exists public.listings (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  seller      text not null check (char_length(seller) between 2 and 24),
  contact     text not null default '' check (char_length(contact) <= 80),
  item        text not null check (char_length(item) between 1 and 40),
  bonuses     jsonb not null check (jsonb_typeof(bonuses) = 'array'
                                    and jsonb_array_length(bonuses) between 1 and 7),
  price_kk    numeric(12,1) not null check (price_kk > 0 and price_kk <= 10000000),
  status      text not null default 'active' check (status in ('active', 'sold')),
  owner_hash  text not null,
  app_version text not null default ''
);
create index if not exists listings_active_idx on public.listings (status, created_at desc);
alter table public.listings enable row level security;          -- no direct access at all
revoke all on public.listings from anon, authenticated;

-- what players can read: active listings of the last 30 days, without the owner hash
create or replace view public.market as
  select id, created_at, seller, contact, item, bonuses, price_kk
  from public.listings
  where status = 'active' and created_at > now() - interval '30 days';
revoke all on public.market from anon, authenticated;   -- Supabase grants ALL by default!
grant select on public.market to anon, authenticated;

create index if not exists listings_owner_idx   on public.listings (owner_hash, created_at desc);
create index if not exists listings_created_idx on public.listings (created_at desc);

create or replace function public.add_listing(p_secret text, p_seller text, p_contact text, p_item text,
                                              p_bonuses jsonb, p_price numeric, p_version text)
returns uuid language plpgsql security definer set search_path = public, extensions, pg_temp as $$
declare h text; n int; new_id uuid; e jsonb;
begin
  if p_secret is null or char_length(p_secret) not between 24 and 128 then raise exception 'invalid key'; end if;
  if p_seller is null or p_item is null or p_price is null then raise exception 'missing fields'; end if;
  if concat(p_seller, p_contact, p_item, p_version) ~ '[[:cntrl:]]' then raise exception 'invalid characters'; end if;
  if p_bonuses is null or jsonb_typeof(p_bonuses) <> 'array' then raise exception 'invalid bonuses'; end if;
  if octet_length(p_bonuses::text) > 1000 then raise exception 'bonuses too large'; end if;
  for e in select * from jsonb_array_elements(p_bonuses) loop
    if jsonb_typeof(e) <> 'array' then raise exception 'invalid bonuses'; end if;
    if jsonb_array_length(e) <> 2 or jsonb_typeof(e->0) <> 'string' or char_length(e->>0) > 40
       or jsonb_typeof(e->1) not in ('number', 'string') or char_length(e->>1) > 12 then
      raise exception 'invalid bonuses';
    end if;
  end loop;

  perform pg_advisory_xact_lock(727001);          -- one insert at a time: no count/insert race
  h := encode(digest(p_secret, 'sha256'), 'hex');

  delete from listings where created_at < now() - interval '60 days'
                          or (status = 'sold' and created_at < now() - interval '7 days');

  select count(*) into n from listings where owner_hash = h and status = 'active'
                                         and created_at > now() - interval '30 days';
  if n >= 60 then raise exception 'You already have 60 active listings.'; end if;
  select count(*) into n from listings where owner_hash = h and created_at > now() - interval '1 minute';
  if n >= 10 then raise exception 'Slow down a bit - too many new listings in one minute.'; end if;
  select count(*) into n from listings where created_at > now() - interval '1 minute';
  if n >= 30 then raise exception 'The market is busy - try again in a minute.'; end if;
  select count(*) into n from listings where status = 'active' and created_at > now() - interval '30 days';
  if n >= 5000 then raise exception 'The market is full right now.'; end if;

  insert into listings (seller, contact, item, bonuses, price_kk, owner_hash, app_version)
  values (trim(p_seller), coalesce(trim(p_contact), ''), trim(p_item), p_bonuses, p_price, h,
          left(coalesce(p_version, ''), 20))
  returning id into new_id;
  return new_id;
end $$;

create or replace function public.remove_listing(p_secret text, p_id uuid)
returns boolean language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  delete from listings where id = p_id and owner_hash = encode(digest(p_secret, 'sha256'), 'hex');
  return found;
end $$;

create or replace function public.mark_sold(p_secret text, p_id uuid)
returns boolean language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  update listings set status = 'sold'
  where id = p_id and owner_hash = encode(digest(p_secret, 'sha256'), 'hex');
  return found;
end $$;

create or replace function public.my_listings(p_secret text)
returns table (id uuid, created_at timestamptz, seller text, contact text, item text,
               bonuses jsonb, price_kk numeric, status text)
language sql security definer set search_path = public, extensions, pg_temp as $$
  select id, created_at, seller, contact, item, bonuses, price_kk, status
  from listings where owner_hash = encode(digest(p_secret, 'sha256'), 'hex')
  order by created_at desc limit 200;
$$;

-- ---------------------------------------------------------------- official presets
create table if not exists public.official_presets (
  id         uuid primary key default gen_random_uuid(),
  updated_at timestamptz not null default now(),
  name       text not null check (char_length(name) between 1 and 40),
  color      text not null default 'rainbow' check (char_length(color) <= 16),
  data       jsonb not null,
  sort       int not null default 0
);
alter table public.official_presets enable row level security;
drop policy if exists "read official presets" on public.official_presets;
create policy "read official presets" on public.official_presets for select to anon, authenticated using (true);
revoke all on public.official_presets from anon, authenticated;
grant select on public.official_presets to anon, authenticated;

-- admin keys: only the sha256 hash is stored; the key itself stays on the admin's PC
create table if not exists public.admin_keys (key_hash text primary key, note text);
alter table public.admin_keys enable row level security;
revoke all on public.admin_keys from anon, authenticated;
insert into public.admin_keys (key_hash, note)
values ('c9dc3d34a2cdc95e7de5bfddbbf58523c4dc7fd788bbfbd05f0d332d64c56229', 'app owner')
on conflict do nothing;

create or replace function public.is_admin(p_secret text)
returns boolean language sql security definer set search_path = public, extensions, pg_temp as $$
  select exists (select 1 from admin_keys where key_hash = encode(digest(coalesce(p_secret, ''), 'sha256'), 'hex'));
$$;

create or replace function public.admin_publish_preset(p_secret text, p_id uuid, p_name text, p_color text,
                                                       p_data jsonb, p_sort int)
returns uuid language plpgsql security definer set search_path = public, extensions, pg_temp as $$
declare new_id uuid;
begin
  if not is_admin(p_secret) then raise exception 'not allowed'; end if;
  if p_id is null then
    insert into official_presets (name, color, data, sort) values (p_name, p_color, p_data, coalesce(p_sort, 0))
    returning id into new_id;
    return new_id;
  end if;
  update official_presets set name = p_name, color = p_color, data = p_data, sort = coalesce(p_sort, sort),
         updated_at = now() where id = p_id;
  if not found then raise exception 'preset not found'; end if;
  return p_id;
end $$;

create or replace function public.admin_delete_preset(p_secret text, p_id uuid)
returns boolean language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  if not is_admin(p_secret) then raise exception 'not allowed'; end if;
  delete from official_presets where id = p_id;
  return found;
end $$;

grant execute on function public.add_listing(text, text, text, text, jsonb, numeric, text) to anon, authenticated;
grant execute on function public.remove_listing(text, uuid) to anon, authenticated;
grant execute on function public.mark_sold(text, uuid) to anon, authenticated;
grant execute on function public.my_listings(text) to anon, authenticated;
grant execute on function public.is_admin(text) to anon, authenticated;
grant execute on function public.admin_publish_preset(text, uuid, text, text, jsonb, int) to anon, authenticated;
grant execute on function public.admin_delete_preset(text, uuid) to anon, authenticated;
