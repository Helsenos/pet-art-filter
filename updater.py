"""
Auto-update ("patcher") for the packaged app.

New versions are published as GitHub Releases with a zip that contains the
"Pet Art Filter" folder. On start the app asks GitHub for the latest release; if it is
newer, the user can install it with one click: the zip is downloaded and unpacked to a
temp folder, the app closes, a small script copies the new files over the install
folder and starts the new version.

Only the app's own files are replaced (the .exe, README and the _internal folder);
nothing else in the folder is touched, and settings live in %APPDATA%\\PetArtFilter.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

APP_VERSION = "1.1.12"
UPDATE_REPO = "Helsenos/pet-art-filter"
# Every release is signed with the owner's private key (kept only on the owner's PC);
# an update is installed only if its signature matches this public key.
RELEASE_PUBKEY = "ad8c40f4a28ef6770b4c3d5c4a72af1e9e5fd9b96cda1264afc81addbef909ae"
SIG_NAME = "release.sig"
EXE_NAME = "Pet Art Filter.exe"
USER_AGENT = f"PetArtFilter/{APP_VERSION}"


def _ver(v):
    nums = re.findall(r"\d+", v or "")[:4]          # 1.1.10 / hotfix 1.1.10.1
    return tuple(int(n) for n in nums) + (0,) * (4 - len(nums))


def is_newer(remote, local=APP_VERSION):
    return _ver(remote) > _ver(local)


def can_self_update():
    """Only the packaged .exe can replace itself (running from source: no)."""
    return bool(getattr(sys, "frozen", False)) and bool(UPDATE_REPO)


def _windows_download(url, path):
    """Download with Windows' own internet stack (the one Edge and Windows Update use):
    it follows the system's proxy settings and is often let through by antivirus web
    filters that stop other programs. Raises OSError on failure."""
    import ctypes
    try:
        ctypes.windll.wininet.DeleteUrlCacheEntryW(url)        # never an old cached copy
    except Exception:
        pass
    hr = ctypes.windll.urlmon.URLDownloadToFileW(None, url, path, 0, None)
    if hr != 0 or not os.path.exists(path):
        raise OSError(f"Windows download failed (0x{hr & 0xFFFFFFFF:08X})")


def _windows_get(url, limit=4 << 20):
    fd, path = tempfile.mkstemp(prefix="paf_dl_")
    os.close(fd)
    try:
        _windows_download(url, path)
        with open(path, "rb") as fh:
            return fh.read(limit)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _parse_release(rel):
    assets = rel.get("assets", [])
    ver = rel.get("tag_name", "").lstrip("vV")
    zips = [a for a in assets if a.get("name", "").lower().endswith(".zip")]
    asset = next((a for a in zips if a.get("name") == f"Pet.Art.Filter.v{ver}.zip"), zips[0] if zips else None)
    sig = next((a for a in assets if a.get("name", "") == SIG_NAME), None)
    if not asset:
        return None
    return {"version": rel.get("tag_name", "").lstrip("vV"), "notes": rel.get("body") or "",
            "url": asset["browser_download_url"], "size": asset.get("size", 0),
            "sig_url": sig["browser_download_url"] if sig else None}


def _from_version(ver):
    base = f"https://github.com/{UPDATE_REPO}/releases/download/v{ver}/"
    return {"version": ver, "notes": "", "url": base + f"Pet.Art.Filter.v{ver}.zip", "size": 0,
            "sig_url": base + SIG_NAME}


def _latest_via_windows():
    """Same two routes as below, through Windows' downloader."""
    try:
        data = _windows_get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest?t={os.getpid()}")
        return _parse_release(json.loads(data.decode("utf-8")))
    except Exception:
        html = _windows_get(f"https://github.com/{UPDATE_REPO}/releases/latest").decode("utf-8", "ignore")
        m = re.search(r"/releases/tag/v?(\d+(?:\.\d+){1,3})[\"'/?#]", html)
        if not m:
            raise OSError("release page didn't name a version")
        return _from_version(m.group(1))


def check_latest(timeout=6):
    """-> dict(version, notes, url, size, sig_url) for the latest release, or None.
    Asks GitHub's API first (it has the release notes). The API allows only 60 requests
    an hour per internet connection - shared by everyone behind the same address - so
    when it fails, the plain release page is used instead (no limit, no notes).
    Either way the download is checked against the signature before installing."""
    if not UPDATE_REPO:
        return None
    try:
        return _latest_from_api(timeout)
    except Exception as api_error:
        try:
            return _latest_from_page(timeout)
        except Exception as page_error:
            try:
                return _latest_via_windows()       # e.g. proxy / filter only Windows' stack passes
            except Exception as win_error:
                api_error.page_error = page_error  # everything failed: keep all reasons
                api_error.win_error = win_error
                raise api_error


def _latest_from_api(timeout):
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": USER_AGENT,
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rel = json.load(r)
    return _parse_release(rel)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _latest_from_page(timeout):
    """github.com/<repo>/releases/latest redirects to .../releases/tag/v1.2.3"""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(f"https://github.com/{UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": USER_AGENT})
    try:
        opener.open(req, timeout=timeout)
        loc = ""                     # no redirect: some filter answered instead of GitHub
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "") if e.code in (301, 302, 303, 307, 308) else ""
    m = re.search(r"/releases/tag/v?(\d+(?:\.\d+){1,3})$", loc)
    if not m:
        raise OSError("release page didn't name a version")
    return _from_version(m.group(1))


DOWNLOAD_PAGE = f"https://github.com/{UPDATE_REPO}/releases/latest"


def error_detail(e):
    """Technical reason (for finding out what blocks the update), both routes."""
    def one(x):
        return f"{type(x).__name__}: {getattr(x, 'reason', None) or x}"[:180]
    out = one(e)
    for extra in ("page_error", "win_error"):
        if getattr(e, extra, None) is not None:
            out += " | " + one(getattr(e, extra))
    return out


def describe_error(e):
    """Short, human reason why the update check failed."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (403, 429):
            return "GitHub is limiting update checks from your connection right now – try again later."
        return f"The update server answered with an error ({e.code})."
    reason = str(getattr(e, "reason", e)).lower()
    if "certificate" in reason or "ssl" in reason:
        return "Secure connection failed – check that your PC's date and time are right."
    if "timed out" in reason or "timeout" in reason:
        return "The update server didn't answer in time – check your internet or firewall."
    if "getaddrinfo" in reason or "name" in reason and "resolv" in reason:
        return "No internet connection (or GitHub is blocked)."
    return "Couldn’t reach the update server – check your internet or firewall."


def _official(url):
    """Downloads only from this app's own GitHub releases."""
    return isinstance(url, str) and url.startswith(f"https://github.com/{UPDATE_REPO}/releases/download/")


def signed_message(version, sha256_hex):
    return f"PetArtFilter release\n{version}\n{sha256_hex}".encode("utf-8")


def _verify(info, zpath):
    """The zip must be exactly the one the owner signed for this version."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    if not info.get("sig_url") or not _official(info["sig_url"]):
        raise RuntimeError("this update isn't signed - not installing it")
    try:
        req = urllib.request.Request(info["sig_url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read(4096)
    except Exception:
        raw = _windows_get(info["sig_url"], 4096)
    sig = json.loads(raw.decode("utf-8"))
    h = hashlib.sha256()
    with open(zpath, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if sig.get("version") != info["version"] or sig.get("sha256") != digest:
        raise RuntimeError("update signature doesn't match the download - not installing it")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(RELEASE_PUBKEY)).verify(
            bytes.fromhex(sig.get("sig", "")), signed_message(info["version"], digest))
    except (InvalidSignature, ValueError):
        raise RuntimeError("update signature is not valid - not installing it")


def download_and_install(info, progress=None):
    """Download + unpack the release, then hand over to a script that waits for this app
    to exit, copies the new files in and restarts it. Returns True when the caller
    should now close the app."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("self-update only works in the packaged app")
    install_dir = os.path.dirname(sys.executable)
    if not os.access(install_dir, os.W_OK):
        raise RuntimeError("the app's folder is read-only (e.g. Program Files) - "
                           "use the download page or move the app")
    work = tempfile.mkdtemp(prefix="paf_update_")
    try:
        return _download_and_install(info, progress, install_dir, work)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _download_and_install(info, progress, install_dir, work):
    zpath = os.path.join(work, "update.zip")

    if not _official(info.get("url")):
        raise RuntimeError("update doesn't come from the official download page")
    try:
        req = urllib.request.Request(info["url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r, open(zpath, "wb") as fh:
            total = int(r.headers.get("Content-Length") or info.get("size") or 0)
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    except Exception:
        if progress:
            progress(0.0)
        _windows_download(info["url"], zpath)      # no progress here, but it gets through
        if progress:
            progress(1.0)
    if info.get("size") and os.path.getsize(zpath) != info["size"]:
        raise RuntimeError("download incomplete")
    _verify(info, zpath)

    new_dir = os.path.join(work, "new")
    with zipfile.ZipFile(zpath) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError(f"damaged download ({bad})")
        z.extractall(new_dir)
    # the zip holds a "Pet Art Filter" folder (or the files directly)
    src = new_dir
    for root, _dirs, files in os.walk(new_dir):
        if EXE_NAME in files:
            src = root
            break
    if not os.path.exists(os.path.join(src, EXE_NAME)) or not os.path.isdir(os.path.join(src, "_internal")):
        raise RuntimeError("the downloaded update doesn't look like this app")

    script = os.path.join(work, "apply_update.cmd")
    # Paths go in as environment variables: the .cmd file itself stays pure ASCII, so
    # folders with accents (C:\Users\Jürgen) or other special characters still work.
    with open(script, "w", encoding="ascii") as fh:
        fh.write(r"""@echo off
set FIND=%SystemRoot%\System32\find.exe
set TASKLIST=%SystemRoot%\System32\tasklist.exe
set ROBOCOPY=%SystemRoot%\System32\robocopy.exe
set PING=%SystemRoot%\System32\PING.EXE
rem wait for Pet Art Filter to close (at most ~60 s)
set /a TRIES=0
:wait
set /a TRIES+=1
if %TRIES% GTR 60 goto timeout
"%TASKLIST%" /FI "PID eq %PAF_PID%" /NH | "%FIND%" "%PAF_PID%" >nul && ("%PING%" -n 2 127.0.0.1 >nul & goto wait)
cd /d "%PAF_DST%" || goto failed
rem leftovers of an interrupted update
if exist "%PAF_DST%\_internal_old" rmdir /s /q "%PAF_DST%\_internal_old"
if exist "%PAF_DST%\%PAF_EXE%.old" del /f /q "%PAF_DST%\%PAF_EXE%.old"
rem move the current version aside (instant, and undone if anything fails)
set /a TRIES=0
:aside
set /a TRIES+=1
ren "%PAF_DST%\_internal" "_internal_old" 2>nul && goto moved
if %TRIES% GTR 10 goto failed
"%PING%" -n 2 127.0.0.1 >nul
goto aside
:moved
ren "%PAF_DST%\%PAF_EXE%" "%PAF_EXE%.old" 2>nul || goto restore
"%ROBOCOPY%" "%PAF_SRC%\_internal" "%PAF_DST%\_internal" /E /R:10 /W:1 /NFL /NDL /NJH /NJS /NP >nul
if %errorlevel% GEQ 8 goto restore
copy /Y "%PAF_SRC%\%PAF_EXE%" "%PAF_DST%\%PAF_EXE%" >nul || goto restore
if exist "%PAF_SRC%\README.txt" copy /Y "%PAF_SRC%\README.txt" "%PAF_DST%\README.txt" >nul
rmdir /s /q "%PAF_DST%\_internal_old" 2>nul
del /f /q "%PAF_DST%\%PAF_EXE%.old" 2>nul
start "" "%PAF_DST%\%PAF_EXE%"
rmdir /s /q "%PAF_WORK%" 2>nul & exit /b 0
:restore
if exist "%PAF_DST%\_internal_old" rmdir /s /q "%PAF_DST%\_internal" 2>nul
if exist "%PAF_DST%\_internal_old" ren "%PAF_DST%\_internal_old" "_internal"
if exist "%PAF_DST%\%PAF_EXE%.old" del /f /q "%PAF_DST%\%PAF_EXE%" 2>nul
if exist "%PAF_DST%\%PAF_EXE%.old" ren "%PAF_DST%\%PAF_EXE%.old" "%PAF_EXE%"
:failed
echo Update could not be installed (files in use?). Starting the current version.> "%PAF_DST%\update_failed.txt"
start "" "%PAF_DST%\%PAF_EXE%"
rmdir /s /q "%PAF_WORK%" 2>nul & exit /b 1
:timeout
echo Update could not be installed: the app didn't close.> "%PAF_DST%\update_failed.txt"
rmdir /s /q "%PAF_WORK%" 2>nul & exit /b 1
""")
    env = dict(os.environ, PAF_PID=str(os.getpid()), PAF_SRC=src, PAF_DST=install_dir, PAF_EXE=EXE_NAME,
               PAF_WORK=work)
    # Hidden, but WITH an (invisible) console: in a fully detached process (no console)
    # the tasklist | find pipe never finishes and the update would hang forever.
    CREATE_NO_WINDOW, NEW_GROUP, BREAKAWAY = 0x08000000, 0x00000200, 0x01000000
    cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    run = [cmd, "/d", "/c", ".\\" + os.path.basename(script)]     # .\ : found even where cmd skips the current folder
    try:
        subprocess.Popen(run, cwd=work, env=env, close_fds=True,
                         creationflags=CREATE_NO_WINDOW | NEW_GROUP | BREAKAWAY)
    except OSError:     # not allowed to leave the job (rare): start it inside
        subprocess.Popen(run, cwd=work, env=env, close_fds=True,
                         creationflags=CREATE_NO_WINDOW | NEW_GROUP)
    return True
