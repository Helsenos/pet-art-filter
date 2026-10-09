"""
Reader lab: measure and tune the pet art reader on real tooltips the app saved
(dataset folder: tooltip cut-outs recorded while scanning).

    python reader_lab.py label   [dataset]   teacher reads every line once -> answer sheet (cached)
    python reader_lab.py eval    [dataset]   fast reader vs answer sheet: complete / wrong / missing / speed
    python reader_lab.py relearn [dataset]   rebuild the font memory from all tooltips (old one backed up)

dataset defaults to %APPDATA%\\PetArtFilter\\dataset
"""
import glob
import json
import os
import shutil
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import font_reader as fr  # noqa: E402
import pet_art_filter as paf  # noqa: E402

PARSE = lambda t: paf.parse_lines([paf.fix_ocr(t)])  # noqa: E731
RANGE = lambda b: tuple(paf.BONUSES[b])  # noqa: E731


def items(ds):
    for j in sorted(glob.glob(os.path.join(ds, "*.json"))):
        png = j[:-5] + ".png"
        if os.path.exists(png):
            yield j, png


def load(j, png):
    with open(j, encoding="utf-8") as fh:
        meta = json.load(fh)
    return meta, np.asarray(Image.open(png).convert("RGB"))


def label(ds):
    """Answer sheet: the teacher reads every line (slow, careful) - stored in each json as 'truth'."""
    T = fr.FontReader(os.path.join(ds, "_truth_tmp.pkl"), PARSE, RANGE)
    T._classify = lambda ln: (None, None, "")
    T.save = lambda: None
    n = done = 0
    for j, png in items(ds):
        meta, rgb = load(j, png)
        n += 1
        if "truth" in meta:
            continue
        meta["truth"] = T.read(rgb, meta["cx"])[1]
        with open(j, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        done += 1
    print(f"labelled {done} new, {n} total")


def evaluate(ds, memory=paf.FONT_PATH):
    R = fr.FontReader(memory, PARSE, RANGE)
    R.save = lambda: None
    fr._lut()      # build the colour table before timing anything
    tot = complete = wrong = missing = extra = 0
    times, bad = [], []
    for j, png in items(ds):
        meta, rgb = load(j, png)
        if "truth" not in meta:
            continue
        R._cache = {}
        t = time.perf_counter()
        arts = R.read(rgb, meta["cx"], teach=False)[1]
        times.append((time.perf_counter() - t) * 1000)
        truth = dict(map(tuple, meta["truth"]))
        got = dict(arts)
        tot += 1
        complete += len(arts) >= paf.ARTS_PER_PET
        w = [b for b in got if b in truth and truth[b] != got[b]]
        wrong += len(w)
        missing += sum(1 for b in truth if b not in got)
        extra += sum(1 for b in got if b not in truth)
        if w or len(arts) < paf.ARTS_PER_PET:
            bad.append((os.path.basename(png), w, [b for b in truth if b not in got]))
    if not tot:
        print("no labelled tooltips - run 'label' first")
        return
    print(f"tooltips {tot} | complete {complete}/{tot} ({complete / tot:.1%}) | wrong values {wrong} | "
          f"missing {missing} | extra {extra} | {np.mean(times):.1f} ms avg, {np.percentile(times, 95):.1f} ms p95")
    for b in bad[:15]:
        print("   ", b)
    return {"tot": tot, "complete": complete, "wrong": wrong, "missing": missing}


def relearn(ds):
    if os.path.exists(paf.FONT_PATH):
        shutil.copy(paf.FONT_PATH, paf.FONT_PATH + f".{time.strftime('%Y%m%d-%H%M%S')}.bak")
    R = fr.FontReader(paf.FONT_PATH, PARSE, RANGE)
    for _ in range(2):
        for j, png in items(ds):
            meta, rgb = load(j, png)
            R.read(rgb, meta["cx"], teach=True)
    R.save()
    print("relearned; known:", R.known())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "eval"
    ds = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.environ.get("APPDATA", ""), "PetArtFilter", "dataset")
    {"label": label, "eval": evaluate, "relearn": relearn}[cmd](ds)
