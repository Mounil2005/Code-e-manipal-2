#!/usr/bin/env python
"""Snapshot the observed Sentinel-1 flood mask (9 Aug vs 21 Aug 2018) for each Kerala map, plus
the validation numbers, so the app can show observed water next to the model.

Uses the same change detection as scripts/validate_flood.py (and its scene cache). Output:
backend/app/data/events/s1_2018__<map>.json with the mask as horizontal pixel runs in the map's
km frame.

    python scripts/build_s1_mask.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
from rasterio.warp import transform as warp_transform

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))
import validate_flood as v  # noqa: E402
from app.simulation.maps import Projection  # noqa: E402

OUT = ROOT / "backend" / "app" / "data" / "events"


def build(map_id: str) -> dict:
    snap = json.loads((ROOT / "backend" / "app" / "data" / "maps" / f"{map_id}.json").read_text(encoding="utf-8"))
    s, w, n, e = snap["bbox"]
    bbox = (w, s, e, n)
    pre = v.find_scene(bbox, "2018-08-09", "descending", None)
    post = v.find_scene(bbox, "2018-08-21", "descending", pre["properties"].get("sat:relative_orbit"))
    a_pre, ext, crs = v.read_vv(pre, bbox)
    a_post, _, _ = v.read_vv(post, bbox)
    h = min(a_pre.shape[0], a_post.shape[0])
    wd = min(a_pre.shape[1], a_post.shape[1])
    db_pre, db_post = v.to_db(a_pre[:h, :wd]), v.to_db(a_post[:h, :wd])
    valid = np.isfinite(a_pre[:h, :wd]) & np.isfinite(a_post[:h, :wd])
    flooded = ((db_post - db_pre) < -3.0) & (db_post < -10.0) & valid
    l, b, r, t = ext
    pw, ph = (r - l) / wd, (t - b) / h
    proj = Projection(*snap["origin"])
    runs = []
    for row in range(h):
        cols = np.flatnonzero(flooded[row])
        if not len(cols):
            continue
        # contiguous runs of flooded pixels on this row
        breaks = np.flatnonzero(np.diff(cols) > 1)
        starts = np.r_[cols[0], cols[breaks + 1]]
        ends = np.r_[cols[breaks], cols[-1]]
        ys = t - (row + 0.5) * ph
        for c0, c1 in zip(starts, ends):
            xs = [l + c0 * pw, l + (c1 + 1) * pw]
            lons, lats = warp_transform(crs, "EPSG:4326", xs, [ys, ys])
            (x0, y0), (x1, _) = proj.xy(lats[0], lons[0]), proj.xy(lats[1], lons[1])
            runs.append([round(x0, 3), round(y0, 3), round(x1, 3)])
    val = v.validate(map_id, "2018-08-09", "2018-08-21", "descending", -10.0, None, 0.05, 3.0, 400.0)
    return {
        "map": map_id,
        "source": "Copernicus Sentinel-1 RTC (VV), Microsoft Planetary Computer",
        "method": "change detection: VV(21 Aug) - VV(9 Aug) < -3 dB and VV(21 Aug) < -10 dB, 3x3 mean despeckle, 40 m pixels",
        "pre": pre["id"][:48],
        "post": post["id"][:48],
        "post_time": post["properties"]["datetime"],
        "pixel_km": round((pw + ph) / 2000, 4),
        "row_km": round(ph / 1000, 4),
        "flooded_km2": round(float(flooded.sum()) * pw * ph / 1e6, 2),
        "runs": runs,
        "validation": {k: val[k] for k in ("discharge_m3s", "ramp_c", "river_level_model_m", "nodes", "observed_wet", "model_wet", "confusion", "auc_model_depth", "auc_inverse_elevation", "auc_inverse_river_distance", "base_rate", "radius_m")},
        "built": time.strftime("%Y-%m-%d"),
    }


def main() -> None:
    for map_id in ("kerala_aluva", "kerala_chengannur"):
        d = build(map_id)
        path = OUT / f"s1_2018__{map_id}.json"
        path.write_text(json.dumps(d, separators=(",", ":"), default=float), encoding="utf-8")
        print(f"{path.name}: {len(d['runs'])} runs, {d['flooded_km2']} km2 observed flooding; AUC(elev) {d['validation']['auc_inverse_elevation']:.2f}")


if __name__ == "__main__":
    main()
