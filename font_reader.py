"""
Font reader for Metin2 pet art tooltips.

Instead of general OCR, this learns what the game's own font looks like:
  * text is separated from the icons behind it by colour (every tooltip colour keeps
    its tint at every brightness, the icons don't),
  * lines sit on a fixed pixel grid, words are split on the spaces,
  * each bonus name is remembered as a picture, values as whole pictures ("+9%") and
    as single glyphs (0-9 + % ,) for values not seen yet,
  * reading is pixel matching against those pictures: a few milliseconds.

Anything it isn't sure about is read once by RapidOCR (the "teacher") and learned,
so the more pet arts you hover the less the teacher is needed.
"""
import hashlib
import os
import pickle
import re
import threading

import numpy as np

TINTS = [(225, 250, 225), (176, 223, 180), (137, 184, 141), (255, 199, 0), (255, 255, 255),
         (155, 48, 255), (0, 191, 255), (255, 69, 0), (245, 119, 117), (255, 255, 0),
         (190, 180, 125), (0, 255, 255)]   # last: cyan = Magical Power
_TINTS = [(np.array(t, np.float32), float(np.dot(t, t))) for t in TINTS]

STRONG = 0.6       # text-layer value that counts as a solid letter pixel
LINE_H = 14        # rows kept per line (ascenders + descenders)
SPACE = 4          # empty columns that separate words
PITCH = 17         # tooltip line spacing in pixels
HALF_W = 190       # tooltip text is searched within this distance of the slot centre
NAME_TOL = 0.18    # max difference (worst 4-column window) for a name picture to match
TOKEN_TOL = 0.11   # same for a value picture ("+6%" vs "+5%" differ by ~0.13)
RATIO = 0.75       # ...and it must beat the best match with a different label by this much
FAINT = 0.4        # text-layer values below this are icon specks, ignored when comparing
GLYPH_TOL = 0.2    # same for a single character
USE_DP = True      # read unseen values character by character
MAX_PICS = 6
VALUE_TOKEN = re.compile(r"^\+?\d{1,3}(,\d{3})*%?$")


# ================================================================ image -> text layer
def text_layer_exact(rgb):
    """HxWx3 -> HxW float 0..1: how much each pixel looks like tooltip text (reference)."""
    h, w = rgb.shape[:2]
    keep = np.zeros(h * w, np.float32)
    flat = rgb.reshape(-1, 3)
    idx = np.flatnonzero(flat.max(1) >= 60)        # darker pixels can't be text
    if len(idx) == 0:
        return keep.reshape(h, w)
    px = flat[idx].astype(np.float32)
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    aa = r * r + g * g + b * b
    best = np.zeros(len(idx), np.float32)
    for c, cc in _TINTS:
        dot = r * c[0] + g * c[1] + b * c[2]
        s_ = dot / cc
        ok = (s_ > 0.3) & (s_ < 1.15) & (aa - dot * dot / cc < 256)
        best = np.maximum(best, np.where(ok, np.minimum(s_, 1.0), 0))
    keep[idx] = best
    return keep.reshape(h, w)


def _build_lut():
    """Text-ness of every possible colour (16.7 million), exact, stored as 0..255.
    Built once (about a second); then reading a pixel is a single table lookup."""
    lut = np.empty(1 << 24, np.uint8)
    rg = np.arange(1 << 16, dtype=np.uint32)
    r = (rg >> 8).astype(np.uint8)
    g = (rg & 255).astype(np.uint8)
    for b in range(256):                      # one blue value at a time: 65536 colours
        rgb = np.stack([r, g, np.full_like(r, b)], -1).reshape(-1, 1, 3)
        lut[(rg << 8) | b] = np.round(text_layer_exact(rgb).ravel() * 255).astype(np.uint8)
    return lut


_LUT = None
_LUT_LOCK = threading.Lock()


def _lut():
    global _LUT
    if _LUT is None:
        with _LUT_LOCK:
            if _LUT is None:
                _LUT = _build_lut()
    return _LUT


def text_layer(rgb):
    """HxWx3 -> HxW float 0..1: how much each pixel looks like tooltip text.
    One table lookup per pixel (the colour maths is done once for every colour)."""
    c = rgb.astype(np.uint32)
    idx = (c[..., 0] << 16) | (c[..., 1] << 8) | c[..., 2]
    return _lut()[idx].astype(np.float32) * (1 / 255)


def _remove_borders(layer):
    """The tooltip frame is a thin vertical line in a text colour: wipe long vertical runs."""
    on = layer > 0.25
    run = np.zeros(on.shape[1], np.int32)
    best = np.zeros(on.shape[1], np.int32)
    for row in on:
        run = np.where(row, run + 1, 0)
        np.maximum(best, run, out=best)
    layer[:, best > 40] = 0
    return layer


def _segments(cols, min_gap):
    """Boolean column array -> [(start, end)]; a new run starts after >= min_gap empty columns."""
    xs = np.flatnonzero(cols)
    if len(xs) == 0:
        return []
    cuts = np.flatnonzero(np.diff(xs) - 1 >= max(1, min_gap))
    starts = np.concatenate(([xs[0]], xs[cuts + 1]))
    ends = np.concatenate((xs[cuts] + 1, [xs[-1] + 1]))
    return list(zip(starts.tolist(), ends.tolist()))


def find_frame(layer, cx):
    """The tooltip has a thin frame: two vertical lines of equal height left and right of
    the slot. -> (left, right, top, bottom) in layer coordinates, or None."""
    on = layer > 0.2
    h, w = on.shape
    # length of the vertical run ending at each pixel: rows counted so far minus the row
    # count at the last "off" pixel above it
    c = np.cumsum(on, axis=0, dtype=np.int32)
    run = c - np.maximum.accumulate(np.where(on, 0, c), axis=0)
    best = run.max(0)
    end = run.argmax(0).astype(np.int32)
    cand = [x for x in range(w) if 200 <= best[x] <= 520]
    pick, score = None, -1e9
    for l in cand:
        if l >= cx:
            continue
        for r in cand:
            if r <= cx or not 150 <= r - l <= 460:
                continue
            if abs(int(end[l]) - int(end[r])) > 2:
                continue  # the two frame lines always end on the same row
            tl, tr = end[l] - best[l], end[r] - best[r]
            sc = max(best[l], best[r]) + 0.1 * (r - l)
            if sc > score:
                pick, score = (l, r, int(max(tl, tr)), int(min(end[l], end[r]))), sc
    return pick


def find_lines(rgb, cx):
    """-> list of dicts (y, x0, x1, img, words) for the tooltip's text lines."""
    h, w = rgb.shape[:2]
    x0w, x1w = int(max(0, cx - 260)), int(min(w, cx + 260))
    layer = text_layer(rgb[:, x0w:x1w])
    frame = find_frame(layer, cx - x0w)
    if frame:
        l, r, top_y, bot_y = frame
        sub = layer[:, l + 2:r - 1].copy()
        sub[:max(0, top_y), :] = 0
        sub[bot_y + 1:, :] = 0
        offset, centre = x0w + l + 2, (r - l) / 2 - 2
    else:
        sub = _remove_borders(layer)
        offset, centre = x0w, cx - x0w
    strong = sub > STRONG
    rows = strong.sum(1).astype(np.float32)
    cs = np.concatenate(([0.0], np.cumsum(rows)))
    best = (-1e9, 0)
    for ph in range(PITCH):
        idx = np.arange(ph, h - PITCH, PITCH)
        score = (cs[idx + 7] - cs[idx]).sum() - 0.5 * (cs[idx + PITCH] - cs[idx + 8]).sum()
        if score > best[0]:
            best = (score, ph)
    ph = best[1]

    def chain(segs):
        i = min(range(len(segs)), key=lambda k: abs((segs[k][0] + segs[k][1]) / 2 - centre))
        a = b = i
        while a > 0 and segs[a][0] - segs[a - 1][1] <= 7:
            a -= 1
        while b < len(segs) - 1 and segs[b + 1][0] - segs[b][1] <= 7:
            b += 1
        return segs[a:b + 1]

    off = lambda c: abs((c[0][0] + c[-1][1]) / 2 - centre)
    out = []
    for core in range(ph, h - 7, PITCH):
        if strong[core:core + 7].sum() < 12:
            continue
        top = max(0, core - 4)
        img = sub[top:top + LINE_H]
        if img.shape[0] < LINE_H:
            img = np.pad(img, ((0, LINE_H - img.shape[0]), (0, 0)))
        segs = _segments((img > STRONG).any(0), SPACE)
        if not segs:
            continue
        ws = chain(segs)
        # tooltip text is centred: trim specks glued to one end
        while len(ws) > 1 and off(ws) > 3:
            trimmed = ws[1:] if centre - ws[0][0] > ws[-1][1] - centre else ws[:-1]
            if off(trimmed) >= off(ws):
                break
            ws = trimmed
        if off(ws) > (6 if frame else 4):
            continue
        a = ws[0][0]
        out.append({"y": top, "x0": a + offset, "x1": ws[-1][1] + offset,
                    "img": img[:, a:ws[-1][1]], "words": [(s0 - a, e0 - a) for s0, e0 in ws]})
    return out


def _wwin(col):
    """worst 4-column window of per-column differences (rows = candidates)."""
    if col.shape[-1] <= 4:
        return col.mean(-1)
    cs = np.concatenate((np.zeros(col.shape[:-1] + (1,), col.dtype), np.cumsum(col, -1)), -1)
    return ((cs[..., 4:] - cs[..., :-4]) / 4).max(-1)


def _batch_diff(q, stack):
    """Difference of picture q (H x wq) to every picture in stack (n x H x ws):
    worst 4-column window, best over shifting either side by 0..2 px."""
    best = _batch_diff1(q, stack, 0, 0)
    near = np.flatnonzero(best < SHIFT_GATE)
    if len(near) == 0:
        return best
    sub_ = stack[near]
    for sq, ss in ((1, 0), (0, 1), (2, 0), (0, 2)):
        best[near] = np.minimum(best[near], _batch_diff1(q, sub_, sq, ss))
    return best


SHIFT_GATE = 0.8   # pictures this different unshifted never get close enough by shifting


def _batch_diff1(q, stack, sq, ss):
    best = None
    for sq, ss in ((sq, ss),):
        a, b = q[:, sq:], stack[:, :, ss:]
        wa, wb = a.shape[1], b.shape[2]
        m = min(wa, wb)
        col = np.abs(b[:, :, :m] - a[None, :, :m]).mean(1)
        if wa > m:
            col = np.concatenate((col, np.broadcast_to(a[:, m:].mean(0), (len(b), wa - m))), 1)
        elif wb > m:
            col = np.concatenate((col, b[:, :, m:].mean(1)), 1)
        d = _wwin(col)
        best = d if best is None else np.minimum(best, d)
    return best


def _diff(a, b):
    if abs(a.shape[1] - b.shape[1]) > 2:
        return 1.0
    return float(_batch_diff(a, b[None])[0])


class _Index:
    """Pictures of one table grouped by width, so a query is compared with all
    candidates of similar width in a few numpy operations."""

    def __init__(self, items):
        groups = {}
        for label, pic in items:
            groups.setdefault(pic.shape[1], []).append((label, pic))
        self.by_w = {w: ([l for l, _ in g], np.stack([p for _, p in g])) for w, g in groups.items()}

    def best(self, q, tol):
        per = {}
        wq = q.shape[1]
        for w in range(wq - 2, wq + 3):
            g = self.by_w.get(w)
            if g is None:
                continue
            labels, stack = g
            for l, d in zip(labels, _batch_diff(q, stack)):
                if d < per.get(l, 9):
                    per[l] = d
        if not per:
            return None
        ranked = sorted(per.items(), key=lambda kv: kv[1])
        l1, d1 = ranked[0]
        if d1 > tol:
            return None
        if len(ranked) > 1 and d1 > RATIO * ranked[1][1]:
            return None   # two labels look alike here: not sure
        return l1


def _clean(pic):
    """Drop faint pixels (icon specks behind the text), keep letter strokes."""
    return np.clip((pic - FAINT) / 0.3, 0, 1)


def _num(token):
    return int(re.sub(r"\D", "", token))


# ================================================================ the reader
class FontReader:
    def __init__(self, path, parse_text, bonus_range):
        """parse_text(text) -> [(bonus, value)] (teacher text -> bonus);
        bonus_range(bonus) -> (lo, hi)."""
        self.path = path
        self.parse_text = parse_text
        self.bonus_range = bonus_range
        self.lock = threading.Lock()
        self.names = {}    # bonus name / "#special" / "=other text" -> [picture]
        self.tokens = {}   # value text ("+9%") -> [picture]
        self.glyphs = {}   # char -> [picture]
        self.teacher = None
        self.teacher_lock = threading.Lock()
        self.stats = {"fast": 0, "taught": 0}
        self._ver = 0
        self._idx = (None, None)
        self._cache = {}     # exact line picture -> fast-path answer (cleared when something is learned)
        self._cache_ver = 0
        self._load()

    # ---- persistence
    def _load(self):
        try:
            with open(self.path, "rb") as fh:
                d = pickle.load(fh)
            self.names = d.get("names", {})
            self.tokens = d.get("tokens", {})
            self.glyphs = d.get("glyphs", {})
        except Exception:
            pass

    def save(self):
        with self.lock:
            data = pickle.dumps({"names": self.names, "tokens": self.tokens, "glyphs": self.glyphs})
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, self.path)

    def purge_text_bonuses(self):
        """Drop 'other text' pictures whose text is really a bonus line (old learning bug)."""
        with self.lock:
            bad = [k for k in self.names if k[0] == "=" and self.parse_text(k[1:])]
            for k in bad:
                self.names.pop(k)
            self._ver += 1
        return bad

    def known(self):
        return sum(1 for k in self.names if k[0] not in "#="), len(self.tokens)

    def _indexes(self):
        """(lines, bonus names, values, glyphs) indexes, rebuilt when something was learned."""
        ver, idx = self._idx
        if ver != self._ver or idx is None:
            with self.lock:
                lines = [(k, p) for k, ps in self.names.items() if k[0] in "#=" for p in ps]
                names = [(k, p) for k, ps in self.names.items() if k[0] not in "#=" for p in ps]
                toks = [(k, p) for k, ps in self.tokens.items() for p in ps]
                gl = []
                for ch, gs in self.glyphs.items():   # only samples of a char's usual width
                    widths = [g.shape[1] for g in gs]
                    mode = max(set(widths), key=widths.count)
                    gl += [(ch, g) for g in gs if abs(g.shape[1] - mode) <= 1]
                idx = tuple(_Index(x) for x in (lines, names, toks, gl))
                self._idx = (self._ver, idx)
        return idx

    # ---- teacher (RapidOCR), loaded on first use
    def _teach_text(self, rgb_crop):
        with self.teacher_lock:
            if self.teacher is None:
                from rapidocr_onnxruntime import RapidOCR
                self.teacher = RapidOCR(intra_op_num_threads=4)
            big = np.ascontiguousarray(np.repeat(np.repeat(rgb_crop, 2, 0), 2, 1))
            res, _ = self.teacher(big, use_det=False, use_cls=False)
        if not res:
            return ""
        first = res[0]
        return first[0] if isinstance(first, (list, tuple)) else str(first)

    # ---- matching
    def _read_glyphs(self, pic, gidx):
        """Cut a value into characters (they always have a blank column between them)
        and recognise each one; None if any character is uncertain."""
        segs = _segments((pic > 0.35).any(0), 1)
        if not segs:
            return None
        out = ""
        for a, b in segs:
            if b - a > 13:
                return None   # two characters touching
            ch = gidx.best(pic[:, a:b], GLYPH_TOL)
            if ch is None:
                return None
            out += ch
        return out if VALUE_TOKEN.match(out) else None

    def _read_value(self, pic):
        _, _, tidx, gidx = self._indexes()
        tok = tidx.best(pic, TOKEN_TOL)
        if tok:
            return tok
        return self._read_glyphs(pic, gidx) if USE_DP else None

    def _classify(self, ln):
        """Fast path -> (kind, payload, text); kind None = not sure. Identical line pictures
        (very common: base stats, frequent bonus lines) are answered from a cache."""
        if self._cache_ver != self._ver:
            self._cache, self._cache_ver = {}, self._ver
        key = hashlib.blake2b(ln["img"].tobytes() + repr(ln["words"]).encode() +
                              repr(ln["img"].shape).encode(), digest_size=16).digest()
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        res = self._classify_uncached(ln)
        if len(self._cache) > 20000:
            self._cache.clear()
        self._cache[key] = res
        return res

    def _classify_uncached(self, ln):
        img, words = _clean(ln["img"]), ln["words"]
        lidx, nidx, _, _ = self._indexes()
        k = lidx.best(img, NAME_TOL)
        if k:
            return (k, None, k) if k[0] == "#" else ("text", k[1:], k[1:])
        if len(words) < 2:
            return None, None, ""
        for vfirst in (False, True):
            if vfirst:
                npic, vpic = img[:, words[1][0]:], img[:, words[0][0]:words[0][1]]
            else:
                npic, vpic = img[:, :words[-2][1]], img[:, words[-1][0]:words[-1][1]]
            bonus = nidx.best(npic, NAME_TOL)
            if not bonus:
                continue
            vtxt = self._read_value(vpic)
            if not vtxt:
                return None, None, ""
            lo, hi = self.bonus_range(bonus)
            v = _num(vtxt)
            if not lo <= v <= hi:
                return None, None, ""   # impossible value = misread, let the teacher look
            text = f"{vtxt} {bonus}" if vfirst else f"{bonus} {vtxt}"
            return "bonus", (bonus, v), text
        return None, None, ""

    # ---- learning
    @staticmethod
    def _special_key(text):
        low = re.sub(r"[^a-z]", "", text.lower())
        if "plvl" in low or "fromp" in low:
            return "#plvl"
        if "defen" in low and "magic" in low:   # not "Magical Power"!
            return "#defense"
        if "petart" in low or "artifact" in low:
            return "#petart"
        if "wearable" in low:
            return "#wearable"
        return "=" + text.strip()

    def _add(self, table, key, pic):
        lst = table.setdefault(key, [])
        if len(lst) < MAX_PICS and all(_diff(pic, t) > 0.02 for t in lst):
            lst.append(pic.copy())
            self._ver += 1

    def _learn_line(self, ln, text):
        """Store pictures for a line the teacher read as `text`. Returns (kind, payload)."""
        img, words = _clean(ln["img"]), ln["words"]
        key = self._special_key(text)
        found = self.parse_text(text) if key[0] == "=" else []
        if found and len(words) >= 2:
            bonus, value = found[0]
            nums = re.findall(r"\+?\d[\d,]*%?", text)
            if nums:
                if re.match(r"\s*\d", text):
                    vtok, vpic, npic = nums[0], img[:, words[0][0]:words[0][1]], img[:, words[1][0]:]
                else:
                    vtok, vpic, npic = nums[-1], img[:, words[-1][0]:words[-1][1]], img[:, :words[-2][1]]
                lo, hi = self.bonus_range(bonus)
                if not (VALUE_TOKEN.match(vtok) and lo <= _num(vtok) <= hi):
                    return None, None
                # the per-character reading must agree with the teacher, or neither is trusted
                seen = self._read_glyphs(vpic, self._indexes()[3])
                if seen and seen.lstrip("+") != vtok.lstrip("+"):
                    return None, None
                with self.lock:
                    self._add(self.names, bonus, npic)
                    self._add(self.tokens, vtok, vpic)
                    segs = _segments((vpic > 0.35).any(0), 1)
                    if len(segs) == len(vtok):
                        for (a, b), ch in zip(segs, vtok):
                            lst = self.glyphs.setdefault(ch, [])
                            if len(lst) < 12:
                                lst.append(vpic[:, a:b].copy())
                                self._ver += 1
                return "bonus", (bonus, _num(vtok))
        if found:
            # bonus line we couldn't cut into name + value: use the teacher's reading,
            # but never store it as "other text" (it would hide the bonus next time)
            bonus, value = found[0]
            lo, hi = self.bonus_range(bonus)
            return ("bonus", (bonus, value)) if lo <= value <= hi else (None, None)
        with self.lock:
            self._add(self.names, key, img)
        return (key, None) if key[0] == "#" else ("text", key[1:])

    # ---- reading one tooltip
    def read(self, rgb, cx, teach=True, want_box=False):
        """rgb: HxWx3 capture, cx: slot centre x in it.
        -> (item name, [(bonus, value)] arts, debug text lines[, tooltip box (x0, y0, x1, y1)])"""
        lines = find_lines(rgb, cx)
        out, learned = [], False
        for ln in lines:
            kind, payload, text = self._classify(ln)
            if kind is None and teach:
                y, x0, x1 = ln["y"], ln["x0"], ln["x1"]
                text = self._teach_text(rgb[max(0, y - 1):y + LINE_H + 1, max(0, x0 - 3):x1 + 3])
                self.stats["taught"] += 1
                if text:
                    kind, payload = self._learn_line(ln, text)
                    learned = True
            elif kind is not None:
                self.stats["fast"] += 1
            out.append((kind, payload, text))
            if kind == "#petart":
                break
        if learned:
            try:
                self.save()
            except Exception:
                pass

        # layout: name, From PLvl, base stats, Defense against Magic Power, 7 arts, [Pet Artifact]
        kinds = [k for k, _, _ in out]
        name = ""
        if "#plvl" in kinds:
            i = kinds.index("#plvl")
            if i > 0 and out[i - 1][0] == "text":
                name = out[i - 1][1]
        if "#defense" in kinds:
            start = kinds.index("#defense") + 1
        elif "#plvl" in kinds:
            start = kinds.index("#plvl") + 3
        else:
            start = 0
        arts, seen = [], set()
        for kind, payload, _ in out[start:]:
            if kind == "#petart":
                break
            if kind == "bonus" and payload[0] not in seen:
                seen.add(payload[0])
                arts.append(payload)
        debug = [t if t else "?" for _, _, t in out]
        if want_box:
            box = None
            if lines:
                box = (min(l["x0"] for l in lines), min(l["y"] for l in lines),
                       max(l["x1"] for l in lines), max(l["y"] for l in lines) + LINE_H)
            return name, arts, debug, box
        return name, arts, debug
