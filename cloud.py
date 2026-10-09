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
import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = "https://kqxumbdzwwioxfxvmpst.supabase.co"
SUPABASE_KEY = "sb_publishable_Z_N5IbXaISwnXlCJ1VPQOQ_JEomuCcM"   # public (publishable) key - safe to ship
TIMEOUT = 10


class CloudError(Exception):
    pass


def configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _req(method, path, body=None, params=None):
    if not configured():
        raise CloudError("The online market isn't connected yet.")
    if not SUPABASE_URL.lower().startswith("https://"):
        raise CloudError("Market URL must use https.")
    url = SUPABASE_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"apikey": SUPABASE_KEY, "Content-Type": "application/json", "Accept": "application/json"}
    if SUPABASE_KEY.startswith("eyJ"):    # legacy anon key (a JWT); new sb_publishable_ keys go in apikey only
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
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
    except (OSError, ValueError) as e:   # timeouts, connection resets, bad JSON
        raise CloudError(f"Market request failed ({type(e).__name__}).")


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
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:   # write + rename: a crash can't leave an empty key
        fh.write(k)
    os.replace(tmp, path)
    return k


def key_hash(k):
    return hashlib.sha256(k.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- market
def market(limit=300):
    """Active listings, newest first."""
    return _req("GET", "/rest/v1/market", params={"select": "*", "order": "created_at.desc",
                                                    "limit": str(limit)}) or []


# selling is invite-only: the admin creates sellers, each gets a private seller code
CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"     # no 0/O or 1/I mix-ups


def new_seller_code():
    """e.g. PAF-7K2M-Q9XD-4HTB-R8WN (80 random bits)."""
    raw = "".join(secrets.choice(CODE_CHARS) for _ in range(16))
    return "PAF-" + "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def seller_check(code):
    """-> {"name", "active"} for a valid seller code, else None."""
    rows = _rpc("seller_check", p_code=code) or []
    return rows[0] if rows else None


def my_listings(code):
    return _rpc("my_listings", p_secret=code) or []


def add_listing(code, contact, item, bonuses, price_kk, version, picture=None):
    """bonuses: [(name, value), ...]; picture: small WebP of the tooltip (bytes) or None.
    The seller name comes from the seller account."""
    args = dict(p_code=code, p_contact=contact, p_item=item, p_bonuses=[[b, v] for b, v in bonuses],
                p_price=price_kk, p_version=version)
    if picture:
        args["p_picture"] = base64.b64encode(picture).decode("ascii")
    return _rpc("add_listing", **args)


MAX_PICTURE = 40000


def listing_picture(listing_id):
    """-> WebP bytes of a listing's tooltip picture, or None."""
    data = _rpc("listing_picture", p_id=listing_id)
    if not isinstance(data, str) or len(data) > MAX_PICTURE * 4 // 3 + 8:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except ValueError:
        return None


def remove_listing(code, listing_id):
    return _rpc("remove_listing", p_secret=code, p_id=listing_id)


def mark_sold(code, listing_id):
    return _rpc("mark_sold", p_secret=code, p_id=listing_id)


# ---------------------------------------------------------------- sellers (admin)
def admin_create_seller(admin_key, name):
    """-> the new seller's code (shown once - only its hash is stored online)."""
    code = new_seller_code()
    _rpc("admin_create_seller", p_secret=admin_key, p_name=name, p_code=code)
    return code


def admin_sellers(admin_key):
    return _rpc("admin_sellers", p_secret=admin_key) or []


def admin_set_seller_active(admin_key, seller_id, active):
    return _rpc("admin_set_seller_active", p_secret=admin_key, p_id=seller_id, p_active=bool(active))


def admin_new_seller_code(admin_key, seller_id):
    code = new_seller_code()
    _rpc("admin_new_seller_code", p_secret=admin_key, p_id=seller_id, p_code=code)
    return code


def admin_delete_seller(admin_key, seller_id):
    return _rpc("admin_delete_seller", p_secret=admin_key, p_id=seller_id)


def admin_remove_listing(admin_key, listing_id):
    return _rpc("admin_remove_listing", p_secret=admin_key, p_id=listing_id)


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
