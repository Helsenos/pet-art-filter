"""
Online features (Supabase): the pet art market and official filter presets.

All access goes through the public "anon" key, which is safe to ship in the app:
the database only allows reading the market and the official presets, and changing
anything goes through functions that check a secret:
  * every player has a random "owner key" (kept in their data folder) that proves
    which listings are theirs;
  * the admin key (only on the admin's PC, file admin.key) allows publishing
    official presets. Only its hash is stored in the database.
"""
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = ""   # e.g. "https://abcdefgh.supabase.co" - set when the project exists
SUPABASE_KEY = ""   # the project's public anon key
TIMEOUT = 10


class CloudError(Exception):
    pass


def configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _req(method, path, body=None, params=None):
    if not configured():
        raise CloudError("The online market isn't connected yet.")
    url = SUPABASE_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "ignore")
        try:
            msg = json.loads(text).get("message") or text
        except Exception:
            msg = text or str(e)
        raise CloudError(msg[:200])
    except urllib.error.URLError as e:
        raise CloudError(f"No connection to the market ({e.reason}).")


def _rpc(fn, **args):
    return _req("POST", f"/rest/v1/rpc/{fn}", args)


def load_key(path, create=True):
    """Read a secret key file; create a new random key if it doesn't exist."""
    try:
        with open(path, encoding="utf-8") as fh:
            k = fh.read().strip()
            if k:
                return k
    except OSError:
        pass
    if not create:
        return None
    k = secrets.token_urlsafe(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(k)
    return k


def key_hash(k):
    return hashlib.sha256(k.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- market
def market(limit=300):
    """Active listings, newest first."""
    return _req("GET", "/rest/v1/market", params={"select": "*", "order": "created_at.desc",
                                                    "limit": str(limit)}) or []


def my_listings(owner_key):
    return _rpc("my_listings", p_secret=owner_key) or []


def add_listing(owner_key, seller, contact, item, bonuses, price_kk, version):
    """bonuses: [(name, value), ...]"""
    return _rpc("add_listing", p_secret=owner_key, p_seller=seller, p_contact=contact, p_item=item,
                p_bonuses=[[b, v] for b, v in bonuses], p_price=price_kk, p_version=version)


def remove_listing(owner_key, listing_id):
    return _rpc("remove_listing", p_secret=owner_key, p_id=listing_id)


def mark_sold(owner_key, listing_id):
    return _rpc("mark_sold", p_secret=owner_key, p_id=listing_id)


# ---------------------------------------------------------------- official presets
def official_presets():
    return _req("GET", "/rest/v1/official_presets",
                params={"select": "*", "order": "sort.asc,updated_at.asc"}) or []


def publish_preset(admin_key, name, color, data, preset_id=None, sort=0):
    return _rpc("admin_publish_preset", p_secret=admin_key, p_id=preset_id, p_name=name,
                p_color=color, p_data=data, p_sort=sort)


def delete_preset(admin_key, preset_id):
    return _rpc("admin_delete_preset", p_secret=admin_key, p_id=preset_id)


def check_admin(admin_key):
    return bool(_rpc("is_admin", p_secret=admin_key))
