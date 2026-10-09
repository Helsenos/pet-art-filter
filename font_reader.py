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
SPECK_TOL = 0.27   # names: a speck next to the text may push the worst-window score up to this...
SPECK_MEAN = 0.08  # ...if the average difference over the whole word stays this small
SPECK_RATIO = 0.62 # ...and the best name is clearly better than any other (d1 <= 0.62 * d2)
GLYPH_SPECK = True
PREFIX_MATCH = True  # speck-proof fallback for bonus lines (see _prefix_match) # characters may use the speck-tolerant match too
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


_VALS = np.arange(256, dtype=np.float32) * (1 / 255)   # 0..255 -> the float layer values


def _u8_above(t):
    """Smallest 0..255 value whose float layer value is > t (so `u8 >= it` == `float > t`)."""
    return int(np.argmax(_VALS > t)) if (_VALS > t).any() else 256


_U20, _U25 = _u8_above(0.2), _u8_above(0.25)


def text_layer_u8(rgb):
    """HxWx3 -> HxW uint8 0..255: how much each pixel looks like tooltip text.
    One table lookup per pixel (the colour maths is done once for every colour)."""
    c = rgb.astype(np.uint32)
    idx = (c[..., 0] << 16) | (c[..., 1] << 8) | c[..., 2]
    return _lut()[idx]


def text_layer(rgb):
    """Same as text_layer_u8, as float 0..1."""
    return _VALS[text_layer_u8(rgb)]


def _remove_borders(layer):
    """The tooltip frame is a thin vertical line in a text colour: wipe long vertical runs."""
    on = layer >= _U25
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
    the slot. -> (left, right, top, bottom) in layer coordinates, or None.
    layer: uint8 (text_layer_u8) or float (text_layer)."""
    on = (layer >= _U20) if layer.dtype == np.uint8 else (layer > 0.2)
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


_USTRONG = _u8_above(STRONG)


def find_lines(rgb, cx, y_from=0):
    """-> list of dicts (y, x0, x1, img, words) for the tooltip's text lines.
    y_from: where the slot is in the capture - the tooltip opens below it, so that part is
    searched first (half the work); if no tooltip frame is found there, the whole capture."""
    if y_from > 0:
        out = _find_lines(rgb[y_from:], cx, need_frame=True)
        if out is not None:
            for ln in out:
                ln["y"] += y_from
            return out
    return _find_lines(rgb, cx)


def _find_lines(rgb, cx, need_frame=False):
    h, w = rgb.shape[:2]
    x0w, x1w = int(max(0, cx - 260)), int(min(w, cx + 260))
    layer = text_layer_u8(rgb[:, x0w:x1w])
    frame = find_frame(layer, cx - x0w)
    if frame and need_frame and frame[2] <= 1:
        return None          # the frame runs into the top edge: the tooltip starts higher up
    if frame:
        l, r, top_y, bot_y = frame
        sub = layer[:, l + 2:r - 1].copy()
        sub[:max(0, top_y), :] = 0
        sub[bot_y + 1:, :] = 0
        offset, centre = x0w + l + 2, (r - l) / 2 - 2
    elif need_frame:
        return None
    else:
        sub = _remove_borders(layer)
        offset, centre = x0w, cx - x0w
    strong = sub >= _USTRONG
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
        segs = _segments((img >= _USTRONG).any(0), SPACE)
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
                    "img": _VALS[img[:, a:ws[-1][1]]], "words": [(s0 - a, e0 - a) for s0, e0 in ws]})
    return out


def _tint_line(rgb, ln):
    """Second look at a line that couldn't be read: every tooltip line is written in ONE
    colour, so keep only the pixels of the line's main text colour - specks of the item
    icons behind the tooltip in other colours disappear. -> new line dict or None."""
    y, x0, x1 = ln["y"], ln["x0"] - 2, ln["x1"] + 2
    crop = rgb[y:y + LINE_H, max(0, x0):x1]
    if crop.shape[0] < LINE_H or crop.shape[1] < 4:
        return None
    px = crop.reshape(-1, 3).astype(np.float32)
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    aa, bright = r * r + g * g + b * b, px.max(1) >= 60
    best, best_n = None, 0
    for c, cc in _TINTS:
        dot = r * c[0] + g * c[1] + b * c[2]
        s_ = dot / cc
        ok = bright & (s_ > 0.3) & (s_ < 1.15) & (aa - dot * dot / cc < 256)
        lay = np.where(ok, np.minimum(s_, 1.0), 0).astype(np.float32)
        n = int((lay > STRONG).sum())
        if n > best_n:
            best, best_n = lay, n
    if best is None:
        return None
    img = best.reshape(crop.shape[:2])
    cols = np.flatnonzero((img > STRONG).any(0))
    if len(cols) == 0:
        return None
    img = img[:, cols[0]:cols[-1] + 1]
    return {"y": y, "x0": max(0, x0) + int(cols[0]), "x1": max(0, x0) + int(cols[-1]) + 1,
            "img": img, "words": _segments((img > STRONG).any(0), SPACE)}


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


def _mean_diff(a, b):
    """Average difference (not worst spot) of two pictures, best of small shifts."""
    best = 9.0
    for sa, sb in ((0, 0), (1, 0), (0, 1), (2, 0), (0, 2)):
        x, y = a[:, sa:], b[:, sb:]
        m = min(x.shape[1], y.shape[1])
        d = (np.abs(x[:, :m] - y[:, :m]).sum() + x[:, m:].sum() + y[:, m:].sum()) / (x.shape[0] * max(x.shape[1], y.shape[1]))
        best = min(best, float(d))
    return best


def _diff(a, b):
    if abs(a.shape[1] - b.shape[1]) > 2:
        return 1.0
    return float(_batch_diff(a, b[None])[0])


def _padded_diff1(q, P, widths, sq, ss):
    """_batch_diff1 for candidates zero-padded to one shared width (one numpy batch instead
    of one per width). Zero columns add nothing to a window that isn't already there, so the
    result equals the per-width version; pictures of 4 columns or less use the plain mean,
    exactly as before."""
    a = q[:, sq:]
    wa = a.shape[1]
    b = P[:, :, ss:]
    wc = max(wa, b.shape[2])
    if b.shape[2] < wc:
        b = np.concatenate((b, np.zeros(b.shape[:2] + (wc - b.shape[2],), b.dtype)), 2)
    if wa < wc:
        a = np.concatenate((a, np.zeros((a.shape[0], wc - wa), a.dtype)), 1)
    col = np.abs(b - a[None]).mean(1)
    cs = np.concatenate((np.zeros((len(col), 1), col.dtype), np.cumsum(col, -1)), -1)
    span = np.maximum(wa, widths - ss)                  # columns the original compared
    narrow = span <= 4
    out = np.empty(len(col), col.dtype)
    if wc > 4:
        out[:] = ((cs[:, 4:] - cs[:, :-4]) / 4).max(-1)
    if narrow.any():
        out[narrow] = cs[narrow, -1] / span[narrow]
    return out


def _padded_diff(q, P, widths):
    best = _padded_diff1(q, P, widths, 0, 0)
    near = np.flatnonzero(best < SHIFT_GATE)
    if len(near) == 0:
        return best
    Pn, wn = P[near], widths[near]
    for sq, ss in ((1, 0), (0, 1), (2, 0), (0, 2)):
        best[near] = np.minimum(best[near], _padded_diff1(q, Pn, wn, sq, ss))
    return best


class _Index:
    """Pictures of one table, zero-padded to one width, so a query is compared with all
    candidates of similar width (+-2 px) in one numpy batch."""

    def __init__(self, items):
        groups = {}
        for label, pic in items:
            groups.setdefault(pic.shape[1], []).append((label, pic))
        self.by_w = {w: ([l for l, _ in g], np.stack([p for _, p in g])) for w, g in groups.items()}
        # candidates ordered by width (same order the per-width search used: same tie-breaking)
        self.labels, pics = [], []
        for w in sorted(self.by_w):
            labels, stack = self.by_w[w]
            self.labels += labels
            pics += list(stack)
        self.widths = np.array([p.shape[1] for p in pics], np.int64)
        if pics:
            h, wmax = pics[0].shape[0], int(self.widths.max())
            self.pad = np.zeros((len(pics), h, wmax), pics[0].dtype)
            for i, p in enumerate(pics):
                self.pad[i, :, :p.shape[1]] = p
        else:
            self.pad = None

    def best(self, q, tol, speck=False):
        if self.pad is None:
            return None
        wq = q.shape[1]
        sel = np.flatnonzero(np.abs(self.widths - wq) <= 2)
        if len(sel) == 0:
            return None
        diffs = _padded_diff(q, self.pad[sel], self.widths[sel])
        per, pic = {}, {}
        for i, d in zip(sel.tolist(), diffs.tolist()):
            l = self.labels[i]
            if d < per.get(l, 9):
                per[l] = d
                pic[l] = self.pad[i, :, :self.widths[i]]
        ranked = sorted(per.items(), key=lambda kv: kv[1])
        l1, d1 = ranked[0]
        d2 = ranked[1][1] if len(ranked) > 1 else 9.0
        if d1 <= tol and d1 <= RATIO * d2:
            return l1
        # a speck of the icon behind the tooltip can spoil the worst spot of an otherwise
        # perfect name: accept only if the whole word matches closely AND it's a clear winner
        if speck and d1 <= SPECK_TOL and d1 <= SPECK_RATIO * d2:
            m1 = _mean_diff(q, pic[l1])
            m2 = _mean_diff(q, pic[ranked[1][0]]) if len(ranked) > 1 else 9.0
            if m1 <= SPECK_MEAN and m1 <= 0.6 * m2:
                return l1
        return None


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
        self._save_lock = threading.Lock()
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
        self.clean_memory()

    def save(self):
        with self.lock:
            data = pickle.dumps({"names": self.names, "tokens": self.tokens, "glyphs": self.glyphs})
        with self._save_lock:          # several reader threads may learn at the same moment
            tmp = f"{self.path}.{threading.get_ident()}.tmp"
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

    @staticmethod
    def _plausible_token(tok, pic):
        """A value picture must be about as wide as its text (~4-8 px per character)."""
        return 4 * len(tok) <= pic.shape[1] <= 8 * len(tok) + 6

    def _is_bonus_text(self, text):
        low = text.lower()
        flat = re.sub(r"[^a-z0-9]", "", low)            # "Max.SP" == "Max. SP"
        return any(b.lower() in low or flat.startswith(re.sub(r"[^a-z0-9]", "", b.lower()))
                   for b in self.names if b[0] not in "#=")

    def clean_memory(self):
        """Forget pictures learned from teacher misreads: value pictures that don't look like
        their text (e.g. "+2,700" stored as "700"), and bonus lines stored as plain text
        (which would make the reader skip that bonus forever)."""
        gidx = self._indexes()[3]
        with self.lock:
            n = 0
            for tok in list(self.tokens):
                keep = []
                for pic in self.tokens[tok]:
                    seen = self._read_glyphs(pic, gidx)
                    if self._plausible_token(tok, pic) and not (seen and seen.lstrip("+") != tok.lstrip("+")):
                        keep.append(pic)
                n += len(self.tokens[tok]) - len(keep)
                if keep:
                    self.tokens[tok] = keep
                else:
                    self.tokens.pop(tok)
            bad = [k for k in self.names if k[0] == "=" and self._is_bonus_text(k[1:])]
            for k in bad:
                self.names.pop(k)
            if n or bad:
                self._ver += 1
        return n + len(bad)

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

    def _teach_clean(self, rgb_crop):
        """Second try for the teacher: only the text, dark on white, 3x bigger - the item icons
        behind the tooltip are gone."""
        lay = text_layer(rgb_crop)
        clean = np.clip((lay - FAINT) / 0.3, 0, 1)
        dark = (255 - clean * 255).astype(np.uint8)
        big = np.pad(np.kron(dark, np.ones((3, 3), np.uint8)), ((12, 12), (16, 16)), constant_values=255)
        with self.teacher_lock:
            if self.teacher is None:
                from rapidocr_onnxruntime import RapidOCR
                self.teacher = RapidOCR(intra_op_num_threads=4)
            res, _ = self.teacher(np.ascontiguousarray(np.stack([big] * 3, -1)), use_det=False, use_cls=False)
        if not res:
            return ""
        first = res[0]
        return first[0] if isinstance(first, (list, tuple)) else str(first)

    # ---- matching
    def _read_glyphs(self, pic, gidx):
        """Cut a value into characters (they always have a blank column between them)
        and recognise each one; None if any character is uncertain."""
        segs = _segments((pic > 0.35).any(0), 1)
        chars = [self._read_piece(pic[:, a:b], gidx) for a, b in segs]
        # a speck of the icon behind the tooltip can sit right next to the value; it's only
        # dropped where the value format allows nothing: before the "+", after the "%",
        # after a complete ",000" group - so a digit is never thrown away
        while len(chars) > 1 and chars[0] is None and chars[1] == "+":
            chars = chars[1:]
        while len(chars) > 1 and chars[-1] is None and None not in chars[:-1] and \
                re.search(r"(%|,\d{3})$", "".join(chars[:-1])):
            chars = chars[:-1]
        if not chars or None in chars:
            return None
        out = "".join(chars)
        return out if VALUE_TOKEN.match(out) else None

    def _read_piece(self, piece, gidx):
        w = piece.shape[1]
        ch = gidx.best(piece, GLYPH_TOL, speck=GLYPH_SPECK) if w <= 13 else None
        if ch is not None:
            return ch
        # a faint speck can glue two characters together: cut this piece again on solid
        # strokes only, keeping 1 px of soft edge
        parts = _segments((piece > 0.7).any(0), 1)
        if len(parts) < 1 or (len(parts) == 1 and parts[0] == (0, w)):
            return None
        out = ""
        for sa, sb in parts:
            sa, sb = max(0, sa - 1), min(w, sb + 1)
            c = gidx.best(piece[:, sa:sb], GLYPH_TOL, speck=GLYPH_SPECK)
            if c is None:
                return None
            out += c
        return out

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
        cache = self._cache        # this thread's snapshot: a cleared cache isn't refilled with old answers
        key = hashlib.blake2b(ln["img"].tobytes() + repr(ln["words"]).encode() +
                              repr(ln["img"].shape).encode(), digest_size=16).digest()
        hit = cache.get(key)
        if hit is not None:
            return hit
        res = self._classify_uncached(ln)
        if len(cache) > 20000:
            cache.clear()
        cache[key] = res
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
            bonus = nidx.best(npic, NAME_TOL, speck=True)
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
        return self._prefix_match(img, nidx) if PREFIX_MATCH else (None, None, "")

    def _prefix_match(self, img, nidx):
        """Fallback when a speck spoils the word cut (glued between name and value, or sitting
        in front of the name): match known name pictures against the start of the line and
        read everything after the name as the value. Only accepted if that rest is a complete,
        possible value - otherwise the teacher looks."""
        segs = _segments((img > 0.35).any(0), 1)
        starts = [0]
        if len(segs) > 1 and segs[0][1] - segs[0][0] <= 3:
            starts.append(segs[1][0])          # skip a tiny speck in front of the name
        W = img.shape[1]
        cand = {}
        for w, (labels, stack) in nidx.by_w.items():
            for s0 in starts:
                if s0 + w + 6 > W:
                    continue
                for l, d in zip(labels, _batch_diff(img[:, s0:s0 + w], stack)):
                    if d <= NAME_TOL and d < cand.get(l, (9,))[0]:
                        cand[l] = (float(d), s0 + w)
        _, _, _, gidx = self._indexes()
        for bonus, (d, end) in sorted(cand.items(), key=lambda kv: -kv[1][1]):   # longest name first
            rest = img[:, end:]
            cols = np.flatnonzero((rest > 0.35).any(0))
            if len(cols) == 0 or cols[0] > 8:
                continue
            vtxt = self._read_glyphs(rest[:, cols[0]:cols[-1] + 1], gidx)
            if not vtxt:
                continue
            lo, hi = self.bonus_range(bonus)
            v = _num(vtxt)
            if lo <= v <= hi:
                return "bonus", (bonus, v), f"{bonus} {vtxt}"
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

    @staticmethod
    def _clean_number(text, tok):
        """The teacher's number must stand on its own: "MaxSP ly400" is not 400 (it's +1,400
        with the "+1," misread) - a number glued to letters is never trusted."""
        return re.search(r"(?<![A-Za-z0-9.,])" + re.escape(tok) + r"(?![A-Za-z0-9])", text) is not None

    def _learn_line(self, ln, text, arts_zone=False):
        """Store pictures for a line the teacher read as `text`. Returns (kind, payload).
        arts_zone: the line is one of the 7 bonuses (between the base stats and [Pet Artifact])."""
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
                # never a wrong value: the number must stand alone and fit the value's width
                if not self._clean_number(text, vtok) or not self._plausible_token(vtok, vpic):
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
            nums = re.findall(r"\+?\d[\d,]*%?", text)
            if not nums or not any(self._clean_number(text, t) and _num(t) == value for t in nums):
                return None, None
            return ("bonus", (bonus, value)) if lo <= value <= hi else (None, None)
        if arts_zone or (key[0] == "=" and self._is_bonus_text(key[1:])):
            return None, None      # a bonus line the teacher misread: don't remember it as text
        with self.lock:
            self._add(self.names, key, img)
        return (key, None) if key[0] == "#" else ("text", key[1:])

    # ---- reading one tooltip
    def read(self, rgb, cx, teach=True, want_box=False, y_from=0):
        """rgb: HxWx3 capture, cx: slot centre x in it.
        -> (item name, [(bonus, value)] arts, debug text lines[, tooltip box (x0, y0, x1, y1)])"""
        lines = find_lines(rgb, cx, y_from)       # y_from: slot row in the capture (tooltip opens below)
        out, learned = [], False
        arts_zone = False                          # after "Defense against Magic Power": the 7 bonuses
        for i, ln in enumerate(lines):
            if i >= 3 and not any(k == "#plvl" for k, _, _ in out):
                break      # not a pet art (its 2nd line is always "From PLvl"): don't read the rest
            kind, payload, text = self._classify(ln)
            if kind is None:                       # icon specks? look again in the line's own colour
                ln2 = _tint_line(rgb, ln)
                if ln2 is not None:
                    k2 = self._classify(ln2)
                    if k2[0] is not None:
                        kind, payload, text = k2
            if kind is None and teach:
                y, x0, x1 = ln["y"], ln["x0"], ln["x1"]
                crop = rgb[max(0, y - 1):y + LINE_H + 1, max(0, x0 - 3):x1 + 3]
                text = self._teach_text(crop)
                self.stats["taught"] += 1
                if text:
                    kind, payload = self._learn_line(ln, text, arts_zone)
                    learned = True
                if kind is None and arts_zone:      # icons behind the text: try once more, text only
                    text2 = self._teach_clean(crop)
                    if text2:
                        kind, payload = self._learn_line(ln, text2, arts_zone)
                        text = text2 if kind else text
            elif kind is not None:
                self.stats["fast"] += 1
            if kind == "#defense":
                arts_zone = True
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
            used = lines[:len(out)]       # name ... [Pet Artifact] (reading stops there)
            if used:
                box = (min(l["x0"] for l in used), min(l["y"] for l in used),
                       max(l["x1"] for l in used), max(l["y"] for l in used) + LINE_H)
            return name, arts, debug, box
        return name, arts, debug
