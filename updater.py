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
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

APP_VERSION = "1.1.0"
UPDATE_REPO = ""          # "githubuser/repository", set when the GitHub repo exists
EXE_NAME = "Pet Art Filter.exe"
USER_AGENT = f"PetArtFilter/{APP_VERSION}"


def _ver(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def is_newer(remote, local=APP_VERSION):
    return _ver(remote) > _ver(local)


def can_self_update():
    """Only the packaged .exe can replace itself (running from source: no)."""
    return bool(getattr(sys, "frozen", False)) and bool(UPDATE_REPO)


def check_latest(timeout=6):
    """-> dict(version, notes, url, size) for the latest release, or None."""
    if not UPDATE_REPO:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": USER_AGENT,
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rel = json.load(r)
    asset = next((a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".zip")), None)
    if not asset:
        return None
    return {"version": rel.get("tag_name", "").lstrip("vV"), "notes": rel.get("body") or "",
            "url": asset["browser_download_url"], "size": asset.get("size", 0)}


def download_and_install(info, progress=None):
    """Download + unpack the release, then hand over to a script that waits for this app
    to exit, copies the new files in and restarts it. Returns True when the caller
    should now close the app."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("self-update only works in the packaged app")
    install_dir = os.path.dirname(sys.executable)
    work = tempfile.mkdtemp(prefix="paf_update_")
    zpath = os.path.join(work, "update.zip")

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
    if info.get("size") and os.path.getsize(zpath) != info["size"]:
        raise RuntimeError("download incomplete")

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
    pid = os.getpid()
    with open(script, "w", encoding="ascii", errors="ignore") as fh:
        fh.write(f"""@echo off
rem wait for Pet Art Filter to close
:wait
tasklist /FI "PID eq {pid}" | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
rem replace only the app's own files
robocopy "{src}\\_internal" "{install_dir}\\_internal" /MIR /R:5 /W:1 /NFL /NDL /NJH /NJS /NP >nul
copy /Y "{src}\\{EXE_NAME}" "{install_dir}\\{EXE_NAME}" >nul
if exist "{src}\\README.txt" copy /Y "{src}\\README.txt" "{install_dir}\\README.txt" >nul
start "" "{install_dir}\\{EXE_NAME}"
rmdir /s /q "{work}" 2>nul
""")
    subprocess.Popen(["cmd", "/c", script], creationflags=0x08000000 | 0x00000008,  # no window, detached
                     close_fds=True)
    return True
