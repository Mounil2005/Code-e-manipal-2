"""Central Water Commission (CWC) flood-forecasting telemetry.

CWC's public flood map (ffs.india-water.gov.in) is backed by an undocumented JSON service. It
needs no account. Because it is undocumented and can change without notice, everything here is
best-effort: short timeouts, results cached, and callers must work without it.

Data types seen per station: HHS = hourly water level (m, gauge reading), HZS = level with the
gauge datum added, MPS = rainfall (mm); reservoirs add FIN/FOU (inflow/outflow, m3/s) and STR
(storage).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Optional

BASE = "https://ffs.india-water.gov.in/iam/api"
UA = "resqnet-research/0.1 (educational flood-rescue simulation)"

# River gauge + upstream reservoirs per map. base_m / peak_m: the 2018 flood record at that gauge
# (14 Aug 00:00 start and the highest reading), used to map a reading onto the simulator's river
# ramp (0 = calm, 1 = the 2018 peak).
STATIONS: dict[str, dict[str, Any]] = {
    "kerala_aluva": {
        "gauge": {"code": "012-SWRDKOCHI", "name": "Neeleeswaram", "river": "Periyar", "base_m": 5.9, "peak_m": 12.4},
        "dams": [
            {"code": "0024-SWRDKOCHI", "name": "Idukki reservoir"},
            {"code": "0025-SWRDKOCHI", "name": "Idamalayar reservoir"},
        ],
    },
    "kerala_chengannur": {
        "gauge": {"code": "018-SWRDKOCHI", "name": "Malakkara", "river": "Pamba", "base_m": 4.9, "peak_m": 9.58},
        "dams": [],
    },
}


def ramp_for_level(map_id: str, level_m: float) -> float:
    g = STATIONS.get(map_id, {}).get("gauge")
    if not g:
        return 0.0
    return max(0.0, min(1.0, (level_m - g["base_m"]) / (g["peak_m"] - g["base_m"])))


def _expr(field: str, op: str, value: str) -> dict:
    return {"expression": {"valueIsRelationField": False, "fieldName": field, "operator": op, "value": value}}


def fetch_series(code: str, t0: datetime, t1: datetime, timeout: float = 25.0) -> dict[str, list[tuple[str, float]]]:
    """All data types for a station between t0 and t1 (local station time), oldest first."""
    spec = {"where": {"where": _expr("id.stationCode", "eq", code), "and": _expr("dataValue", "null", "false")}, "and": _expr("id.dataTime", "btn", f"{t0:%Y-%m-%dT%H:%M:%S.000},{t1:%Y-%m-%dT%H:%M:%S.000}")}
    params = {"specification": json.dumps(spec), "sort-criteria": json.dumps({"sortOrderDtos": [{"sortDirection": "ASC", "field": "id.dataTime"}]})}
    req = urllib.request.Request(
        f"{BASE}/new-entry-data/specification/sorted?" + urllib.parse.urlencode(params),
        headers={"User-Agent": UA, "Accept": "application/json", "class-name": "NewEntryDataDto", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rows = json.loads(r.read())
    out: dict[str, list[tuple[str, float]]] = {}
    for x in rows:
        out.setdefault(x["id"]["datatypeCode"], []).append((x["id"]["dataTime"][:16], float(x["dataValue"])))
    return out


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()
TTL_S = 600.0  # CWC updates hourly; never poll more often than every 10 minutes


def latest(map_id: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """Latest readings + the last 24 h for the map's gauge and dams. Never raises."""
    cfg = STATIONS.get(map_id)
    if not cfg:
        return {"available": False, "reason": "no CWC station configured for this map"}
    with _LOCK:
        hit = _CACHE.get(map_id)
        if hit and time.time() - hit[0] < TTL_S:
            return hit[1]
    now = now or datetime.utcnow() + timedelta(hours=5, minutes=30)  # station times are IST
    t0 = now - timedelta(hours=26)
    res: dict[str, Any] = {"available": True, "fetched_at": time.time(), "source": "CWC flood-forecasting telemetry (ffs.india-water.gov.in)", "gauge": None, "dams": []}
    try:
        g = cfg["gauge"]
        s = fetch_series(g["code"], t0, now)
        level = s.get("HHS", [])
        res["gauge"] = {
            **g,
            "level_m": level[-1][1] if level else None,
            "time": level[-1][0] if level else None,
            "history": level[-24:],
            "rain_mm_24h": round(sum(v for _, v in s.get("MPS", [])[-8:]), 1),
            "ramp": ramp_for_level(map_id, level[-1][1]) if level else None,
        }
        for d in cfg["dams"]:
            ds = fetch_series(d["code"], t0, now)
            outflow = ds.get("FOU", [])
            inflow = ds.get("FIN", [])
            lvl = ds.get("HHS", [])
            res["dams"].append({
                **d,
                "outflow_m3s": outflow[-1][1] if outflow else None,
                "inflow_m3s": inflow[-1][1] if inflow else None,
                "level_m": lvl[-1][1] if lvl else None,
                "time": (outflow or lvl or [(None, None)])[-1][0],
                "outflow_history": outflow[-24:],
            })
    except Exception as e:  # noqa: BLE001  — the feed must never break the app
        res = {"available": False, "reason": f"CWC unreachable ({type(e).__name__})", "fetched_at": time.time()}
        with _LOCK:
            prev = _CACHE.get(map_id)
        if prev and prev[1].get("available"):
            stale = dict(prev[1])
            stale["stale"] = True
            stale["reason"] = res["reason"]
            return stale
    with _LOCK:
        _CACHE[map_id] = (time.time(), res)
    return res
