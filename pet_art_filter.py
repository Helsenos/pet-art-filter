"""
Metin2 Jintea - Pet Art Filter

Sweep your mouse over pet artifacts in the stash / inventory. The app grabs
each tooltip, reads it with a reader trained on the game's own font, checks it
against your filter and makes the slots of good pet arts glow.

Read-only: it only takes screenshots. It never clicks or types in the game.

Hotkeys (work while the game is focused):
    F6  start / stop scanning
    F7  clear slot marks (do this after switching stash tab)
"""
import asyncio
import colorsys
import ctypes
import ctypes.wintypes as wt
import difflib
import json
import os
import queue
import shutil
import sys
import re
import unicodedata
import threading
import time
import tkinter as tk
from tkinter import ttk, simpledialog

import customtkinter as ctk

import mss
from PIL import Image, ImageChops, ImageStat
import numpy as np

import font_reader
import grid_finder

MSS = getattr(mss, "MSS", None) or mss.mss

try:
    import winsound
except ImportError:
    winsound = None

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

BONUSES = {
    "Resistance against critical hits": [1, 10],
    "Resistance against piercing hits": [1, 10],
    "Strong against Animal": [5, 20],
    "Strong against Devils": [1, 15],
    "Strong against Half Humans": [1, 15],
    "Strong against Metinstones": [1, 20],
    "Strong against Esoteric": [1, 15],
    "Strong against Monsters": [1, 20],
    "Strong against Orcs": [1, 15],
    "Strong against Undead": [1, 15],
    "Attack Value": [10, 100],
    "Attack Speed": [1, 10],
    "Casting Speed": [1, 10],
    "Chance of critical hit": [1, 10],
    "Defence": [50, 300],
    "Armor": [10, 200],
    "Portaldamage": [1, 30],
    "Burn Damage": [1, 20],
    "Curse Damage": [1, 20],
    "Lightspeed Factor": [1, 3],
    "Magical Power": [1, 20],
    "Poison Damage": [1, 20],
    "Resistance to Monster": [1, 5],
    "Skill Speed": [1, 10],
    "Thorns": [50, 500],
    "Blitzschaden": [1, 20],
    "Universality": [1, 15],
    "Chance for EXP Bonus": [1, 20],
    "increased Item-Drop": [1, 20],
    "Max. HP": [100, 3000],
    "Max. SP": [100, 3000],
    "Average Damage": [1, 10],
    "Average Damage Resistance": [1, 5],
    "Chance for piercing Hits": [1, 10],
    "Poisoning chance": [1, 3],
    "Skill Damage": [1, 10],
    "Resistance against Skill Damage": [1, 5],
    "Slowing chance": [1, 3],
    "HP gain on hit": [1, 20],
    "SP gain on hit": [1, 35],
    "Blackout chance": [1, 3],
}

CATEGORIES = [
    ("Strong against", ["Strong against Monsters", "Strong against Metinstones", "Strong against Animal",
                        "Strong against Devils", "Strong against Half Humans", "Strong against Esoteric",
                        "Strong against Orcs", "Strong against Undead"]),
    ("Offense", ["Attack Value", "Average Damage", "Skill Damage", "Chance of critical hit",
                 "Chance for piercing Hits", "Attack Speed", "Casting Speed", "Skill Speed",
                 "Magical Power", "Universality", "Portaldamage", "Lightspeed Factor"]),
    ("Elements & status", ["Burn Damage", "Curse Damage", "Poison Damage", "Blitzschaden", "Blackout chance",
                           "Poisoning chance", "Slowing chance"]),
    ("Defense", ["Max. HP", "Max. SP", "Defence", "Armor", "Thorns",
                 "Average Damage Resistance", "Resistance against Skill Damage",
                 "Resistance against critical hits", "Resistance against piercing hits",
                 "Resistance to Monster"]),
    ("Utility", ["Chance for EXP Bonus", "increased Item-Drop", "HP gain on hit", "SP gain on hit"]),
]
assert sorted(n for _, ns in CATEGORIES for n in ns) == sorted(BONUSES)

if getattr(sys, "frozen", False):
    # packaged .exe: settings + learned font live in the user's own AppData folder;
    # the font memory shipped with the app is the starting point
    BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PetArtFilter")
    os.makedirs(APP_DIR, exist_ok=True)
    _seed = os.path.join(BUNDLE_DIR, "font_memory.pkl")
    if not os.path.exists(os.path.join(APP_DIR, "font_memory.pkl")) and os.path.exists(_seed):
        shutil.copy(_seed, os.path.join(APP_DIR, "font_memory.pkl"))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "pet_art_config.json")
DEBUG_DIR = os.path.join(APP_DIR, "debug_captures")
FONT_PATH = os.path.join(APP_DIR, "font_memory.pkl")  # what the app has learned of the game font
MODES = ["Ignore", "Want", "Must"]
OCR_WORKERS = 3
OCR_SCALES = (2.5, 3.0)  # each tooltip is read at these zooms and the results merged
ARTS_PER_PET = 7  # every pet art has 7 bonuses; fewer read = OCR missed some

BG, PANEL, FG, MUTED = "#0c0f16", "#151a25", "#e9edf5", "#8790a5"
GOOD, BAD, WARN, ACCENT = "#34d399", "#f87171", "#fbbf24", "#7c5cff"
ACCENT_H, CYAN = "#6847f5", "#22d3ee"
SIDEBAR, CARD, CARD2, BORDER = "#10141d", "#151a25", "#1d2331", "#262e40"
MUST_COL = "#f5b041"
KEY_COLOR = "#010203"  # overlay transparency key

VK_F6, VK_F7 = 0x75, 0x76


# ================================================================ parsing
def _norm(s):
    return re.sub(r"[^a-z]", "", s.lower())


NORM_NAMES = {_norm(n): n for n in BONUSES}


def match_bonus(text):
    key = _norm(text)
    if len(key) < 4:
        return None
    if key in NORM_NAMES:
        return NORM_NAMES[key]
    hit = difflib.get_close_matches(key, NORM_NAMES.keys(), n=1, cutoff=0.8)
    if not hit and "m" in key:  # OCR often reads "rn" as "m" (Thorns -> Thoms, Burn -> Bum)
        hit = difflib.get_close_matches(key.replace("m", "rn"), NORM_NAMES.keys(), n=1, cutoff=0.8)
    return NORM_NAMES[hit[0]] if hit else None


def art_block(lines):
    """Tooltip layout:
        Brown Collar +0                 <- name
        From PLvl: 250                  \
        Max. SP +150                     > base stats of the collar, skipped
        Defense against Magic Power +2  /
        Curse Damage +7% ... (7 arts)   <- what we filter on
        [Pet Artifact]                  <- end
    Returns (name, art lines)."""
    low = [_norm(l) for l in lines]
    lvl = next((i for i, l in enumerate(low) if "plvl" in l or "fromp" in l), None)
    name = ""
    if lvl:
        name = lines[lvl - 1].strip()
    elif lines:
        name = lines[0].strip()
    start = 0
    magic = [i for i, l in enumerate(low) if "magic" in l and "defen" in l]
    if magic:
        start = magic[0] + 1
    elif lvl is not None:
        start = lvl + 3  # PLvl line + 2 base stat lines
    end = next((i for i, l in enumerate(low) if i >= start and "petart" in l), len(lines))
    return name, lines[start:end]


TOOLTIP_HALF = 150  # px either side of the tooltip's centre line that still count as tooltip


def tooltip_lines(wlines, cx):
    """Keep only the words that belong to the tooltip. Every tooltip line is centred on
    the same x, so find the x most lines agree on, measure the tooltip width from those
    lines, and drop words (chat, stash tips, other UI) outside that column.
    `cx` (slot centre) is the fallback."""
    def mid(ws):
        return (min(w[1] for w in ws) + max(w[1] + w[3] for w in ws)) / 2

    mids = [mid(ws) for ws in wlines]
    best = max(mids, key=lambda m: sum(abs(m - o) <= 6 for o in mids), default=cx)
    members = [ws for ws, m in zip(wlines, mids) if abs(m - best) <= 6]
    top, bottom = -1e9, 1e9
    if len(members) >= 3:
        # centred lines that are far apart vertically (e.g. "Inventory" window title way
        # above) are not the tooltip: split on big gaps and keep the biggest stack
        members.sort(key=lambda ws: ws[0][2])
        groups, cur = [], [members[0]]
        for a, b in zip(members, members[1:]):
            if b[0][2] - a[0][2] > 45:
                groups.append(cur)
                cur = []
            cur.append(b)
        groups.append(cur)
        members = max(groups, key=len)
        top, bottom = members[0][0][2] - 25, members[-1][0][2] + 25
    if len(members) >= 3:
        centre = sum(mid(ws) for ws in members) / len(members)
        half = max((max(w[1] + w[3] for w in ws) - min(w[1] for w in ws)) / 2 for ws in members)
    else:
        centre, half = cx, TOOLTIP_HALF
    lo, hi = centre - half - 10, centre + half + 10
    out = []
    for ws in sorted(wlines, key=lambda ws: ws[0][2]):
        if not top <= ws[0][2] <= bottom:
            continue
        keep = [w[0] for w in ws if lo <= w[1] + w[3] / 2 <= hi]
        if keep:
            out.append(" ".join(keep))
    return out


def fix_ocr(text):
    """Undo typical OCR damage: accents (Damáge -> Damage) and letter O read
    instead of zero inside numbers (1,7óó -> 1,700)."""
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(ch))
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"(?<=[\d,.])[oO]|[oO](?=\d)", "0", text)
    return text


def parse_tooltip(lines):
    name, block = art_block([fix_ocr(l) for l in lines])
    return name, parse_lines(block)


def parse_passes(passes):
    """Each tooltip is read at several zoom levels; a line one read garbles is often
    fine in another. Merge them: union of bonuses, value = the one most reads agree
    on (values outside the bonus range lose)."""
    names, values, order = [], {}, []
    for lines in passes:
        name, found = parse_tooltip(lines)
        if name:
            names.append(name)
        for b, v in found:
            if b not in values:
                values[b] = []
                order.append(b)
            values[b].append(v)
    found = []
    for b in order:
        lo, hi = BONUSES[b]
        vs = values[b]
        ok = [v for v in vs if lo <= v <= hi]
        if ok:  # a value outside the bonus range is a misread, not a real roll
            found.append((b, max(ok, key=ok.count)))
    name = max(names, key=names.count) if names else ""
    return name, found


def parse_lines(lines):
    """OCR lines -> [(bonus, value)]. Handles '+7%', '1%' and '20 HP gain on hit'."""
    found, seen = [], set()
    for raw in lines:
        nums = re.findall(r"\d+", raw.replace(".", "").replace(",", ""))
        if not nums:
            continue
        name_part = re.sub(r"[+\-]?\s*\d[\d.,]*\s*%?", " ", raw)
        bonus = match_bonus(name_part)
        if bonus and bonus not in seen:
            seen.add(bonus)
            found.append((bonus, int(nums[-1])))
    return found


# ================================================================ screen helpers
def cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def virtual_screen():
    with MSS() as s:
        m = s.monitors[0]
    return m["left"], m["top"], m["width"], m["height"]


def slot_at(grids, x, y):
    for gi, g in enumerate(grids):
        if g["x"] <= x < g["x"] + g["w"] and g["y"] <= y < g["y"] + g["h"]:
            c = int((x - g["x"]) / (g["w"] / g["cols"]))
            r = int((y - g["y"]) / (g["h"] / g["rows"]))
            return gi, min(r, g["rows"] - 1), min(c, g["cols"] - 1)
    return None


def slot_rect(g, r, c):
    cw, ch = g["w"] / g["cols"], g["h"] / g["rows"]
    return (g["x"] + c * cw, g["y"] + r * ch, g["x"] + (c + 1) * cw, g["y"] + (r + 1) * ch)


ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
         "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"]


def roman(i):
    return ROMAN[i] if 0 <= i < len(ROMAN) else str(i + 1)


def tab_rect(t, i):
    cw, ch = t["w"] / t["cols"], t["h"] / t["rows"]
    r, c = divmod(i, t["cols"])
    return (t["x"] + c * cw, t["y"] + r * ch, t["x"] + (c + 1) * cw, t["y"] + (r + 1) * ch)


def active_tab(img, rows, cols, exclude=None):
    """The selected page button looks different from all the others (lighter / green).
    Compare each button's background colour (median, so the I/II/III text doesn't
    matter) against the typical button and return the odd one out, or None."""
    cw, ch = img.width / cols, img.height / rows
    idxs = [i for i in range(rows * cols) if i != exclude]
    cols_rgb = []
    for i in idxs:
        r, c = divmod(i, cols)
        box = (int(c * cw + cw * 0.15), int(r * ch + ch * 0.2),
               int((c + 1) * cw - cw * 0.15), int((r + 1) * ch - ch * 0.2))
        cols_rgb.append(ImageStat.Stat(img.crop(box)).median[:3])
    if len(cols_rgb) < 3:
        return None
    typical = [sorted(v[k] for v in cols_rgb)[len(cols_rgb) // 2] for k in range(3)]
    dist = [sum((a - b) ** 2 for a, b in zip(v, typical)) ** 0.5 for v in cols_rgb]
    order = sorted(range(len(dist)), key=lambda i: -dist[i])
    best, second = dist[order[0]], dist[order[1]]
    if best >= 15 and best >= 1.6 * max(second, 1):
        return idxs[order[0]]
    if exclude is not None and best < 10:
        return exclude  # all the other buttons look alike, so the hovered one is the open page
    return None


def detect_page(sct, tabs):
    x, y = cursor_pos()
    hovered = None
    if tabs["x"] <= x < tabs["x"] + tabs["w"] and tabs["y"] <= y < tabs["y"] + tabs["h"]:
        # a hovered button lights up, so leave it out of the comparison
        c = min(int((x - tabs["x"]) / (tabs["w"] / tabs["cols"])), tabs["cols"] - 1)
        r = min(int((y - tabs["y"]) / (tabs["h"] / tabs["rows"])), tabs["rows"] - 1)
        hovered = r * tabs["cols"] + c
    shot = sct.grab({"left": int(tabs["x"]), "top": int(tabs["y"]),
                     "width": int(tabs["w"]), "height": int(tabs["h"])})
    return active_tab(Image.frombytes("RGB", shot.size, shot.rgb), tabs["rows"], tabs["cols"], hovered)


class PageWatcher(threading.Thread):
    """Keeps `pages[grid index]` up to date with the page/tab open in the game."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app

    def run(self):
        with MSS() as sct:
            while True:
                for gi, g in enumerate(list(self.app.grids)):
                    if not g.get("tabs"):
                        continue
                    try:
                        pg = detect_page(sct, g["tabs"])
                    except Exception:
                        pg = None
                    if pg is not None and self.app.pages.get(gi) != pg:
                        self.app.pages[gi] = pg
                        self.app.event_q.put(("page", gi))
                time.sleep(0.04)


# ================================================================ scanner
class Scanner(threading.Thread):
    """Follows the mouse. Each time it sits on a new slot for `delay` ms, grabs
    the area where the tooltip shows up and queues it for OCR."""

    def __init__(self, grids, pages, icons, marks, cap, delay_ms, ocr_q, event_q):
        super().__init__(daemon=True)
        self.grids, self.pages, self.icons, self.marks, self.cap = grids, pages, icons, marks, cap
        self.delay = delay_ms / 1000.0
        self.ocr_q, self.event_q = ocr_q, event_q
        self.stop_evt = threading.Event()
        self.seq = 0

    def stop(self):
        self.stop_evt.set()

    def run(self):
        vx, vy, vw, vh = virtual_screen()
        cur, entered, grabbed = None, 0.0, False
        with MSS() as sct:
            while not self.stop_evt.is_set():
                slot = slot_at(self.grids, *cursor_pos())
                if slot is not None:
                    slot = (slot[0], self.pages.get(slot[0], 0), slot[1], slot[2])
                now = time.perf_counter()
                if slot != cur:
                    if cur is not None and not grabbed:
                        self.event_q.put(("missed", cur))
                    cur, entered, grabbed = slot, now, False
                elif cur is not None and not grabbed and now - entered >= self.delay:
                    gi, pg, r, c = cur
                    tabs = self.grids[gi].get("tabs")
                    if tabs:
                        now_pg = detect_page(sct, tabs)
                        if now_pg is not None and now_pg != pg:
                            self.pages[gi] = pg = now_pg
                            cur = (gi, pg, r, c)
                    x0, y0, x1, y1 = slot_rect(self.grids[gi], r, c)
                    grabbed = True
                    # Already read this slot and the item icon still looks the same?
                    # Then it's the same pet art, skip it.
                    ix, iy = (x1 - x0) * 0.3, (y1 - y0) * 0.3
                    ishot = sct.grab({"left": int(x0 + ix), "top": int(y0 + iy),
                                      "width": max(2, int(x1 - x0 - 2 * ix)),
                                      "height": max(2, int(y1 - y0 - 2 * iy))})
                    icon = Image.frombytes("RGB", ishot.size, ishot.rgb)
                    old = self.icons.get(cur)
                    same_item = old is not None and old.size == icon.size and \
                        sum(ImageStat.Stat(ImageChops.difference(old, icon)).mean) / 3 < 8
                    if same_item and self.marks.get(cur) in ("good", "bad"):
                        time.sleep(0.004)
                        continue   # same pet art, already fully read
                    self.icons[cur] = icon
                    cx = (x0 + x1) / 2
                    left = int(max(vx, cx - self.cap["width"] / 2))
                    top = int(max(vy, y0 - self.cap["above"]))
                    right = int(min(vx + vw, cx + self.cap["width"] / 2))
                    bottom = int(min(vy + vh, y1 + self.cap["below"]))
                    shot = sct.grab({"left": left, "top": top,
                                     "width": right - left, "height": bottom - top})
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    self.seq += 1
                    self.ocr_q.put((self.seq, cur, img, cx - left, same_item))
                    self.event_q.put(("grabbed", cur))
                time.sleep(0.004)


class OcrWorker(threading.Thread):
    """Reads tooltips with the learned game font (font_reader)."""

    def __init__(self, ocr_q, result_q, reader):
        super().__init__(daemon=True)
        self.ocr_q, self.result_q, self.reader = ocr_q, result_q, reader

    def run(self):
        while True:
            seq, slot, img, cx, same_item = self.ocr_q.get()
            try:
                t = time.perf_counter()
                name, arts, debug = self.reader.read(np.asarray(img), cx)
                ms = (time.perf_counter() - t) * 1000
                self.result_q.put(("result", (seq, slot, name, arts, debug, same_item, ms)))
                try:
                    os.makedirs(DEBUG_DIR, exist_ok=True)
                    base = os.path.join(DEBUG_DIR, f"capture_{seq % 40:02d}")
                    img.save(base + ".png")
                    with open(base + ".json", "w", encoding="utf-8") as fh:
                        json.dump({"seq": seq, "slot": list(slot), "cx": cx, "name": name,
                                   "arts": arts, "lines": debug}, fh, ensure_ascii=False, indent=1)
                except Exception:
                    pass
            except Exception as e:
                self.result_q.put(("error", f"{type(e).__name__}: {e}"))


# ================================================================ overlay
class Overlay:
    """Click-through, always-on-top transparent window that draws slot marks."""

    def __init__(self, root):
        self.vx, self.vy, vw, vh = virtual_screen()
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.geometry(f"{vw}x{vh}+{self.vx}+{self.vy}")
        self.win.configure(bg=KEY_COLOR)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", KEY_COLOR)
        self.cv = tk.Canvas(self.win, bg=KEY_COLOR, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.win.update_idletasks()
        hwnd = user32.GetParent(self.win.winfo_id())
        ex = user32.GetWindowLongW(hwnd, -20)
        # layered | transparent (click-through) | toolwindow (no taskbar entry)
        user32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80)
        # keep the glow / X marks out of screenshots so they never cover tooltip text
        # for the OCR (you still see them on screen). WDA_EXCLUDEFROMCAPTURE = 0x11
        try:
            user32.SetWindowDisplayAffinity(hwnd, 0x11)
        except Exception:
            pass
        self.pulse = False
        self.phase = 0.0
        self.ticks = 0
        self.flash_slot = None
        self.state = ([], {}, False, False)
        self.pages = {}
        self._tick()

    def _tick(self):
        self.ticks += 1
        self.phase = (self.phase + 0.04) % 1.0
        if self.ticks % 6 == 0:
            self.pulse = not self.pulse
        try:
            self.redraw()
            if self.ticks % 15 == 0:
                self.win.attributes("-topmost", True)
        except tk.TclError:
            return
        self.win.after(60, self._tick)

    @staticmethod
    def _hue(h):
        r, g, b = colorsys.hsv_to_rgb(h % 1.0, 1.0, 1.0)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

    def set(self, grids, marks, show_grid, show_scanned, pages):
        self.state = (grids, marks, show_grid, show_scanned)
        self.pages = pages
        self.redraw()

    def flash(self, slot):
        self.flash_slot = slot
        self.redraw()
        self.win.after(2500, lambda: (setattr(self, "flash_slot", None), self.redraw()))

    def redraw(self):
        grids, marks, show_grid, show_scanned = self.state
        cv = self.cv
        cv.delete("all")
        ox, oy = self.vx, self.vy
        if show_grid:
            for g in grids:
                for r in range(g["rows"]):
                    for c in range(g["cols"]):
                        x0, y0, x1, y1 = slot_rect(g, r, c)
                        cv.create_rectangle(x0 - ox, y0 - oy, x1 - ox, y1 - oy, outline="#5b8cff")
            for gi, g in enumerate(grids):
                t = g.get("tabs")
                if not t:
                    continue
                for i in range(t["rows"] * t["cols"]):
                    x0, y0, x1, y1 = tab_rect(t, i)
                    on = self.pages.get(gi, 0) == i
                    cv.create_rectangle(x0 - ox, y0 - oy, x1 - ox, y1 - oy,
                                        outline=GOOD if on else "#e0b040", width=3 if on else 1)
        for (gi, pg, r, c), status in marks.items():
            if gi >= len(grids) or pg != self.pages.get(gi, 0):
                continue
            x0, y0, x1, y1 = slot_rect(grids[gi], r, c)
            x0, y0, x1, y1 = x0 - ox, y0 - oy, x1 - ox, y1 - oy
            if status == "good":
                # animated rainbow: each side a different hue, colours run around the slot
                h = self.phase + (r * 0.07 + c * 0.05)
                for k, pad in enumerate((5, 2, -1)):
                    a0, b0, a1, b1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
                    w = 4 if k < 2 else 2
                    for side, (sx0, sy0, sx1, sy1) in enumerate(
                            ((a0, b0, a1, b0), (a1, b0, a1, b1), (a1, b1, a0, b1), (a0, b1, a0, b0))):
                        cv.create_line(sx0, sy0, sx1, sy1, width=w, capstyle="projecting",
                                       fill=self._hue(h + side * 0.25 + k * 0.12))
            elif status == "incomplete":
                cv.create_line(x0 + 5, y0 + 5, x1 - 5, y1 - 5, fill="#ff2a2a", width=4)
                cv.create_line(x1 - 5, y0 + 5, x0 + 5, y1 - 5, fill="#ff2a2a", width=4)
            elif status == "missed":
                cv.create_oval(x1 - 10, y0 + 3, x1 - 3, y0 + 10, fill=WARN, outline="")
            elif show_scanned and status == "bad":
                cv.create_line(x0 + 4, y1 - 4, x1 - 4, y1 - 4, fill=BAD, width=2)
        if self.flash_slot and self.flash_slot[0] < len(grids):
            gi, pg, r, c = self.flash_slot
            g = grids[gi]
            col = "#ffffff" if self.pulse else ACCENT
            if g.get("tabs") and pg != self.pages.get(gi, 0):
                # item is on another page: flash that page's button instead
                x0, y0, x1, y1 = tab_rect(g["tabs"], pg)
                cv.create_rectangle(x0 - ox - 3, y0 - oy - 3, x1 - ox + 3, y1 - oy + 3, outline=col, width=4)
            else:
                x0, y0, x1, y1 = slot_rect(g, r, c)
                cv.create_rectangle(x0 - ox - 6, y0 - oy - 6, x1 - ox + 6, y1 - oy + 6, outline=col, width=4)


# ================================================================ click picker
def pick_point(root, prompt, callback):
    """Full-screen see-through layer: one click -> callback((x, y)) in screen pixels,
    Esc -> callback(None)."""
    vx, vy, vw, vh = virtual_screen()
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.geometry(f"{vw}x{vh}+{vx}+{vy}")
    top.attributes("-topmost", True)
    top.attributes("-alpha", 0.25)
    top.configure(bg="black")
    cv = tk.Canvas(top, bg="black", highlightthickness=0, cursor="crosshair")
    cv.pack(fill="both", expand=True)
    cv.create_text(vw // 2, 50, fill="white", font=("Segoe UI", 20, "bold"), text=prompt)

    def click(e):
        top.destroy()
        callback((e.x + vx, e.y + vy))

    def cancel(e=None):
        top.destroy()
        callback(None)

    cv.bind("<ButtonPress-1>", click)
    top.bind("<Escape>", cancel)
    top.focus_force()


# ================================================================ region picker
def pick_region(root, prompt, callback):
    vx, vy, vw, vh = virtual_screen()
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.geometry(f"{vw}x{vh}+{vx}+{vy}")
    top.attributes("-topmost", True)
    top.attributes("-alpha", 0.3)
    top.configure(bg="black")
    cv = tk.Canvas(top, bg="black", highlightthickness=0, cursor="crosshair")
    cv.pack(fill="both", expand=True)
    cv.create_text(vw // 2, 50, fill="white", font=("Segoe UI", 20, "bold"), text=prompt)
    st = {}

    def down(e):
        st["x0"], st["y0"] = e.x, e.y
        st["r"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ff88", width=2)

    def move(e):
        if "r" in st:
            cv.coords(st["r"], st["x0"], st["y0"], e.x, e.y)

    def up(e):
        if "x0" not in st:
            return
        x0, y0 = min(st["x0"], e.x), min(st["y0"], e.y)
        x1, y1 = max(st["x0"], e.x), max(st["y0"], e.y)
        top.destroy()
        callback((x0 + vx, y0 + vy, x1 - x0, y1 - y0) if x1 - x0 > 10 and y1 - y0 > 10 else None)

    def cancel(e=None):
        top.destroy()
        callback(None)

    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", up)
    top.bind("<Escape>", cancel)
    top.focus_force()


# ================================================================ app
class App:
    def __init__(self, root):
        self.root = root

        self.ocr_q, self.result_q, self.event_q = queue.Queue(), queue.Queue(), queue.Queue()
        self.scanner = None
        self.grids = []
        self.marks = {}        # (gi, r, c) -> "good" | "bad" | "missed" | "pending" | "empty"
        self.seq_sig = {}      # seq -> (slot, signature) for stale-tooltip detection
        self.pages = {}        # grid index -> open page (0 = I)
        self.icons = {}        # slot -> item icon when it was read (skip re-reading the same item)
        self.slot_arts = {}    # slot -> {bonus: value} gathered over hovers of the same item
        self.rolls = self.goods = 0
        self.keys_prev = {VK_F6: False, VK_F7: False}

        self.vars = {}
        self.min_matches = tk.IntVar(value=1)
        self.delay_ms = tk.IntVar(value=60)
        self.cap_width = tk.IntVar(value=480)
        self.cap_above = tk.IntVar(value=520)
        self.cap_below = tk.IntVar(value=480)
        self.ocr_scale = tk.DoubleVar(value=2.5)
        self.beep = tk.BooleanVar(value=True)
        self.show_grid = tk.BooleanVar(value=False)
        self.show_scanned = tk.BooleanVar(value=True)
        self.only_good = tk.BooleanVar(value=False)
        self.on_top = tk.BooleanVar(value=False)

        self._style()
        self._build()
        self._load()
        self.overlay = Overlay(root)
        self._refresh_overlay()

        self.reader = font_reader.FontReader(
            FONT_PATH, lambda t: parse_lines([fix_ocr(t)]), lambda b: tuple(BONUSES[b]))
        for _ in range(OCR_WORKERS):
            OcrWorker(self.ocr_q, self.result_q, self.reader).start()

        PageWatcher(self).start()
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(30, self._poll)

    # ------------------------------------------------------------ style / layout
    def f(self, size, weight="normal", family="Segoe UI"):
        return ctk.CTkFont(family=family, size=size, weight=weight)

    def _style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("Modern.Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                    rowheight=32, borderwidth=0, relief="flat", font=("Segoe UI", 10))
        s.configure("Modern.Treeview.Heading", background=CARD, foreground=MUTED, relief="flat",
                    borderwidth=0, font=("Segoe UI", 9, "bold"), padding=(8, 8))
        s.map("Modern.Treeview.Heading", background=[("active", CARD2)])
        s.map("Modern.Treeview", background=[("selected", "#2a2350")], foreground=[("selected", FG)])
        s.layout("Modern.Treeview", [("Modern.Treeview.treearea", {"sticky": "nswe"})])

    def _card(self, parent, title=None, subtitle=None):
        c = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        if title:
            c.header = ctk.CTkFrame(c, fg_color="transparent")
            c.header.pack(fill="x", padx=18, pady=(14, 2))
            ctk.CTkLabel(c.header, text=title, font=self.f(15, "bold"), text_color=FG).pack(side="left")
        if subtitle:
            ctk.CTkLabel(c, text=subtitle, font=self.f(12), text_color=MUTED, justify="left",
                         anchor="w", wraplength=760).pack(fill="x", padx=18, pady=(0, 6))
        return c

    def _btn(self, parent, text, cmd, kind="soft", **kw):
        colors = {"accent": (ACCENT, ACCENT_H, "white"), "soft": (CARD2, BORDER, FG),
                  "danger": (CARD2, "#3a2028", BAD), "ghost": ("transparent", CARD2, ACCENT)}[kind]
        kw.setdefault("height", 34)
        kw.setdefault("corner_radius", 10)
        return ctk.CTkButton(parent, text=text, command=cmd, fg_color=colors[0], hover_color=colors[1],
                             text_color=colors[2], font=self.f(13, "bold" if kind == "accent" else "normal"),
                             **kw)

    def _build(self):
        r = self.root
        r.title("Pet Art Filter - Jintea")
        icon = os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "app.ico")
        if os.path.exists(icon):   # CustomTkinter sets its own icon shortly after start
            r.after(300, lambda: r.iconbitmap(icon))
        r.configure(fg_color=BG)
        r.geometry("1280x840")
        r.minsize(1100, 700)
        r.grid_columnconfigure(1, weight=1)
        r.grid_rowconfigure(0, weight=1)

        # ---- sidebar
        side = ctk.CTkFrame(r, width=236, corner_radius=0, fg_color=SIDEBAR)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.pack_propagate(False)
        logo = ctk.CTkFrame(side, fg_color="transparent")
        logo.pack(fill="x", padx=20, pady=(26, 26))
        badge = ctk.CTkLabel(logo, text="◆", width=42, height=42, corner_radius=12,
                             fg_color=ACCENT, text_color="white", font=self.f(20, "bold"))
        badge.pack(side="left")
        tb = ctk.CTkFrame(logo, fg_color="transparent")
        tb.pack(side="left", padx=12)
        ctk.CTkLabel(tb, text="Pet Art Filter", font=self.f(17, "bold"), text_color=FG,
                     height=22, anchor="w").pack(anchor="w")
        ctk.CTkLabel(tb, text="Metin2  ·  Jintea", font=self.f(12), text_color=MUTED,
                     height=16, anchor="w").pack(anchor="w")

        ctk.CTkLabel(side, text="MENU", font=self.f(11, "bold"), text_color=MUTED, anchor="w"
                     ).pack(fill="x", padx=26, pady=(0, 6))
        self.nav = {}
        for key, icon, label in (("scan", "◎", "Scanner"), ("filter", "☰", "Filter"),
                                 ("setup", "⚙", "Setup")):
            b = ctk.CTkButton(side, text=f"{icon}    {label}", anchor="w", height=44, corner_radius=12,
                              fg_color="transparent", hover_color=CARD2, text_color=MUTED,
                              font=self.f(14, "bold"), command=lambda k=key: self._show_page(k))
            b.pack(fill="x", padx=14, pady=3)
            self.nav[key] = b

        bottom = ctk.CTkFrame(side, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=14, pady=18)
        self.scan_btn = ctk.CTkButton(bottom, text="▶   Start scanning", height=50, corner_radius=14,
                                      fg_color=ACCENT, hover_color=ACCENT_H, font=self.f(15, "bold"),
                                      command=self._toggle)
        self.scan_btn.pack(fill="x")
        ctk.CTkLabel(bottom, text="F6  start / stop     F7  clear marks", text_color=MUTED,
                     font=self.f(11)).pack(pady=(6, 10))
        self._btn(bottom, "Clear marks", self._clear_marks).pack(fill="x")
        sw = ctk.CTkFrame(bottom, fg_color="transparent")
        sw.pack(fill="x", pady=(16, 0))
        ctk.CTkSwitch(sw, text="Beep on good", variable=self.beep, progress_color=ACCENT,
                      font=self.f(13), text_color=FG, command=self._save).pack(anchor="w", pady=4)
        ctk.CTkSwitch(sw, text="Always on top", variable=self.on_top, progress_color=ACCENT,
                      font=self.f(13), text_color=FG,
                      command=lambda: self.root.attributes("-topmost", self.on_top.get())
                      ).pack(anchor="w", pady=4)
        self.status = ctk.CTkLabel(bottom, text="Ready", text_color=MUTED, font=self.f(11),
                                   wraplength=200, justify="left", anchor="w")
        self.status.pack(fill="x", pady=(14, 0))

        # ---- pages
        main = ctk.CTkFrame(r, fg_color=BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)
        self.pages_ui = {"scan": self._build_scan(main), "filter": self._build_filter(main),
                         "setup": self._build_setup(main)}
        self._show_page("scan")

    def _show_page(self, key):
        for k, page in self.pages_ui.items():
            if k == key:
                page.grid(row=0, column=0, sticky="nsew")
            else:
                page.grid_forget()
        for k, b in self.nav.items():
            on = k == key
            b.configure(fg_color=CARD2 if on else "transparent", text_color=FG if on else MUTED)

    def _page_title(self, parent, title, subtitle):
        h = ctk.CTkFrame(parent, fg_color="transparent")
        h.pack(fill="x", padx=28, pady=(26, 14))
        ctk.CTkLabel(h, text=title, font=self.f(26, "bold"), text_color=FG, anchor="w").pack(anchor="w")
        ctk.CTkLabel(h, text=subtitle, font=self.f(13), text_color=MUTED, anchor="w").pack(anchor="w")
        return h

    # ------------------------------------------------------------ scanner page
    def _build_scan(self, parent):
        pg = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        self._page_title(pg, "Scanner", "Sweep your mouse over pet arts. Good ones light up in the game.")
        body = ctk.CTkFrame(pg, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        body.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="t")
        body.grid_rowconfigure(3, weight=1)

        self.hero = ctk.CTkFrame(body, fg_color=CARD, corner_radius=18, border_width=2, border_color=BORDER)
        self.hero.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 12))
        self.verdict = ctk.CTkLabel(self.hero, text="Ready", font=self.f(32, "bold"), text_color=MUTED)
        self.verdict.pack(pady=(18, 0))
        self.verdict_sub = ctk.CTkLabel(self.hero, text="Press F6, then hover your pet arts",
                                        font=self.f(13), text_color=MUTED)
        self.verdict_sub.pack(pady=(0, 18))

        self.tiles = {}
        for i, (k, label, col) in enumerate((("scanned", "Scanned", FG), ("good", "Good", GOOD),
                                             ("inc", "Incomplete", BAD), ("speed", "Read time", CYAN))):
            t = ctk.CTkFrame(body, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
            t.grid(row=1, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == 3 else 6), pady=(0, 12))
            v = ctk.CTkLabel(t, text="0" if k != "speed" else "–", font=self.f(30, "bold"),
                             text_color=col, anchor="w")
            v.pack(fill="x", padx=18, pady=(12, 0))
            ctk.CTkLabel(t, text=label.upper(), font=self.f(11, "bold"), text_color=MUTED,
                         anchor="w").pack(fill="x", padx=18, pady=(0, 12))
            self.tiles[k] = v

        mid = ctk.CTkFrame(body, fg_color="transparent")
        mid.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 12))
        mid.grid_columnconfigure(0, weight=3)
        mid.grid_columnconfigure(1, weight=2)
        last = self._card(mid, "Last pet art")
        last.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.cur_name = ctk.CTkLabel(last.header, text="", font=self.f(13, "bold"), text_color=MUST_COL)
        self.cur_name.pack(side="right")
        box = ctk.CTkFrame(last, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=18, pady=(6, 14))
        box.grid_columnconfigure(1, weight=1)
        self.cur_rows = []
        for i in range(ARTS_PER_PET):
            n = ctk.CTkLabel(box, text="", font=self.f(13), text_color=FG, anchor="w", width=230, height=24)
            bar = ctk.CTkProgressBar(box, height=8, corner_radius=4, progress_color=BORDER, fg_color=CARD2)
            bar.set(0)
            v = ctk.CTkLabel(box, text="", font=self.f(13, "bold"), text_color=FG, width=56, anchor="e", height=24)
            st = ctk.CTkLabel(box, text="", font=self.f(11, "bold"), text_color=MUTED, width=82, anchor="e", height=24)
            n.grid(row=i, column=0, sticky="w", pady=2)
            bar.grid(row=i, column=1, sticky="ew", padx=12)
            v.grid(row=i, column=2)
            st.grid(row=i, column=3)
            self.cur_rows.append((n, bar, v, st))
        raw = self._card(mid, "What the app read")
        raw.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.raw = ctk.CTkTextbox(raw, font=self.f(11, family="Consolas"), fg_color=CARD2,
                                  text_color=MUTED, corner_radius=10, height=230, wrap="none", border_width=0)
        self.raw.pack(fill="both", expand=True, padx=14, pady=(6, 14))

        res = self._card(body, "Results")
        res.grid(row=3, column=0, columnspan=4, sticky="nsew")
        ctk.CTkLabel(res.header, text="click a row to flash its slot in the game", font=self.f(12),
                     text_color=MUTED).pack(side="left", padx=12)
        self._btn(res.header, "Clear list", self._clear_hist, width=96, height=30).pack(side="right")
        ctk.CTkSwitch(res.header, text="Only good", variable=self.only_good, progress_color=ACCENT,
                      font=self.f(12), text_color=FG).pack(side="right", padx=14)
        tf = ctk.CTkFrame(res, fg_color=CARD, corner_radius=0)
        tf.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        self.hist = ttk.Treeview(tf, columns=("slot", "name", "v", "b"), show="headings",
                                 style="Modern.Treeview")
        for col, txt, w in [("slot", "SLOT", 130), ("name", "PET ART", 150), ("v", "RESULT", 80),
                            ("b", "BONUSES", 560)]:
            self.hist.heading(col, text=txt, anchor="w")
            self.hist.column(col, width=w, anchor="w", stretch=(col == "b"))
        self.hist.tag_configure("good", foreground=GOOD)
        self.hist.tag_configure("bad", foreground=MUTED)
        self.hist.tag_configure("incomplete", foreground=BAD)
        self.hist.bind("<<TreeviewSelect>>", self._hist_select)
        sb = ctk.CTkScrollbar(tf, command=self.hist.yview, button_color=CARD2, button_hover_color=BORDER)
        self.hist.configure(yscrollcommand=sb.set)
        self.hist.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.hist_slots = {}
        return pg

    def _set_status(self, text, color=MUTED):
        self.status.configure(text=text, text_color=color)

    def _set_verdict(self, text, color, sub=""):
        self.verdict.configure(text=text, text_color=color)
        self.verdict_sub.configure(text=sub)
        self.hero.configure(border_color=BORDER if color == MUTED else color)

    def _set_scanning(self, on):
        if on:
            self.scan_btn.configure(text="■   Stop scanning", fg_color=BAD, hover_color="#e25c5c")
            self._set_verdict("Scanning…", ACCENT, "Hover your pet arts – good ones glow in the game")
        else:
            self.scan_btn.configure(text="▶   Start scanning", fg_color=ACCENT, hover_color=ACCENT_H)
            self._set_verdict("Ready", MUTED, "Press F6, then hover your pet arts")

    def _update_stats(self):
        marks = list(self.marks.values())
        self.tiles["scanned"].configure(text=str(len(self.hist_slots)))
        self.tiles["good"].configure(text=str(marks.count("good")))
        self.tiles["inc"].configure(text=str(marks.count("incomplete")))
        ms = getattr(self, "last_ms", None)
        self.tiles["speed"].configure(text=f"{ms:.0f} ms" if ms else "–")

    def _show_current(self, name, rows):
        self.cur_name.configure(text=name)
        colors = {"hit": GOOD, "miss": BAD, "warn": WARN}
        for i, (n, bar, v, st) in enumerate(self.cur_rows):
            if i < len(rows):
                bonus, val, pct, status, tag = rows[i]
                col = colors.get(tag, "#3b4459")
                n.configure(text=bonus, text_color=FG if tag else MUTED)
                bar.configure(progress_color=col)
                bar.set(min(1.0, val / BONUSES[bonus][1]))
                v.configure(text=str(val), text_color=FG if tag else MUTED)
                st.configure(text="" if status == "-" else status.upper(), text_color=col)
            else:
                n.configure(text="")
                bar.set(0)
                v.configure(text="")
                st.configure(text="")

    # ------------------------------------------------------------ filter page
    def _build_filter(self, parent):
        for name, (lo, hi) in BONUSES.items():
            self.vars[name] = (tk.StringVar(value="Ignore"), tk.IntVar(value=lo))
        self.chips = {}

        pg = ctk.CTkScrollableFrame(parent, fg_color=BG, corner_radius=0,
                                    scrollbar_button_color=CARD2, scrollbar_button_hover_color=BORDER)
        self._page_title(pg, "Filter", "Pick the bonuses you want. Pet arts that match glow in the game.")

        rc = self._card(pg)
        rc.pack(fill="x", padx=28, pady=(0, 18))
        row = ctk.CTkFrame(rc, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(row, text="Glow a pet art when it has at least", font=self.f(16), text_color=FG
                     ).pack(side="left")
        self._btn(row, "−", lambda: self._bump_min(-1), width=38, height=38).pack(side="left", padx=(14, 0))
        ctk.CTkLabel(row, textvariable=self.min_matches, font=self.f(28, "bold"), text_color=GOOD,
                     width=54).pack(side="left")
        self._btn(row, "+", lambda: self._bump_min(1), width=38, height=38).pack(side="left")
        ctk.CTkLabel(row, text="of my bonuses", font=self.f(16), text_color=FG).pack(side="left", padx=14)
        self.rule_hint = ctk.CTkLabel(rc, text="", font=self.f(12), text_color=MUTED, anchor="w",
                                      justify="left", wraplength=800)
        self.rule_hint.pack(fill="x", padx=20, pady=(0, 16))

        head = ctk.CTkFrame(pg, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(0, 8))
        ctk.CTkLabel(head, text="My bonuses", font=self.f(18, "bold"), text_color=FG).pack(side="left")
        self._btn(head, "Clear all", self._reset, kind="danger", width=96, height=30).pack(side="right")
        self._btn(head, "Re-check scanned", self._recheck, kind="ghost", width=140, height=30
                  ).pack(side="right", padx=8)
        self.sel_frame = ctk.CTkFrame(pg, fg_color="transparent")
        self.sel_frame.pack(fill="x", padx=28)
        self.sel_frame.grid_columnconfigure((0, 1), weight=1, uniform="s")

        head2 = ctk.CTkFrame(pg, fg_color="transparent")
        head2.pack(fill="x", padx=28, pady=(24, 4))
        ctk.CTkLabel(head2, text="Add bonuses", font=self.f(18, "bold"), text_color=FG).pack(side="left")
        ctk.CTkLabel(head2, text="click = WANT      right-click = MUST (always required)",
                     font=self.f(12), text_color=MUTED).pack(side="right")
        self.search = ctk.CTkEntry(pg, placeholder_text="\U0001F50D  Search bonuses…", height=40,
                                   corner_radius=12, fg_color=CARD, border_color=BORDER, text_color=FG,
                                   font=self.f(13))
        self.search.pack(fill="x", padx=28, pady=(6, 6))
        self.search.bind("<KeyRelease>", lambda e: self._render_chips(rebuild=True))
        self.chip_frame = ctk.CTkFrame(pg, fg_color="transparent")
        self.chip_frame.pack(fill="x", padx=28, pady=(0, 28))
        return pg

    def _bump_min(self, d):
        self.min_matches.set(max(0, min(7, self._int(self.min_matches, 1) + d)))
        self._filter_changed()

    def _set_mode(self, name, mode):
        self.vars[name][0].set(mode)
        self._filter_changed()

    def _filter_changed(self):
        self._save()
        self._render_selected()
        self._render_chips()

    def _render_selected(self):
        f = self.sel_frame
        for w in f.winfo_children():
            w.destroy()
        chosen = [n for n in BONUSES if self.vars[n][0].get() != "Ignore"]
        musts = sum(1 for n in chosen if self.vars[n][0].get() == "Must")
        need = self._int(self.min_matches, 1)
        if not chosen:
            e = ctk.CTkFrame(f, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
            e.grid(row=0, column=0, columnspan=2, sticky="ew")
            ctk.CTkLabel(e, text="Nothing picked yet – add bonuses below.", font=self.f(13),
                         text_color=MUTED).pack(pady=20)
            self.rule_hint.configure(text="Pick at least one bonus, otherwise nothing will glow.",
                                     text_color=WARN)
            return
        hint = (f"A bonus counts when its value is at or above the minimum you set. "
                f"{len(chosen)} picked")
        if musts:
            hint += f", {musts} marked MUST (always required)"
        if need > len(chosen):
            self.rule_hint.configure(text=hint + f". You need {need} but only picked {len(chosen)}, "
                                                 "so nothing can glow.", text_color=WARN)
        else:
            self.rule_hint.configure(text=hint + ".", text_color=MUTED)
        for i, name in enumerate(chosen):
            mode, mn = self.vars[name]
            lo, hi = BONUSES[name]
            must = mode.get() == "Must"
            col = MUST_COL if must else ACCENT
            card = ctk.CTkFrame(f, fg_color=CARD, corner_radius=14, border_width=1,
                                border_color=col if must else BORDER)
            card.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else 6, 6 if i % 2 == 0 else 0),
                      pady=6)
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=16, pady=(12, 0))
            ctk.CTkLabel(top, text=name, font=self.f(14, "bold"), text_color=FG).pack(side="left")
            ctk.CTkButton(top, text="✕", width=28, height=28, corner_radius=8, fg_color="transparent",
                          hover_color="#3a2028", text_color=MUTED, font=self.f(13),
                          command=lambda n=name: self._set_mode(n, "Ignore")).pack(side="right")
            seg = ctk.CTkSegmentedButton(top, values=["Want", "Must"], height=26, font=self.f(11, "bold"),
                                         selected_color=col, selected_hover_color=col,
                                         unselected_color=CARD2, unselected_hover_color=BORDER,
                                         fg_color=CARD2, text_color=FG,
                                         command=lambda v, n=name: self._set_mode(n, v))
            seg.set("Must" if must else "Want")
            seg.pack(side="right", padx=8)
            bot = ctk.CTkFrame(card, fg_color="transparent")
            bot.pack(fill="x", padx=16, pady=(8, 14))
            step = 50 if hi >= 1000 else 10 if hi >= 200 else 5 if hi >= 100 else 1
            val = ctk.CTkLabel(bot, text=f"≥ {mn.get()}", font=self.f(14, "bold"), text_color=GOOD,
                               width=66, anchor="w")
            val.pack(side="left")

            def moved(v, var=mn, lbl=val, st=step, lo_=lo):
                iv = int(round((v - lo_) / st) * st + lo_)
                var.set(iv)
                lbl.configure(text=f"≥ {iv}")

            sl = ctk.CTkSlider(bot, from_=lo, to=hi, number_of_steps=max(1, (hi - lo) // step),
                               progress_color=col, button_color=col, button_hover_color=FG,
                               fg_color=CARD2, height=16, command=moved)
            sl.set(mn.get())
            sl.pack(side="left", fill="x", expand=True, padx=8)
            sl.bind("<ButtonRelease-1>", lambda e: self._save())
            ctk.CTkLabel(bot, text=f"max {hi}", font=self.f(12), text_color=MUTED).pack(side="left")

    def _chip_style(self, chip, name):
        mode = self.vars[name][0].get()
        label = name.replace("Strong against ", "vs ")
        if mode == "Want":
            chip.configure(text="✓  " + label, fg_color=ACCENT, hover_color=ACCENT_H,
                           text_color="white", border_color=ACCENT)
        elif mode == "Must":
            chip.configure(text="★  " + label, fg_color=MUST_COL, hover_color="#e09c2c",
                           text_color="#1b1206", border_color=MUST_COL)
        else:
            chip.configure(text="+  " + label, fg_color=CARD, hover_color=CARD2, text_color=FG,
                           border_color=BORDER)

    def _render_chips(self, rebuild=False):
        if self.chips and not rebuild:
            for name, chip in self.chips.items():
                self._chip_style(chip, name)
            return
        f = self.chip_frame
        for w in f.winfo_children():
            w.destroy()
        self.chips = {}
        q = self.search.get().strip().lower()
        for cat, names in CATEGORIES:
            shown = [n for n in names if q in n.lower() or q in cat.lower()]
            if not shown:
                continue
            ctk.CTkLabel(f, text=cat.upper(), font=self.f(11, "bold"), text_color=MUTED, anchor="w"
                         ).pack(fill="x", pady=(14, 6))
            grid = ctk.CTkFrame(f, fg_color="transparent")
            grid.pack(fill="x")
            grid.grid_columnconfigure((0, 1, 2), weight=1, uniform="c")
            for i, name in enumerate(shown):
                chip = ctk.CTkButton(grid, text="", height=34, corner_radius=17, border_width=1,
                                     anchor="w", font=self.f(12),
                                     command=lambda n=name: self._set_mode(
                                         n, "Ignore" if self.vars[n][0].get() != "Ignore" else "Want"))
                chip.bind("<Button-3>", lambda e, n=name: self._set_mode(
                    n, "Ignore" if self.vars[n][0].get() == "Must" else "Must"))
                chip.grid(row=i // 3, column=i % 3, sticky="ew", padx=3, pady=3)
                self._chip_style(chip, name)
                self.chips[name] = chip

    # ------------------------------------------------------------ setup page
    def _build_setup(self, parent):
        pg = ctk.CTkScrollableFrame(parent, fg_color=BG, corner_radius=0,
                                    scrollbar_button_color=CARD2, scrollbar_button_hover_color=BORDER)
        self._page_title(pg, "Setup", "Tell the app where your stash and inventory slots are.")

        g = self._card(pg, "Slot grids", "Open the inventory or stash in the game, click “Find grid with one "
                       "click” and click any slot. The app finds the slots and the page buttons by itself "
                       "and shows the grid lines so you can check them. If it can’t, use “Draw "
                       "manually” and drag from the first slot’s top-left to the last slot’s "
                       "bottom-right corner.")
        g.pack(fill="x", padx=28, pady=(0, 16))
        lb = ctk.CTkFrame(g, fg_color=CARD2, corner_radius=12)
        lb.pack(fill="x", padx=18, pady=(6, 10))
        self.grid_list = tk.Listbox(lb, height=4, bg=CARD2, fg=FG, relief="flat", bd=0,
                                    selectbackground="#2a2350", selectforeground=FG, highlightthickness=0,
                                    font=("Segoe UI", 11), activestyle="none")
        self.grid_list.pack(fill="x", padx=12, pady=10)
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(0, 8))
        self._btn(row, "◎  Find grid with one click", self._auto_grid, kind="accent", width=220
                  ).pack(side="left")
        self._btn(row, "Draw manually", self._add_grid, width=120).pack(side="left", padx=8)
        self._btn(row, "Redo selected", self._redo_grid, width=120).pack(side="left", padx=(0, 8))
        self._btn(row, "Remove selected", self._remove_grid, kind="danger", width=140).pack(side="left")
        row2 = ctk.CTkFrame(g, fg_color="transparent")
        row2.pack(fill="x", padx=18, pady=(0, 6))
        self._btn(row2, "Set page buttons (I, II, III…)", self._set_tabs, width=230).pack(side="left")
        self._btn(row2, "No pages", self._clear_tabs, width=100).pack(side="left", padx=8)
        ctk.CTkLabel(g, text="Pages: select a grid, click “Set page buttons” and drag around ALL the "
                     "page buttons (inventory I–X, stash I–XV). The glow is then kept per page.",
                     font=self.f(12), text_color=MUTED, anchor="w", justify="left", wraplength=760
                     ).pack(fill="x", padx=18, pady=(4, 8))
        sw = ctk.CTkFrame(g, fg_color="transparent")
        sw.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkSwitch(sw, text="Show grid lines", variable=self.show_grid, progress_color=ACCENT,
                      font=self.f(13), text_color=FG, command=self._refresh_overlay).pack(side="left")
        ctk.CTkSwitch(sw, text="Red line under scanned bad slots", variable=self.show_scanned,
                      progress_color=ACCENT, font=self.f(13), text_color=FG,
                      command=self._refresh_overlay).pack(side="left", padx=24)

        sp = self._card(pg, "Speed & tooltip area", "Tooltip delay is how long the mouse must rest on a slot "
                        "before it’s grabbed. Lower is faster; if it’s too low you get yellow dots "
                        "(missed slots) – just hover those again.")
        sp.pack(fill="x", padx=28, pady=(0, 16))
        grid = ctk.CTkFrame(sp, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=(6, 16))
        grid.grid_columnconfigure(1, weight=1)
        for i, (label, var, lo, hi, step, unit) in enumerate([
                ("Tooltip delay", self.delay_ms, 15, 500, 5, "ms"),
                ("Capture width", self.cap_width, 200, 1200, 20, "px"),
                ("Capture above slot", self.cap_above, 0, 1400, 20, "px"),
                ("Capture below slot", self.cap_below, 0, 1400, 20, "px")]):
            ctk.CTkLabel(grid, text=label, font=self.f(13), text_color=FG, anchor="w", width=170
                         ).grid(row=i, column=0, sticky="w", pady=6)
            val = ctk.CTkLabel(grid, text=f"{var.get()} {unit}", font=self.f(13, "bold"), text_color=CYAN,
                               width=80, anchor="e")
            val.grid(row=i, column=2, sticky="e")

            def moved(v, var=var, lbl=val, st=step, lo_=lo, u=unit):
                iv = int(round((v - lo_) / st) * st + lo_)
                var.set(iv)
                lbl.configure(text=f"{iv} {u}")

            sl = ctk.CTkSlider(grid, from_=lo, to=hi, number_of_steps=(hi - lo) // step, command=moved,
                               progress_color=ACCENT, button_color=ACCENT, button_hover_color=FG,
                               fg_color=CARD2, height=16)
            sl.set(var.get())
            sl.grid(row=i, column=1, sticky="ew", padx=14)
            sl.bind("<ButtonRelease-1>", lambda e: self._save())
            self.setup_sliders = getattr(self, "setup_sliders", []) + [(sl, var, val, unit)]

        lc = self._card(pg, "Font memory", "The app learns your game’s font while you hover. "
                        "New words are read once by the slower teacher, then recognised instantly.")
        lc.pack(fill="x", padx=28, pady=(0, 16))
        self.learn_lbl = ctk.CTkLabel(lc, text="", font=self.f(13), text_color=FG, anchor="w", justify="left")
        self.learn_lbl.pack(fill="x", padx=18, pady=(4, 16))

        note = self._card(pg)
        note.pack(fill="x", padx=28, pady=(0, 28))
        ctk.CTkLabel(note, text="ⓘ  Run the game in windowed or borderless-windowed mode so the glow can be "
                     "drawn on top of it.", font=self.f(12), text_color=MUTED, anchor="w"
                     ).pack(fill="x", padx=18, pady=14)
        return pg

    # ------------------------------------------------------------ config
    def _load(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {}
        for name, d in cfg.get("bonuses", {}).items():
            name = {"Chance of piercing Hits": "Chance for piercing Hits"}.get(name, name)
            if name in self.vars:
                self.vars[name][0].set(d.get("mode", "Ignore"))
                self.vars[name][1].set(d.get("min", BONUSES[name][0]))
        for key, var in self._settings().items():
            if key in cfg:
                var.set(cfg[key])
        self.grids = cfg.get("grids", [])
        for sl, var, lbl, unit in getattr(self, "setup_sliders", []):
            sl.set(var.get())
            lbl.configure(text=f"{var.get()} {unit}")
        self._refresh_grid_list()
        self._render_selected()
        self._render_chips()

    def _settings(self):
        return {"min_matches": self.min_matches, "delay_ms": self.delay_ms,
                "cap_width": self.cap_width, "cap_above": self.cap_above,
                "cap_below": self.cap_below, "ocr_scale": self.ocr_scale,
                "beep": self.beep, "show_scanned": self.show_scanned}

    def _save(self):
        cfg = {}
        for key, var in self._settings().items():
            try:
                cfg[key] = var.get()
            except Exception:
                pass
        cfg["grids"] = self.grids
        cfg["bonuses"] = {n: {"mode": m.get(), "min": self._int(v, BONUSES[n][0])}
                          for n, (m, v) in self.vars.items()}
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
        except Exception:
            pass

    @staticmethod
    def _int(var, default):
        try:
            return int(var.get())
        except Exception:
            return default

    def _reset(self):
        for m, _ in self.vars.values():
            m.set("Ignore")
        self._filter_changed()

    def _set_mins(self, frac):
        for n, (_, v) in self.vars.items():
            lo, hi = BONUSES[n]
            v.set(max(lo, round(hi * frac)))
        self._save()

    # ------------------------------------------------------------ grids
    def _refresh_grid_list(self):
        sel = self.grid_list.curselection()
        self.grid_list.delete(0, "end")
        for gi, g in enumerate(self.grids):
            t = g.get("tabs")
            pages = (f"   |  {t['rows'] * t['cols']} pages, open: {roman(self.pages.get(gi, 0))}"
                     if t else "   |  no pages")
            self.grid_list.insert("end", f"{g['name']}  -  {g['rows']}x{g['cols']} slots{pages}")
        for i in sel:
            if i < len(self.grids):
                self.grid_list.selection_set(i)

    def _add_grid(self, replace_idx=None):
        self.root.withdraw()
        self.overlay.win.withdraw()

        def done(rect):
            self.root.deiconify()
            self.overlay.win.deiconify()
            if not rect:
                return
            old = self.grids[replace_idx] if replace_idx is not None else None
            name = simpledialog.askstring("Grid name", "Name (e.g. Stash, Inventory):",
                                          initialvalue=old["name"] if old else
                                          ("Stash" if not self.grids else "Inventory"),
                                          parent=self.root)
            if name is None:
                return
            rows = simpledialog.askinteger("Rows", "How many rows of slots?", parent=self.root,
                                           initialvalue=old["rows"] if old else 10, minvalue=1, maxvalue=50)
            cols = simpledialog.askinteger("Columns", "How many columns of slots?", parent=self.root,
                                           initialvalue=old["cols"] if old else 18, minvalue=1, maxvalue=50)
            if not rows or not cols:
                return
            g = {"name": name, "x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3],
                 "rows": rows, "cols": cols}
            if old and old.get("tabs"):
                g["tabs"] = old["tabs"]
            if replace_idx is not None:
                self.grids[replace_idx] = g
            else:
                self.grids.append(g)
            self.show_grid.set(True)
            self._clear_marks()
            self._refresh_grid_list()
            self._save()

        self.root.after(250, lambda: pick_region(
            self.root, "Drag from top-left of first slot to bottom-right of last slot  (Esc = cancel)", done))

    def _auto_grid(self):
        self.root.withdraw()
        self.overlay.win.withdraw()

        def shot_then_pick():
            with MSS() as sct:
                m = sct.monitors[0]
                g = sct.grab(m)
            gray = np.asarray(Image.frombytes("RGB", g.size, g.rgb).convert("L"))
            pick_point(self.root, "Click any slot of your inventory or stash  (Esc = cancel)",
                       lambda pt: self._auto_grid_done(pt, gray, m["left"], m["top"]))

        self.root.after(300, shot_then_pick)

    def _auto_grid_done(self, pt, gray, vx, vy):
        self.root.deiconify()
        self.overlay.win.deiconify()
        if pt is None:
            return
        found = grid_finder.find(gray, pt[0] - vx, pt[1] - vy)
        if not found:
            self._set_status("Couldn’t find a slot grid there – click right on a slot "
                             "and try again, or use “Draw manually”.", WARN)
            return
        if not found["tabs"]:
            # stash: pages are a list of boxes left of the grid instead of buttons above it
            found["tabs"] = grid_finder.find_side_tabs(gray, found)
        # stash: page list on the left (1 column of boxes) or a wide grid; else inventory
        t = found["tabs"]
        is_stash = (t is not None and t["cols"] == 1 and t["rows"] > 2) or found["cols"] > 6
        name = "Stash" if is_stash else "Inventory"
        g = {"name": name, "x": found["x"] + vx, "y": found["y"] + vy, "w": found["w"], "h": found["h"],
             "rows": found["rows"], "cols": found["cols"]}
        if found["tabs"]:
            t = found["tabs"]
            g["tabs"] = {"x": t["x"] + vx, "y": t["y"] + vy, "w": t["w"], "h": t["h"],
                         "rows": t["rows"], "cols": t["cols"]}
        # replace the previous inventory / stash (also older hand-named ones like "inv")
        same = [i for i, old in enumerate(self.grids) if old["name"].lower()[:3] == name.lower()[:3]]
        if same:
            if not g.get("tabs") and self.grids[same[0]].get("tabs"):
                g["tabs"] = self.grids[same[0]]["tabs"]   # keep manually set page buttons
            self.grids[same[0]] = g
        else:
            self.grids.append(g)
        self.pages.clear()
        self.show_grid.set(True)
        self._clear_marks()
        self._refresh_grid_list()
        self._refresh_overlay()
        self._save()
        pages = f" + {t['rows'] * t['cols']} pages" if t else ""
        self._set_status(f"✓ {name} found: {found['rows']}×{found['cols']} slots{pages}. "
                         "Check the blue lines sit on the slots.", GOOD)

    def _set_tabs(self):
        sel = self.grid_list.curselection()
        if not sel:
            self._set_status("Select a grid in the list first", WARN)
            return
        gi = sel[0]
        g = self.grids[gi]
        self.root.withdraw()
        self.overlay.win.withdraw()

        def done(rect):
            self.root.deiconify()
            self.overlay.win.deiconify()
            if not rect:
                return
            stash = "stash" in g["name"].lower()
            old = g.get("tabs") or {}
            rows = simpledialog.askinteger(
                "Page buttons", "How many ROWS of page buttons?\n(inventory: 2, stash list: 15)",
                parent=self.root, initialvalue=old.get("rows", 15 if stash else 2), minvalue=1, maxvalue=20)
            cols = simpledialog.askinteger(
                "Page buttons", "How many COLUMNS of page buttons?\n(inventory: 5, stash list: 1)",
                parent=self.root, initialvalue=old.get("cols", 1 if stash else 5), minvalue=1, maxvalue=20)
            if not rows or not cols:
                return
            g["tabs"] = {"x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3], "rows": rows, "cols": cols}
            self.pages.pop(gi, None)
            self.show_grid.set(True)
            self._refresh_grid_list()
            self._refresh_overlay()
            self._save()

        self.root.after(250, lambda: pick_region(
            self.root, "Drag a box around ALL the page buttons (I, II, III ...)  (Esc = cancel)", done))

    def _clear_tabs(self):
        sel = self.grid_list.curselection()
        if sel and self.grids[sel[0]].pop("tabs", None):
            self.pages.pop(sel[0], None)
            self._clear_marks()
            self._refresh_grid_list()
            self._save()

    def _redo_grid(self):
        sel = self.grid_list.curselection()
        if sel:
            self._add_grid(sel[0])

    def _remove_grid(self):
        sel = self.grid_list.curselection()
        if sel:
            del self.grids[sel[0]]
            self.pages.clear()
            self._clear_marks()
            self._refresh_grid_list()
            self._save()

    def _refresh_overlay(self):
        if hasattr(self, "overlay"):
            self.overlay.set(self.grids, self.marks, self.show_grid.get(), self.show_scanned.get(),
                             self.pages)

    # ------------------------------------------------------------ scanning
    def _toggle(self):
        self._stop() if self.scanner else self._start()

    def _start(self):
        if not self.grids:
            self._set_status("Add a grid first on the Setup page", WARN)
            self._show_page("setup")
            return
        self._save()
        self.show_grid.set(False)
        self._refresh_overlay()
        cap = {"width": self._int(self.cap_width, 480), "above": self._int(self.cap_above, 520),
               "below": self._int(self.cap_below, 480)}
        self.scanner = Scanner(self.grids, self.pages, self.icons, self.marks, cap, self._int(self.delay_ms, 60), self.ocr_q, self.event_q)
        self.scanner.start()
        self._set_scanning(True)

    def _stop(self):
        if self.scanner:
            self.scanner.stop()
            self.scanner = None
        self._set_scanning(False)

    def _clear_marks(self):
        self.marks.clear()
        self.seq_sig.clear()
        self.icons.clear()
        self.slot_arts.clear()
        self._refresh_overlay()
        if hasattr(self, "tiles"):
            self._update_stats()

    def _close(self):
        self._save()
        self._stop()
        self.root.destroy()

    # ------------------------------------------------------------ evaluation
    def evaluate(self, found):
        present = dict(found)
        must_ok = all(present.get(n, -1) >= self._int(mn, 0)
                      for n, (mode, mn) in self.vars.items() if mode.get() == "Must")
        rows, hits = [], 0
        for bonus, val in found:
            mode = self.vars[bonus][0].get()
            mn = self._int(self.vars[bonus][1], 0)
            lo, hi = BONUSES[bonus]
            pct = f"{round(val / hi * 100)}%"
            if mode != "Ignore" and val >= mn:
                hits += 1
            if val > hi or val < lo:
                status, tag = "odd value?", "warn"
            elif mode == "Ignore":
                status, tag = "-", ""
            elif val >= mn:
                status, tag = f"{mode} OK", "hit"
            else:
                status, tag = f"< {mn}", "miss"
            rows.append((bonus, val, pct, status, tag))
        wanted_any = any(m.get() != "Ignore" for m, _ in self.vars.values())
        return wanted_any and must_ok and hits >= self._int(self.min_matches, 0), rows

    def _slot_name(self, slot):
        gi, pg, r, c = slot
        if gi >= len(self.grids):
            return "?"
        g = self.grids[gi]
        page = f" {roman(pg)}" if g.get("tabs") else ""
        return f"{g['name']}{page}  r{r + 1} c{c + 1}"

    def _poll(self):
        # hotkeys
        for vk, fn in ((VK_F6, self._toggle), (VK_F7, self._clear_marks)):
            down = key_down(vk)
            if down and not self.keys_prev[vk]:
                fn()
            self.keys_prev[vk] = down

        changed = False
        while True:
            try:
                kind, slot = self.event_q.get_nowait()
            except queue.Empty:
                break
            if kind == "missed" and self.marks.get(slot) not in ("good", "bad"):
                self.marks[slot] = "missed"
                changed = True
            elif kind == "page":
                changed = True
                self._refresh_grid_list()
        while True:
            try:
                kind, data = self.result_q.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                self.raw.delete("1.0", "end")
                self.raw.insert("end", "ERROR: " + data)
                self._set_status("Error while reading – see “What the app read”", BAD)
            else:
                self._on_result(*data)
                changed = True
        if changed:
            self._refresh_overlay()
        self._tick = getattr(self, "_tick", 0) + 1
        if self._tick % 10 == 0:   # ~3x a second is plenty for the status texts
            pending = self.ocr_q.qsize()
            names, values = self.reader.known()
            if self.scanner or pending:
                self._set_status(f"{pending} waiting to be read" if pending else "Scanning…")
            self.learn_lbl.configure(
                text=f"Bonus names learned:  {names} / 41\nValues learned:  {values}\n"
                     f"Lines read by the teacher this session:  {self.reader.stats['taught']}\n"
                     f"Lines read from memory this session:  {self.reader.stats['fast']}")
        self.root.after(30, self._poll)

    def _on_result(self, seq, slot, name, found, lines, same_item, ms):
        self.read_times = (getattr(self, "read_times", []) + [ms])[-10:]
        self.last_ms = sum(self.read_times) / len(self.read_times)
        sig = tuple(found)
        self.seq_sig[seq] = (slot, sig)
        if not found:
            if same_item and self.slot_arts.get(slot):
                return  # tooltip wasn't up this time; keep what we already read
            self.marks.pop(slot, None)  # empty slot / no tooltip
            self.icons.pop(slot, None)
            return
        # Same text as the slot hovered just before = the old tooltip was still on
        # screen. Mark it as missed so it gets swept again.
        prev = self.seq_sig.get(seq - 1)
        if prev and prev[1] == sig and prev[0] != slot:
            self.marks[slot] = "missed"
            self.icons.pop(slot, None)
            return
        nxt = self.seq_sig.get(seq + 1)
        if nxt and nxt[1] == sig and nxt[0] != slot:
            self.marks[nxt[0]] = "missed"
            self.icons.pop(nxt[0], None)

        # hovering the same item again adds to what was read before instead of replacing it
        got = self.slot_arts.get(slot, {}) if same_item else {}
        for b, v in found:
            got.setdefault(b, v)
        self.slot_arts[slot] = got
        found = list(got.items())

        pretty = {"#plvl": "From PLvl …", "#defense": "Defense against Magic Power (base)",
                  "#petart": "[Pet Artifact]", "#wearable": "[Wearable]", "?": "✖ unreadable line"}
        self.raw.delete("1.0", "end")
        self.raw.insert("end", "\n".join(pretty.get(l, l) for l in lines))
        good, rows = self.evaluate(found)
        complete = len(found) >= ARTS_PER_PET
        if complete:
            self.marks[slot] = "good" if good else "bad"
        else:
            # some bonuses weren't read: don't trust it, red X, read it again next hover
            good = False
            self.marks[slot] = "incomplete"

        self._show_current(name, rows)
        where = self._slot_name(slot)
        if complete:
            self._set_verdict("✦  Good pet art" if good else "Not good", GOOD if good else BAD,
                              f"{name}  ·  {where}")
        else:
            self._set_verdict(f"Incomplete  {len(found)}/{ARTS_PER_PET}", WARN,
                              f"{name}  ·  {where}  ·  hover it again")

        # one list row per slot (rescans replace it)
        for iid, s in list(self.hist_slots.items()):
            if s == slot:
                self.hist.delete(iid)
                del self.hist_slots[iid]
        if good or not self.only_good.get():
            result = ("GOOD" if good else "no") if complete else f"X {len(found)}/{ARTS_PER_PET}"
            tag = ("good" if good else "bad") if complete else "incomplete"
            iid = self.hist.insert("", 0, values=(self._slot_name(slot), name, result,
                                                  ",  ".join(f"{b} {v}" for b, v in found)),
                                   tags=(tag,))
            self.hist_slots[iid] = slot
        self._update_stats()
        if good and self.beep.get() and winsound:
            threading.Thread(target=lambda: winsound.Beep(1500, 120), daemon=True).start()

    def _recheck(self):
        """Re-apply the filter to everything already scanned (after changing the filter)."""
        self._save()
        latest = {}
        for seq in sorted(self.seq_sig):
            slot, sig = self.seq_sig[seq]
            latest[slot] = sig
        for slot, sig in latest.items():
            if sig and self.marks.get(slot) in ("good", "bad"):
                good, _ = self.evaluate(list(sig))
                self.marks[slot] = "good" if good else "bad"
        for iid, slot in self.hist_slots.items():
            good = self.marks.get(slot) == "good"
            self.hist.set(iid, "v", "GOOD" if good else "no")
            self.hist.item(iid, tags=("good" if good else "bad",))
        self._update_stats()
        self._refresh_overlay()

    def _hist_select(self, _e):
        sel = self.hist.selection()
        if sel and sel[0] in self.hist_slots:
            self.overlay.flash(self.hist_slots[sel[0]])

    def _clear_hist(self):
        self.hist.delete(*self.hist.get_children())
        self.hist_slots.clear()
        self._update_stats()


def _selftest():
    """`Pet Art Filter.exe --selftest`: checks the packaged app can read text with the
    teacher; writes the result to selftest.txt in the data folder."""
    from PIL import ImageDraw, ImageFont
    img = Image.new("RGB", (220, 16), (12, 12, 16))
    try:
        fnt = ImageFont.truetype("tahoma.ttf", 12)
    except Exception:
        fnt = ImageFont.load_default()
    ImageDraw.Draw(img).text((4, 1), "Attack Value +40", fill=(225, 250, 225), font=fnt)
    r = font_reader.FontReader(os.path.join(APP_DIR, "selftest.pkl"), lambda t: [], lambda b: (0, 1))
    try:
        res = "teacher read: " + repr(r._teach_text(np.asarray(img)))
    except Exception as e:
        res = f"teacher FAILED: {type(e).__name__}: {e}"
    with open(os.path.join(APP_DIR, "selftest.txt"), "w", encoding="utf-8") as fh:
        fh.write(res)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    App(root)
    root.mainloop()
