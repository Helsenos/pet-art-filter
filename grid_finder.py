"""
One-click grid finder: you click inside the inventory or stash; this finds the slot
grid around that point (edges, rows, columns) and the page buttons right above it.

How: slots repeat every `pitch` px (32 here) and between two slots there is a thin
gap that is dark along its whole length. Scenery or window borders are never dark
along a whole line at exactly that spacing, so walking outward from the click while
the gaps stay dark finds the grid's edges.
"""
import numpy as np

DARK = 32          # brightness below this counts as "gap dark"
FULL = 0.9         # a slot gap must be dark in at least this share of its length


def _pitch(prof, lo=22, hi=56):
    """Repeat distance of a 1-D brightness profile (autocorrelation peak)."""
    v = prof - prof.mean()
    best, best_p = -1e18, None
    for p in range(lo, min(hi, len(v) // 2) + 1):
        c = float(np.dot(v[:-p], v[p:])) / (len(v) - p)
        if c > best:
            best, best_p = c, p
    return best_p


def _gap_score(g, along, idx, span):
    """Share of the line (column x=idx if along=='col', else row y=idx) over `span`
    that has a dark pixel within +-3 px: 1.0 = a perfect slot gap."""
    a, b = span
    if along == "col":
        if idx - 3 < 0 or idx + 4 > g.shape[1]:
            return 0.0
        strip = g[a:b, idx - 3:idx + 4].min(1)
    else:
        if idx - 3 < 0 or idx + 4 > g.shape[0]:
            return 0.0
        strip = g[idx - 3:idx + 4, a:b].min(0)
    return float((strip < DARK).mean()) if len(strip) else 0.0


def _contrast(g, along, idx, span, pitch):
    """Share of the line where the gap is clearly darker than both neighbouring slot
    centres (half a pitch away)."""
    a, b = span
    hp = pitch // 2
    if along == "col":
        if idx - hp - 3 < 0 or idx + hp + 4 > g.shape[1]:
            return 0.0
        gap = g[a:b, idx - 3:idx + 4].min(1)
        left = g[a:b, idx - hp - 3:idx - hp + 4].mean(1)
        right = g[a:b, idx + hp - 3:idx + hp + 4].mean(1)
    else:
        if idx - hp - 3 < 0 or idx + hp + 4 > g.shape[0]:
            return 0.0
        gap = g[idx - 3:idx + 4, a:b].min(0)
        left = g[idx - hp - 3:idx - hp + 4, a:b].mean(0)
        right = g[idx + hp - 3:idx + hp + 4, a:b].mean(0)
    if len(gap) == 0:
        return 0.0
    return float(((left - gap > 12) & (right - gap > 12)).mean())


def _phases(g, px, py, pitch, cells=3):
    """Where the slot borders are (x and y offset mod pitch): stack the slots around the
    click on top of each other; borders look the same in every slot while item pictures
    differ, so the border is where the stacked slots vary least."""
    h, w = g.shape
    x0, y0 = max(0, px - cells * pitch), max(0, py - cells * pitch)
    n = 2 * cells
    reg = g[y0:y0 + n * pitch, x0:x0 + n * pitch]
    ny, nx = reg.shape[0] // pitch, reg.shape[1] // pitch
    if ny < 2 or nx < 2:
        return None
    reg = reg[:ny * pitch, :nx * pitch].reshape(ny, pitch, nx, pitch)
    sd = reg.std(axis=(0, 2))                      # pitch x pitch: variation per cell position
    col = sd.mean(0)
    row = sd.mean(1)

    def centre(v):
        # middle of the low-variation stretch (the border), going round the cycle
        lo_, md = v.min(), np.median(v)
        low = v <= lo_ + 0.3 * (md - lo_)
        i0 = int(np.argmin(v))
        a_ = i0
        while low[(a_ - 1) % pitch] and i0 - a_ < pitch // 3:
            a_ -= 1
        b_ = i0
        while low[(b_ + 1) % pitch] and b_ - i0 < pitch // 3:
            b_ += 1
        return ((a_ + b_) // 2) % pitch

    quality = float(col.min() + row.min())
    return (x0 + centre(col)) % pitch, (y0 + centre(row)) % pitch, quality


def _chain(g, along, centre, pitch, span, cross=None, phase=None):
    """Gap lines spaced `pitch` apart around `centre` that are dark along `span`.
    `cross`: the perpendicular gap lines already known; a new line is only accepted if
    those also continue (are dark) between it and the previous line."""
    other = "row" if along == "col" else "col"

    def crossed(a, b):
        inner = (cross or [])[1:-1]
        if not inner:
            return True
        lo_, hi_ = sorted((a, b))
        # a real gap is darker than the slot centres on both sides of it (a solid black
        # bar like "Coins"/"Yang" is dark everywhere, so it has no such contrast);
        # on average, since stack counts like "8229" may cover a gap in places
        return np.mean([_contrast(g, other, c, (lo_ + 2, hi_ - 1), pitch) for c in inner]) >= 0.3

    # where the gaps sit within the pitch: score every position over ~8 slots around the
    # click and fold onto one cycle; the gap phase stands out even if some gaps are covered
    n = g.shape[1] if along == "col" else g.shape[0]
    xs = range(max(4, centre - 4 * pitch), min(n - 4, centre + 4 * pitch))
    fold = np.zeros(pitch)
    cnt = np.zeros(pitch)
    a, b = span
    for x in xs:   # exact per-pixel darkness (no +-3 tolerance) so the gap centre is sharp
        line = g[a:b, x] if along == "col" else g[x, a:b]
        fold[x % pitch] += float((line < DARK).mean()) if len(line) else 0.0
        cnt[x % pitch] += 1
    fold /= np.maximum(cnt, 1)
    if phase is None:
        sm = np.array([fold[np.arange(i - 3, i + 4) % pitch].mean() for i in range(pitch)])
        phase = int(np.argmax(sm))
    start = phase + ((centre - phase) // pitch) * pitch   # gap line just before the click

    def snap(x):  # the game's grid is perfectly regular: no snapping, or errors add up
        return x if _gap_score(g, along, x, span) >= FULL else None

    if snap(start) is None:
        return []
    lines = [start]
    x = lines[0] - pitch
    while (s := snap(x)) is not None and crossed(s, lines[0]):
        lines.insert(0, s)
        x = s - pitch
    x = lines[-1] + pitch
    while (s := snap(x)) is not None and crossed(lines[-1], s):
        lines.append(s)
        x = s + pitch
    return lines


def find_grid(gray, px, py):
    """gray: 2-D screenshot, (px, py): clicked point inside the grid.
    -> dict(x, y, w, h, rows, cols, pitch, tabs) or None."""
    g = gray.astype(np.float32)
    h, w = g.shape
    band = g[max(0, py - 40):py + 40, max(0, px - 250):px + 250].mean(0)
    pitch = _pitch(band) or 32

    # slot size: the one at which the stacked slots line up best
    best = None
    for p_ in range(pitch - 2, pitch + 3):
        ph = _phases(g, px, py, p_)
        if ph and (best is None or ph[2] < best[1][2]):
            best = (p_, ph)
    if best is None:
        return None
    pitch, (phx, phy, _) = best
    # start with the clicked slot's row, then refine both directions
    rspan = (max(0, py - pitch // 2), min(h, py + pitch // 2))
    cols = _chain(g, "col", px, pitch, rspan, phase=phx)
    if len(cols) < 2:
        return None
    rows = []
    for _ in range(3):
        rows = _chain(g, "row", py, pitch, (cols[0], cols[-1]), cross=cols, phase=phy)
        if len(rows) < 2:
            return None
        cols = _chain(g, "col", px, pitch, (rows[0], rows[-1]), cross=rows, phase=phx)
        if len(cols) < 2:
            return None
    # fit the found gaps to a perfectly even grid (so every click gives the same result)
    def fit(lines):
        k = np.arange(len(lines))
        b, a = np.polyfit(k, np.array(lines, np.float64), 1)
        return a, b

    ax, bx = fit(cols)
    ay, by = fit(rows)
    grid = {"x": int(round(ax)), "y": int(round(ay)), "w": int(round(bx * (len(cols) - 1))),
            "h": int(round(by * (len(rows) - 1))), "rows": len(rows) - 1, "cols": len(cols) - 1,
            "pitch": int(pitch)}
    grid["tabs"] = find_tabs(g, grid)
    return grid


def find_tabs(g, grid):
    """Page buttons (I, II, III ...) right above the grid. Going up from the grid: bright
    button rows with dark gaps between them, ending at the plain window background."""
    x0, x1, top = grid["x"], grid["x"] + grid["w"], grid["y"]
    rows = []
    for y in range(top - 1, max(0, top - 90), -1):
        r = g[y, x0:x1]
        rows.append((y, float(r.mean()), float(r.std())))
    # end of the buttons = first run (going up) of >= 4 plain background rows
    run, end = 0, None
    for y, m, s in rows:
        if s < 15 and 30 < m < 75:
            run += 1
            if run >= 4:
                end = y + run      # first row below the background run
                break
        else:
            run = 0
    if end is None or top - end < 12:
        return None
    band = g[end:top, x0:x1]
    means = band.mean(1)
    bright = means > 60
    n_rows, inside, length = 0, False, 0
    for b in list(bright) + [False]:
        if b:
            length += 1
        else:
            if length >= 6:
                n_rows += 1
            length = 0
    if n_rows == 0:
        return None
    # buttons per row: dark separators across the first button row
    first_row = band[np.argmax(bright):][:max(6, int(bright.sum() / n_rows))]
    dark_cols = (first_row.min(0) < DARK)
    seps, prev = 0, False
    for i, d in enumerate(dark_cols):
        if d and not prev and 4 < i < len(dark_cols) - 4:
            seps += 1
        prev = d
    n_cols = seps + 1 if seps else max(1, round(grid["w"] / grid["pitch"]))
    return {"x": int(x0), "y": int(end), "w": int(x1 - x0), "h": int(top - end),
            "rows": int(n_rows), "cols": int(n_cols)}


def find_grid_vote(gray, px, py):
    """Run the finder from the clicked point and its 4 neighbouring slots and return the
    answer most of them agree on (one unlucky slot can't spoil the result)."""
    first = find_grid(gray, px, py)
    pitch = first["pitch"] if first else 32
    results = [first]
    for dx, dy in ((pitch, 0), (-pitch, 0), (0, pitch), (0, -pitch), (pitch, pitch), (-pitch, -pitch), (pitch, -pitch), (-pitch, pitch)):
        results.append(find_grid(gray, px + dx, py + dy))
    results = [r for r in results if r and r["rows"] >= 2 and r["cols"] >= 2]
    if not results:
        return None

    # group attempts that agree on where the grid starts (same slot lines)
    groups = []
    for r in results:
        for grp in groups:
            if abs(grp[0]["x"] - r["x"]) <= 6 and abs(grp[0]["y"] - r["y"]) <= 6 \
                    and grp[0]["pitch"] == r["pitch"]:
                grp.append(r)
                break
        else:
            groups.append([r])
    best = max(groups, key=len)
    if len(best) < 2:
        return None   # nobody agrees: not a slot grid
    # attempts only ever stop early (never run past the grid), so take the biggest
    big = max(best, key=lambda r: r["rows"] * r["cols"])
    out = dict(big)
    out["x"] = int(round(np.median([r["x"] for r in best])))
    out["y"] = int(round(np.median([r["y"] for r in best])))
    out["w"] = big["cols"] * big["pitch"]
    out["h"] = big["rows"] * big["pitch"]
    tabs = [r["tabs"] for r in best if r["tabs"]]
    out["tabs"] = tabs[0] if tabs else None
    return out


# ---------------------------------------------------------------- cell-based finder
def _ring(cell, b=3):
    """The border band of one cell (where slot frames / gaps are), flattened."""
    return np.concatenate((cell[:b].ravel(), cell[-b:].ravel(), cell[b:-b, :b].ravel(), cell[b:-b, -b:].ravel()))


def find_grid_cells(gray, px, py, reach=26):
    """Lattice from slot size + border position; then every cell whose border looks like
    the slots around the click is a slot; the connected block of slots around the click
    is the grid. One covered border can't cut the grid short this way."""
    g = gray.astype(np.float32)
    h, w = g.shape
    band = g[max(0, py - 40):py + 40, max(0, px - 250):px + 250].mean(0)
    pitch = _pitch(band) or 32
    best = None
    for p_ in range(pitch - 2, pitch + 3):
        ph = _phases(g, px, py, p_)
        if ph and (best is None or ph[2] < best[1][2]):
            best = (p_, ph)
    if best is None:
        return None
    p, (phx, phy, _) = best
    # lattice: cell (i, j) spans from gap line to gap line; gap lines at phase + k*p
    ox = phx + ((px - phx) // p) * p - p // 2   # put the gap in the middle of the ring
    oy = phy + ((py - phy) // p) * p - p // 2
    # shift so cells sit between gap lines (gap line = cell edge)
    ox += p // 2
    oy += p // 2
    ci, cj = 0, 0                                # clicked cell in lattice coords

    def cell(i, j):
        x, y = ox + j * p, oy + i * p
        if x < 0 or y < 0 or x + p > w or y + p > h:
            return None
        return g[y:y + p, x:x + p]

    # reference border: median over the 3x3 cells around the click
    refs = [_ring(c) for c in (cell(i, j) for i in (-1, 0, 1) for j in (-1, 0, 1)) if c is not None]
    if len(refs) < 5:
        return None
    ref = np.median(np.stack(refs), 0)
    spread = float(np.median([np.abs(r - ref).mean() for r in refs])) + 1.0

    n = 2 * reach + 1
    slot = np.zeros((n, n), bool)
    for i in range(-reach, reach + 1):
        for j in range(-reach, reach + 1):
            c = cell(i, j)
            if c is None:
                continue
            d = np.abs(_ring(c) - ref)
            # 60th percentile: an item or stack count covering part of the border is ok
            slot[i + reach, j + reach] = float(np.percentile(d, 60)) < 14   # slots 0-11, outside >= 17
    # connected block around the click
    seen = np.zeros_like(slot)
    stack = [(reach, reach)]
    if not slot[reach, reach]:
        return None
    while stack:
        i, j = stack.pop()
        if seen[i, j] or not slot[i, j]:
            continue
        seen[i, j] = True
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            a, b = i + di, j + dj
            if 0 <= a < n and 0 <= b < n and not seen[a, b]:
                stack.append((a, b))
    rows = np.where(seen.any(1))[0]
    cols = np.where(seen.any(0))[0]
    # trim thin protrusions: keep rows/cols mostly filled with slots
    sub = seen[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    r_ok = np.where(sub.mean(1) >= 0.5)[0]
    c_ok = np.where(sub.mean(0) >= 0.5)[0]
    if len(r_ok) < 2 or len(c_ok) < 2:
        return None
    i0, i1 = rows[0] + r_ok[0] - reach, rows[0] + r_ok[-1] - reach
    j0, j1 = cols[0] + c_ok[0] - reach, cols[0] + c_ok[-1] - reach
    grid = {"x": int(ox + j0 * p), "y": int(oy + i0 * p), "w": int((j1 - j0 + 1) * p),
            "h": int((i1 - i0 + 1) * p), "rows": int(i1 - i0 + 1), "cols": int(j1 - j0 + 1),
            "pitch": int(p)}
    grid["tabs"] = find_tabs(g, grid)
    return grid


def find(gray, px, py, need=4):
    """Main entry: run the cell finder from the click and its 8 neighbouring slots and
    accept a grid only if at least `need` of the 9 runs agree on exactly the same one.
    A rare mistake at one edge gets outvoted; scenery never agrees with itself."""
    first = find_grid_cells(gray, px, py)
    pitch = first["pitch"] if first else 32
    runs = [first]
    for dy in (-pitch, 0, pitch):
        for dx in (-pitch, 0, pitch):
            if dx or dy:
                runs.append(find_grid_cells(gray, px + dx, py + dy))
    groups = []
    for r in runs:
        if not r:
            continue
        for grp in groups:
            q = grp[0]
            if (q["rows"], q["cols"], q["pitch"]) == (r["rows"], r["cols"], r["pitch"]) \
                    and abs(q["x"] - r["x"]) <= 3 and abs(q["y"] - r["y"]) <= 3:
                grp.append(r)
                break
        else:
            groups.append([r])
    if not groups:
        return None
    best = max(groups, key=len)
    if len(best) < need:
        return None
    if not 26 <= best[0]["pitch"] <= 40:
        return None   # not a slot size the game uses
    if best[0]["rows"] > 16 or best[0]["cols"] > 24:
        return None   # far bigger than any inventory / stash: something else on screen
    out = dict(best[0])
    out["x"] = int(round(np.median([r["x"] for r in best])))
    out["y"] = int(round(np.median([r["y"] for r in best])))
    tabs = [r["tabs"] for r in best if r["tabs"]]
    out["tabs"] = tabs[len(tabs) // 2] if tabs else None
    return out


# ---------------------------------------------------------------- stash tab list
def find_side_tabs(gray, grid, lo=34, hi=52):
    """Stash pages are a column of identical boxes (I ... XV) left of the grid. Their
    borders are long horizontal edges repeating every ~41 px: find those edges, take the
    longest evenly spaced chain, count the boxes.
    Returns tabs dict (x, y, w, h, rows, cols=1) or None."""
    g = gray.astype(np.float32)
    H, W = g.shape
    x0, x1 = max(0, grid["x"] - 340), max(0, grid["x"] - 12)
    y0, y1 = max(0, grid["y"] - 160), min(H, grid["y"] + grid["h"] + 360)
    reg = g[y0:y1, x0:x1]
    if reg.shape[0] < 3 * hi or reg.shape[1] < 60:
        return None
    edge = np.abs(np.diff(reg, axis=0)) > 12          # horizontal edges, row y -> y+1
    lines = []                                        # (y, start, end) of long edge runs
    for y in range(edge.shape[0]):
        row = np.concatenate(([False], edge[y], [False]))
        d = np.diff(row.astype(np.int8))
        st, en = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        if len(st):
            k = int(np.argmax(en - st))
            if en[k] - st[k] >= 80:
                lines.append((y, int(st[k]), int(en[k])))
    # merge edge rows of one border (within 4 px)
    merged = []
    for y, a_, b_ in lines:
        if merged and y - merged[-1][0][-1] <= 4:
            merged[-1][0].append(y)
            merged[-1][1].append(a_)
            merged[-1][2].append(b_)
        else:
            merged.append(([y], [a_], [b_]))
    borders = [(int(np.mean(ys)), int(np.median(st)), int(np.median(en))) for ys, st, en in merged]
    # longest chain spaced evenly (lo..hi px) with matching left/right ends
    best = []
    for i in range(len(borders)):
        chain = [borders[i]]
        step = None
        for b in borders[i + 1:]:
            gap = b[0] - chain[-1][0]
            same_span = abs(b[1] - chain[0][1]) <= 8 and abs(b[2] - chain[0][2]) <= 8
            if not same_span:
                continue
            if step is None and lo <= gap <= hi:
                step = gap
                chain.append(b)
            elif step is not None and abs(gap - step) <= 3:
                chain.append(b)
            elif step is not None and gap > step + 3:
                break
        if len(chain) > len(best):
            best = chain
    if len(best) < 4:                                 # at least 3 boxes
        return None
    xs = int(np.median([b[1] for b in best]))
    xe = int(np.median([b[2] for b in best]))
    return {"x": int(x0 + xs), "y": int(y0 + best[0][0]), "w": int(xe - xs),
            "h": int(best[-1][0] - best[0][0]), "rows": len(best) - 1, "cols": 1}
