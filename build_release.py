"""
Build a release of Pet Art Filter.

    python build_release.py              build the exe, self-test it, make the zip and
                                         install it as your own app (+ Desktop shortcut)
    python build_release.py --publish    ...and upload the zip as a GitHub release, so every
                                         player's app offers it as an update
    python build_release.py --no-install build + zip only

The version number comes from updater.APP_VERSION - raise it before publishing.
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import updater  # noqa: E402

VER = updater.APP_VERSION
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
INSTALL_DIR = os.path.join(DESKTOP, "Pet Art Filter", "Pet Art Filter")
WORK = os.path.join(os.environ.get("TEMP", HERE), "pafb")      # short path: PyInstaller hits 260-char limits
APPDATA = os.path.join(os.environ.get("APPDATA", ""), "PetArtFilter")

NOTES = """\
* Filter presets: run several filters at once, each glows in its own colour on the slot
* Official presets from the app owner appear automatically (marked with a star)
* Market: list your scanned pet arts for KK and browse what others sell
* Automatic updates: the app offers new versions with one click
"""

README = f"""PET ART FILTER {VER}  -  Metin2 Jintea
==========================================

Finds good pet arts for you. Hover your pet arts in the inventory or stash and the
good ones light up right in the game - each filter preset in its own colour.

The app only takes screenshots. It never clicks or types anything in the game.


START
-----
1. Unzip this whole folder somewhere (e.g. Desktop). Keep all files together.
2. Double-click "Pet Art Filter.exe".
   Windows may say "Windows protected your PC" because the app isn't signed:
   click "More info" -> "Run anyway".
3. Run Metin2 in WINDOWED or BORDERLESS mode (not fullscreen).


FIRST-TIME SETUP (once)
-----------------------
Setup -> "Find grid with one click" -> click any slot of your open inventory.
Then the same with your stash open. Inventory / stash and their pages are found
automatically. Blue lines show the slots it found.


FILTER PRESETS
--------------
Filter page:
 - Presets: make several (+ New). Each has a colour and can be switched on/off (the dot).
   A pet art matching a preset glows in that preset's colour (several presets = several rings).
 - Star presets are official ones from the app owner. Changing one makes your own copy.
 - Click a bonus = WANT, right-click = MUST. Slider = minimum value.


SCAN
----
F6 (or "Start scanning") and hover your pet arts.
Glow = good.  Red X = not all 7 bonuses read -> hover again.  Yellow dot = too fast.
F7 clears all marks. Every stash/inventory page keeps its own marks.


MARKET
------
Market page: browse pet arts other players sell (prices in KK), sort, search,
"Matches my presets". "Copy name" -> whisper the seller in game.
To sell: Scanner -> select a pet art in Results -> "Sell selected" -> price in KK.
"My listings" -> mark sold / remove.


UPDATES
-------
When a new version is out, a purple box appears in the app: click "Install update".

Your settings are saved in:  %APPDATA%\\PetArtFilter
"""


def run(cmd, **kw):
    print(">", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    return subprocess.run(cmd, check=True, **kw)


def build():
    shutil.rmtree(WORK, ignore_errors=True)
    dist, work = os.path.join(WORK, "dist"), os.path.join(WORK, "work")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
         "--name", "Pet Art Filter", "--icon", os.path.join(HERE, "app.ico"),
         "--add-data", os.path.join(HERE, "font_memory.pkl") + ";.",
         "--add-data", os.path.join(HERE, "app.ico") + ";.",
         "--collect-data", "customtkinter", "--collect-all", "rapidocr_onnxruntime",
         "--paths", HERE, "--distpath", dist, "--workpath", work, "--specpath", WORK,
         os.path.join(HERE, "pet_art_filter.py")])
    app = os.path.join(dist, "Pet Art Filter")
    with open(os.path.join(app, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(README)
    return dist, app


def selftest(app):
    out = os.path.join(APPDATA, "selftest.txt")
    if os.path.exists(out):
        os.remove(out)
    subprocess.run([os.path.join(app, "Pet Art Filter.exe"), "--selftest"], timeout=180)
    with open(out, encoding="utf-8") as fh:
        res = fh.read()
    for f in ("selftest.txt", "selftest.pkl"):
        try:
            os.remove(os.path.join(APPDATA, f))
        except OSError:
            pass
    print("self-test:", res)
    if "teacher read" not in res:
        raise SystemExit("self-test FAILED - not releasing")


def make_zip(dist):
    base = os.path.join(DESKTOP, f"Pet Art Filter v{VER}")
    path = shutil.make_archive(base, "zip", dist, "Pet Art Filter")
    print(f"zip: {path} ({os.path.getsize(path) // 1_000_000} MB)")
    return path


def install(app):
    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Pet Art Filter.exe"], capture_output=True,
                             text=True).stdout
    if "Pet Art Filter.exe" in running:
        raise SystemExit("Close Pet Art Filter first, then run this again (or use --no-install).")
    os.makedirs(INSTALL_DIR, exist_ok=True)
    subprocess.run(["robocopy", os.path.join(app, "_internal"), os.path.join(INSTALL_DIR, "_internal"),
                    "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"])
    for f in ("Pet Art Filter.exe", "README.txt"):
        shutil.copy2(os.path.join(app, f), os.path.join(INSTALL_DIR, f))
    exe = os.path.join(INSTALL_DIR, "Pet Art Filter.exe")
    lnk = os.path.join(DESKTOP, "Pet Art Filter.lnk")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          f"$s.TargetPath='{exe}';$s.WorkingDirectory='{INSTALL_DIR}';$s.IconLocation='{exe},0';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print("installed:", exe, "\nshortcut:", lnk)


def publish(zip_path):
    if not updater.UPDATE_REPO:
        print("publish skipped: set UPDATE_REPO in updater.py first")
        return
    tag = f"v{VER}"
    notes = os.path.join(WORK, "notes.md")
    with open(notes, "w", encoding="utf-8") as fh:
        fh.write(NOTES)
    run(["gh", "release", "create", tag, zip_path, "--repo", updater.UPDATE_REPO,
         "--title", f"Pet Art Filter {VER}", "--notes-file", notes])
    print("published", tag)


if __name__ == "__main__":
    t = time.time()
    dist, app = build()
    selftest(app)
    zp = make_zip(dist)
    if "--no-install" not in sys.argv:
        install(app)
    if "--publish" in sys.argv:
        publish(zp)
    print(f"done in {time.time() - t:.0f}s - version {VER}")
