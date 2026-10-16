#!/usr/bin/env python
"""Snapshot the real CWC hourly gauge record of each map's reference flood (August 2018 for the
Kerala maps, August 2022 for the Mahanadi delta), plus the upstream dams' outflow reports.

The Live mode's "flood drill" replays these readings through the live feed pipeline, so the
drill works offline and is reproducible.

    python scripts/build_cwc_drill.py                          # every map
    python scripts/build_cwc_drill.py odisha_mahanadi_delta    # one map
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


def qc_day_blocks(rows: list[tuple[str, float]], jump_m: float = 1.0, fit_m: float = 0.3) -> tuple[list[dict], list[str]]:
    """Telemetry QC. CWC sometimes files a whole day of readings at a wrong level (Naraj, 16 Aug
    2022: 24.8 m between 27.45 m on either side). A day is rejected when it jumps more than
    `jump_m` at both ends in opposite directions and dropping it joins its neighbours to within
    `fit_m`. Rejected hours are refilled by linear interpolation and marked."""
    days: dict[str, list[tuple[str, float]]] = {}
    for t, v in rows:
        days.setdefault(t[:10], []).append((t, v))
    keys = sorted(days)
    bad: list[str] = []
    for i in range(1, len(keys) - 1):
        prev_end, cur, next_start = days[keys[i - 1]][-1][1], days[keys[i]], days[keys[i + 1]][0][1]
        a, b = cur[0][1] - prev_end, next_start - cur[-1][1]
        if abs(a) > jump_m and abs(b) > jump_m and a * b < 0 and abs(next_start - prev_end) <= fit_m and keys[i - 1] not in bad:
            bad.append(keys[i])
    good = [(t, v) for t, v in rows if t[:10] not in bad]
    out: list[dict] = []
    gi = 0
    for t, v in rows:
        if t[:10] not in bad:
            out.append({"time": t, "level_m": v})
            gi += 1
            continue
        lo, hi = good[gi - 1], good[gi]
        f = (datetime.fromisoformat(t) - datetime.fromisoformat(lo[0])) / (datetime.fromisoformat(hi[0]) - datetime.fromisoformat(lo[0]))
        out.append({"time": t, "level_m": round(lo[1] + f * (hi[1] - lo[1]), 2), "interpolated": True})
    notes = [f"{d}: all readings rejected by QC (filed ~{abs(days[d][0][1] - days[keys[keys.index(d) - 1]][-1][1]):.1f} m off the neighbouring days); interpolated" for d in bad]
    return out, notes


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    only = sys.argv[1:]
    for map_id, cfg in STATIONS.items():
        if only and map_id not in only:
            continue
        g, dr = cfg["gauge"], cfg["drill"]
        t0, t1 = (datetime.fromisoformat(x) for x in dr["fetch"])
        s = fetch_series(g["code"], t0, t1, timeout=120)
        hhs = s.get("HHS", [])
        rain = s.get("MPS", [])
        dams = []
        for d in cfg["dams"]:
            ds = fetch_series(d["code"], t0, t1, timeout=120)
            dams.append({**d, "outflow": [{"time": t, "m3s": v} for t, v in ds.get("FOU", [])], "inflow": [{"time": t, "m3s": v} for t, v in ds.get("FIN", [])]})
        readings, qc = qc_day_blocks(hhs)
        for n in qc:
            print("  QC", n)
        data = {
            "map": map_id,
            "station": g,
            "window": list(dr["window"]),
            "source": "Central Water Commission flood-forecasting telemetry (ffs.india-water.gov.in), hourly gauge readings",
            "built": time.strftime("%Y-%m-%d"),
            "readings": readings,
            "qc": qc,
            "rain": [{"time": t, "mm": v} for t, v in rain],
            "dams": dams,
        }
        path = OUT / f"cwc_{dr['year']}__{map_id}.json"
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        lv = [r["level_m"] for r in data["readings"]]
        print(f"{path.name}: {len(lv)} hourly readings {hhs[0][0]} .. {hhs[-1][0]}, {min(lv)} -> {max(lv)} m; {len(rain)} rain obs; dams {[(d['name'], len(d['outflow'])) for d in dams]}")


if __name__ == "__main__":
    main()
