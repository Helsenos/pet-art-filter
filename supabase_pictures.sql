-- =====================================================================
-- Pet Art Filter - tooltip pictures on listings (run AFTER supabase_hardening.sql)
-- Paste this whole file into Supabase -> SQL Editor -> New query -> Run.
-- Safe to run again.
--  * a listing can carry a small picture (WebP, max 40 KB) of the in-game tooltip
--  * the market list stays light: pictures are fetched one by one with listing_picture()
-- =====================================================================

alter table public.listings add column if not exists picture bytea
  check (picture is null or octet_length(picture) <= 40000);

create or replace view public.market as
  select l.id, l.created_at, s.name as seller, l.contact, l.item, l.bonuses, l.price_kk,
         (l.picture is not null) as has_picture
  from public.listings l join public.sellers s on s.id = l.seller_id
  where l.status = 'active' and s.active and l.created_at > now() - interval '30 days';
revoke all on public.market from anon, authenticated;
grant select on public.market to anon, authenticated;

-- the old version without the picture parameter is replaced (older apps still work: it's optional)
drop function if exists public.add_listing(text, text, text, jsonb, numeric, text);

create or replace function public.add_listing(p_code text, p_contact text, p_item text,
                                              p_bonuses jsonb, p_price numeric, p_version text,
                                              p_picture text default null)
returns uuid language plpgsql security definer set search_path = public, extensions, pg_temp as $$
declare sid uuid; sname text; n int; new_id uuid; e jsonb; pic bytea;
  known text[] := array[
    'Armor',
    'Attack Speed',
    'Attack Value',
    'Average Damage',
    'Average Damage Resistance',
    'Blackout chance',
    'Blitzschaden',
    'Burn Damage',
    'Casting Speed',
    'Chance for EXP Bonus',
    'Chance for piercing Hits',
    'Chance of critical hit',
    'Curse Damage',
    'Defence',
    'HP gain on hit',
    'Lightspeed Factor',
    'Magical Power',
    'Max. HP',
    'Max. SP',
    'Poison Damage',
    'Poisoning chance',
    'Portaldamage',
    'Resistance against Skill Damage',
    'Resistance against critical hits',
    'Resistance against piercing hits',
    'Resistance to Monster',
    'SP gain on hit',
    'Skill Damage',
    'Skill Speed',
    'Slowing chance',
    'Strong against Animal',
    'Strong against Devils',
    'Strong against Esoteric',
    'Strong against Half Humans',
    'Strong against Metinstones',
    'Strong against Monsters',
    'Strong against Orcs',
    'Strong against Undead',
    'Thorns',
    'Universality',
    'increased Item-Drop'
  ];
begin
  sid := seller_id_for(p_code, true);
  if sid is null then raise exception 'Your seller code isn''t valid (or was disabled). Ask the app owner for one.'; end if;
  select name into sname from sellers where id = sid;
  if p_item is null or p_price is null then raise exception 'missing fields'; end if;
  if p_price <= 0 or p_price > 10000000 then raise exception 'invalid price'; end if;
  if char_length(coalesce(p_contact, '')) > 80 or char_length(p_item) not between 1 and 40 then
    raise exception 'text too long';
  end if;
  if concat(p_contact, p_item, p_version) ~ '[[:cntrl:]]'
     or concat(p_contact, p_item, p_version) ~ '[​-‏‪-‮⁠-⁩﻿]' then
    raise exception 'invalid characters';
  end if;
  if p_bonuses is null or jsonb_typeof(p_bonuses) <> 'array' then raise exception 'invalid bonuses'; end if;
  if jsonb_array_length(p_bonuses) not between 1 and 7 or octet_length(p_bonuses::text) > 1000 then
    raise exception 'invalid bonuses';
  end if;
  for e in select * from jsonb_array_elements(p_bonuses) loop
    if jsonb_typeof(e) <> 'array' or jsonb_array_length(e) <> 2 or jsonb_typeof(e->0) <> 'string'
       or jsonb_typeof(e->1) <> 'number' then
      raise exception 'invalid bonuses';
    end if;
    if not (e->>0 = any(known)) then raise exception 'unknown bonus: %', left(e->>0, 40); end if;
    if (e->>1)::numeric < 0 or (e->>1)::numeric > 1000000 then raise exception 'invalid bonus value'; end if;
  end loop;

  -- optional picture of the in-game tooltip: a small WebP image only
  if p_picture is not null then
    begin
      pic := decode(p_picture, 'base64');
    exception when others then
      raise exception 'invalid picture';
    end;
    if octet_length(pic) not between 100 and 40000
       or substring(pic from 1 for 4) <> 'RIFF'::bytea
       or substring(pic from 9 for 4) <> 'WEBP'::bytea then
      raise exception 'invalid picture';
    end if;
  end if;

  perform pg_advisory_xact_lock(727001);
  delete from listings where created_at < now() - interval '60 days'
                          or (status = 'sold' and created_at < now() - interval '7 days');
  select count(*) into n from listings where seller_id = sid and status = 'active'
                                         and created_at > now() - interval '30 days';
  if n >= 60 then raise exception 'You already have 60 active listings.'; end if;
  select count(*) into n from listings where seller_id = sid and created_at > now() - interval '1 minute';
  if n >= 10 then raise exception 'Slow down a bit - too many new listings in one minute.'; end if;
  select count(*) into n from listings where seller_id = sid and created_at > now() - interval '1 day';
  if n >= 200 then raise exception 'You listed 200 pet arts today - that''s the daily limit.'; end if;
  select count(*) into n from listings where created_at > now() - interval '1 minute';
  if n >= 30 then raise exception 'The market is busy - try again in a minute.'; end if;
  select count(*) into n from listings where status = 'active' and created_at > now() - interval '30 days';
  if n >= 5000 then raise exception 'The market is full right now.'; end if;

  insert into listings (seller, seller_id, contact, item, bonuses, price_kk, owner_hash, app_version, picture)
  values (sname, sid, coalesce(trim(p_contact), ''), trim(p_item), p_bonuses, p_price, null,
          left(coalesce(p_version, ''), 20), pic)
  returning id into new_id;
  return new_id;
end $$;

revoke all on function public.add_listing(text, text, text, jsonb, numeric, text, text) from public;
grant execute on function public.add_listing(text, text, text, jsonb, numeric, text, text) to anon, authenticated;

-- picture of one listing that is visible on the market (base64 WebP), or null
create or replace function public.listing_picture(p_id uuid)
returns text language sql stable security definer set search_path = public, extensions, pg_temp as $$
  select encode(l.picture, 'base64')
  from listings l join sellers s on s.id = l.seller_id
  where l.id = p_id and l.status = 'active' and s.active and l.created_at > now() - interval '30 days';
$$;
revoke all on function public.listing_picture(uuid) from public;
grant execute on function public.listing_picture(uuid) to anon, authenticated;
