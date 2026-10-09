"""Label a dataset with the teacher using several processes (reader_lab.label, but parallel).
    python label_parallel.py [dataset] [workers]"""
import glob
import json
import os
import sys
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_T = None


def _init():
    global _T
    import font_reader as fr
    import pet_art_filter as paf
    _T = fr.FontReader(os.devnull, lambda t: paf.parse_lines([paf.fix_ocr(t)]), lambda b: tuple(paf.BONUSES[b]))
    _T._classify = lambda ln: (None, None, "")
    _T.save = lambda: None
    _T.teacher = None


def _one(j):
    import numpy as np
    from PIL import Image
    try:
        with open(j, encoding="utf-8") as fh:
            meta = json.load(fh)
        if "truth" in meta:
            return 0
        rgb = np.asarray(Image.open(j[:-5] + ".png").convert("RGB"))
        meta["truth"] = _T.read(rgb, meta["cx"])[1]
        with open(j, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        return 1
    except Exception:
        return 0


if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ["APPDATA"], "PetArtFilter", "dataset")
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    files = sorted(glob.glob(os.path.join(ds, "*.json")))
    done = 0
    with Pool(workers, initializer=_init) as pool:
        for i, r in enumerate(pool.imap_unordered(_one, files, chunksize=8)):
            done += r
            if (i + 1) % 100 == 0:
                print(f"{i + 1}/{len(files)}", flush=True)
    print(f"labelled {done} new of {len(files)}")
