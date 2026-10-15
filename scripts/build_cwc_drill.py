#!/usr/bin/env python
"""Snapshot the real CWC hourly gauge record of the August 2018 flood for each map.

The Live mode's "flood drill" replays these readings through the live feed pipeline, so the
drill works offline and is reproducible.

    python scripts/build_cwc_drill.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.live.cwc import STATIONS, fetch_series  # noqa: E402

OUT = ROOT / "backend" / "app" / "data" / "events"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for map_id, cfg in STATIONS.items():
        g = cfg["gauge"]
        s = fetch_series(g["code"], datetime(2018, 8, 13), datetime(2018, 8, 21), timeout=120)
        hhs = s.get("HHS", [])
        rain = s.get("MPS", [])
        data = {
            "map": map_id,
            "station": g,
            "source": "Central Water Commission flood-forecasting telemetry (ffs.india-water.gov.in), hourly gauge readings",
            "built": time.strftime("%Y-%m-%d"),
            "readings": [{"time": t, "level_m": v} for t, v in hhs],
            "rain": [{"time": t, "mm": v} for t, v in rain],
        }
        path = OUT / f"cwc_2018__{map_id}.json"
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        lv = [r["level_m"] for r in data["readings"]]
        print(f"{path.name}: {len(lv)} hourly readings {hhs[0][0]} .. {hhs[-1][0]}, {min(lv)} -> {max(lv)} m; {len(rain)} rain obs")


if __name__ == "__main__":
    main()
