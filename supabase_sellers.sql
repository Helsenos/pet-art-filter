-- =====================================================================
-- Pet Art Filter - invite-only selling (run AFTER supabase_setup.sql)
-- Paste this whole file into Supabase -> SQL Editor -> New query -> Run.
-- Safe to run again.
--
-- Only sellers the admin created can list pet arts. Each seller has a
-- private seller code (only its hash is stored here). Listings belong to
-- the seller, not to a PC: a new code keeps the listings, a disabled
-- seller's listings disappear from the market at once.
-- =====================================================================

create table if not exists public.sellers (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  name        text not null check (char_length(name) between 2 and 24),
  code_hash   text not null unique,
  active      boolean not null default true
);
create unique index if not exists sellers_name_idx on public.sellers (lower(name));
alter table public.sellers enable row level security;           -- no direct access at all
revoke all on public.sellers from anon, authenticated;

alter table public.listings add column if not exists seller_id uuid references public.sellers(id) on delete cascade;
alter table public.listings alter column owner_hash drop not null;
create index if not exists listings_seller_idx on public.listings (seller_id, created_at desc);

-- listings from before invites existed have no seller: remove them
delete from public.listings where seller_id is null;

-- what players can read: active listings of active sellers, last 30 days
create or replace view public.market as
  select l.id, l.created_at, s.name as seller, l.contact, l.item, l.bonuses, l.price_kk
  from public.listings l join public.sellers s on s.id = l.seller_id
  where l.status = 'active' and s.active and l.created_at > now() - interval '30 days';
revoke all on public.market from anon, authenticated;
grant select on public.market to anon, authenticated;

-- seller codes are compared without dashes/spaces and case-insensitively
create or replace function public.seller_code_hash(p_code text)
returns text language sql immutable set search_path = public, extensions, pg_temp as $$
  select encode(digest(upper(regexp_replace(coalesce(p_code, ''), '[^A-Za-z0-9]', '', 'g')), 'sha256'), 'hex');
$$;
revoke all on function public.seller_code_hash(text) from public, anon, authenticated;

create or replace function public.seller_id_for(p_code text, p_need_active boolean)
returns uuid language sql stable security definer set search_path = public, extensions, pg_temp as $$
  select id from sellers
  where code_hash = seller_code_hash(p_code) and (active or not p_need_active)
    and char_length(regexp_replace(coalesce(p_code, ''), '[^A-Za-z0-9]', '', 'g')) >= 12;
$$;
revoke all on function public.seller_id_for(text, boolean) from public, anon, authenticated;

-- ---------------------------------------------------------------- for sellers
-- the old open version (anyone could post) is removed
drop function if exists public.add_listing(text, text, text, text, jsonb, numeric, text);

create or replace function public.add_listing(p_code text, p_contact text, p_item text,
                                              p_bonuses jsonb, p_price numeric, p_version text)
returns uuid language plpgsql security definer set search_path = public, extensions, pg_temp as $$
declare sid uuid; sname text; n int; new_id uuid; e jsonb;
begin
  sid := seller_id_for(p_code, true);
  if sid is null then raise exception 'Your seller code isn''t valid (or was disabled). Ask the app owner for one.'; end if;
  select name into sname from sellers where id = sid;
  if p_item is null or p_price is null then raise exception 'missing fields'; end if;
  if p_price <= 0 or p_price > 10000000 then raise exception 'invalid price'; end if;
  if char_length(coalesce(p_contact, '')) > 80 or char_length(p_item) not between 1 and 40 then
    raise exception 'text too long';
  end if;
  if concat(p_contact, p_item, p_version) ~ '[[:cntrl:]]' then raise exception 'invalid characters'; end if;
  if p_bonuses is null or jsonb_typeof(p_bonuses) <> 'array' then raise exception 'invalid bonuses'; end if;
  if octet_length(p_bonuses::text) > 1000 then raise exception 'bonuses too large'; end if;
  for e in select * from jsonb_array_elements(p_bonuses) loop
    if jsonb_typeof(e) <> 'array' then raise exception 'invalid bonuses'; end if;
    if jsonb_array_length(e) <> 2 or jsonb_typeof(e->0) <> 'string' or char_length(e->>0) > 40
       or jsonb_typeof(e->1) not in ('number', 'string') or char_length(e->>1) > 12 then
      raise exception 'invalid bonuses';
    end if;
  end loop;

  perform pg_advisory_xact_lock(727001);
  delete from listings where created_at < now() - interval '60 days'
                          or (status = 'sold' and created_at < now() - interval '7 days');
  select count(*) into n from listings where seller_id = sid and status = 'active'
                                         and created_at > now() - interval '30 days';
  if n >= 60 then raise exception 'You already have 60 active listings.'; end if;
  select count(*) into n from listings where seller_id = sid and created_at > now() - interval '1 minute';
  if n >= 10 then raise exception 'Slow down a bit - too many new listings in one minute.'; end if;
  select count(*) into n from listings where status = 'active' and created_at > now() - interval '30 days';
  if n >= 5000 then raise exception 'The market is full right now.'; end if;

  insert into listings (seller, seller_id, contact, item, bonuses, price_kk, owner_hash, app_version)
  values (sname, sid, coalesce(trim(p_contact), ''), trim(p_item), p_bonuses, p_price, null,
          left(coalesce(p_version, ''), 20))
  returning id into new_id;
  return new_id;
end $$;

-- which seller a code belongs to (the app checks a code when it's entered)
create or replace function public.seller_check(p_code text)
returns table (name text, active boolean)
language sql security definer set search_path = public, extensions, pg_temp as $$
  select name, active from sellers where id = seller_id_for(p_code, false);
$$;

create or replace function public.remove_listing(p_secret text, p_id uuid)
returns boolean language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  delete from listings where id = p_id and seller_id = seller_id_for(p_secret, false);
  return found;
end $$;

create or replace function public.mark_sold(p_secret text, p_id uuid)
returns boolean language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  update listings set status = 'sold' where id = p_id and seller_id = seller_id_for(p_secret, true);
  return found;
end $$;

create or replace function public.my_listings(p_secret text)
returns table (id uuid, created_at timestamptz, seller text, contact text, item text,
               bonuses jsonb, price_kk numeric, status text)
language sql security definer set search_path = public, extensions, pg_temp as $$
  select l.id, l.created_at, s.name, l.contact, l.item, l.bonuses, l.price_kk, l.status
  from listings l join sellers s on s.id = l.seller_id
  where l.seller_id = seller_id_for(p_secret, false)
  order by l.created_at desc limit 200;
$$;

-- ---------------------------------------------------------------- for the admin
create or replace function public.admin_create_seller(p_secret text, p_name text, p_code text)
returns uuid language plpgsql security definer set search_path = public, extensions, pg_temp as $$
declare new_id uuid;
begin
  if not is_admin(p_secret) then raise exception 'not allowed'; end if;
  if p_name is null or char_length(trim(p_name)) not between 2 and 24 or p_name ~ '[[:cntrl:]]' then
    raise exception 'The name must be 2-24 characters.';
  end if;
  if char_length(regexp_replace(coalesce(p_code, ''), '[^A-Za-z0-9]', '', 'g')) < 12 then
    raise exception 'code too short';
  end if;
  if exists (select 1 from sellers where lower(name) = lower(trim(p_name))) then
    raise exception 'There is already a seller called %.', trim(p_name);
  end if;
  insert into sellers (name, code_hash) values (trim(p_name), seller_code_hash(p_code)) returning id into new_id;
  return new_id;
end $$;

create or replace function public.admin_sellers(p_secret text)
returns table (id uuid, name text, active boolean, created_at timestamptz, listings bigint)
language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  if not is_admin(p_secret) then raise exception 'not allowed'; end if;
  return query
    select s.id, s.name, s.active, s.created_at,
           (select count(*) from listings l where l.seller_id = s.id and l.status = 'active'
                                              and l.created_at > now() - interval '30 days')
    from sellers s order by lower(s.name);
end $$;

create or replace function public.admin_set_seller_active(p_secret text, p_id uuid, p_active boolean)
returns boolean language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  if not is_admin(p_secret) then raise exception 'not allowed'; end if;
  update sellers set active = coalesce(p_active, false) where id = p_id;
  return found;
end $$;

create or replace function public.admin_new_seller_code(p_secret text, p_id uuid, p_code text)
returns boolean language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  if not is_admin(p_secret) then raise exception 'not allowed'; end if;
  if char_length(regexp_replace(coalesce(p_code, ''), '[^A-Za-z0-9]', '', 'g')) < 12 then
    raise exception 'code too short';
  end if;
  update sellers set code_hash = seller_code_hash(p_code) where id = p_id;
  return found;
end $$;

create or replace function public.admin_delete_seller(p_secret text, p_id uuid)
returns boolean language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  if not is_admin(p_secret) then raise exception 'not allowed'; end if;
  delete from sellers where id = p_id;          -- their listings go with them
  return found;
end $$;

create or replace function public.admin_remove_listing(p_secret text, p_id uuid)
returns boolean language plpgsql security definer set search_path = public, extensions, pg_temp as $$
begin
  if not is_admin(p_secret) then raise exception 'not allowed'; end if;
  delete from listings where id = p_id;
  return found;
end $$;

revoke all on function public.add_listing(text, text, text, jsonb, numeric, text) from public;
revoke all on function public.seller_check(text) from public;
revoke all on function public.admin_create_seller(text, text, text) from public;
revoke all on function public.admin_sellers(text) from public;
revoke all on function public.admin_set_seller_active(text, uuid, boolean) from public;
revoke all on function public.admin_new_seller_code(text, uuid, text) from public;
revoke all on function public.admin_delete_seller(text, uuid) from public;
revoke all on function public.admin_remove_listing(text, uuid) from public;
grant execute on function public.add_listing(text, text, text, jsonb, numeric, text) to anon, authenticated;
grant execute on function public.seller_check(text) to anon, authenticated;
grant execute on function public.remove_listing(text, uuid) to anon, authenticated;
grant execute on function public.mark_sold(text, uuid) to anon, authenticated;
grant execute on function public.my_listings(text) to anon, authenticated;
grant execute on function public.admin_create_seller(text, text, text) to anon, authenticated;
grant execute on function public.admin_sellers(text) to anon, authenticated;
grant execute on function public.admin_set_seller_active(text, uuid, boolean) to anon, authenticated;
grant execute on function public.admin_new_seller_code(text, uuid, text) to anon, authenticated;
grant execute on function public.admin_delete_seller(text, uuid) to anon, authenticated;
grant execute on function public.admin_remove_listing(text, uuid) to anon, authenticated;
