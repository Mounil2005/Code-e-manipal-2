#!/usr/bin/env python
"""Build a real-geography map snapshot for resqnet.

Sources (all free):
  * OpenStreetMap (ODbL)  - roads, bridges, rivers, shelter candidates, place names, buildings
  * Copernicus DEM GLO-30 - elevation (30 m), via Microsoft Planetary Computer (no account);
                            falls back to GLO-90 via the Open-Meteo elevation API

The result is a committed JSON snapshot (backend/app/data/maps/<id>.json) so simulation
runs stay deterministic and work offline. Re-run only to refresh or add an area:

    python scripts/build_map.py aluva
    python scripts/build_map.py --list
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import networkx as nx
import numpy as np
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "backend" / "app" / "data" / "maps"
CACHE_DIR = ROOT / "tmp" / "map_cache"
UA = "resqnet-research/0.1 (educational flood-rescue simulation; one-off map snapshot)"

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

AREAS: dict[str, dict] = {
    "aluva": {
        "name": "Aluva · Periyar River",
        "region": "Ernakulam district, Kerala, India",
        "bbox": (10.070, 76.315, 10.145, 76.395),  # S, W, N, E
        "target_land": 78,
        "note": "Aluva sits where the Periyar splits around Manappuram; the town was largely submerged in the August 2018 floods.",
    },
    "chengannur": {
        "name": "Chengannur · Pamba River",
        "region": "Alappuzha district, Kerala, India",
        "bbox": (9.285, 76.560, 9.360, 76.640),
        "target_land": 72,
        "note": "Chengannur and the Pamba/Achankovil basin were among the worst-hit areas in the August 2018 floods.",
    },
    "mahanadi_delta": {
        "id": "odisha_mahanadi_delta",
        "name": "Niali–Kantapara · Kathajodi & Devi",
        "region": "Cuttack district, Odisha, India (Mahanadi delta)",
        "bbox": (20.205, 85.985, 20.305, 86.095),
        "target_land": 80,
        "osdma": True,
        "note": "Delta villages on the Kathajodi and Devi, Mahanadi branches that split off at the Naraj gauge; OSDMA has built flood shelters here because these blocks go under almost every big Mahanadi flood.",
    },
}
# OSDMA's shelter register (osdma.org/shelter-locations), saved as committed JSON
OSDMA_SHELTERS = ROOT / "backend" / "app" / "data" / "sources" / "osdma_shelters.json"
OSDMA_KIND = {"MFS": "flood shelter", "MCS": "cyclone shelter", "NCRMP": "cyclone shelter", "IRCS": "cyclone shelter (Red Cross)"}


def osdma_pois(bbox) -> list[dict]:
    s, w, n, e = bbox
    out = []
    for r in json.loads(OSDMA_SHELTERS.read_text(encoding="utf-8")):
        if s <= r["lat"] <= n and w <= r["lon"] <= e:
            kind = next((v for k, v in OSDMA_KIND.items() if r["shelter"].upper().startswith(k)), "flood shelter")
            out.append({"lat": r["lat"], "lon": r["lon"], "tags": {"amenity": "shelter", "operator": "OSDMA", "name": f"{r['name'].title()} {kind}", "village": r["name"].title(), "osdma_type": r["shelter"]}})
    return out


def area_map_id(area_id: str) -> str:
    return AREAS[area_id].get("id", f"kerala_{area_id}")

HIGHWAY_RANK = {
    "motorway": 0, "motorway_link": 0, "trunk": 1, "trunk_link": 1, "primary": 2, "primary_link": 2,
    "secondary": 3, "secondary_link": 3, "tertiary": 4, "tertiary_link": 4, "unclassified": 5,
    "residential": 6, "living_street": 7,
}
# ground-unit speed multiplier by OSM highway class (national highway vs village lane)
SPEED_BY_RANK = {0: 1.4, 1: 1.4, 2: 1.25, 3: 1.1, 4: 1.0, 5: 0.85, 6: 0.75, 7: 0.6}
# indicative relief-camp capacity by facility type (people); scenarios override S1-S3
SHELTER_CAPACITY = {"school": 60, "college": 80, "university": 80, "community_centre": 50, "townhall": 50, "place_of_worship": 40, "hospital": 30, "social_facility": 30, "marketplace": 40, "relief_camp": 40, "flood_shelter": 80}
# Odisha's multipurpose cyclone/flood shelters (OSDMA) are mapped in OSM as amenity=shelter with a
# name saying so; a bus stop or a hut is also amenity=shelter, so the name decides
DISASTER_SHELTER = ("cyclone", "flood", "mcs", "multipurpose", "multi purpose", "relief")
EXTRA_SHELTERS = 3  # S4..S6 beyond the three that scenarios configure
SHELTER_AMENITIES = {"school", "college", "university", "community_centre", "townhall", "hospital", "place_of_worship", "social_facility", "marketplace"}
PLACE_RANK = {"town": 4, "suburb": 3, "neighbourhood": 3, "quarter": 3, "village": 2, "hamlet": 1, "locality": 1, "city_block": 1}


# ----------------------------------------------------------------- geo helpers
class Proj:
    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = 111.320 * math.cos(math.radians(lat0))
        self.ky = 110.574

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.kx, (self.lat0 - lat) * self.ky)

    def ll(self, x: float, y: float) -> tuple[float, float]:
        return (self.lat0 - y / self.ky, self.lon0 + x / self.kx)


def hav_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742.0 * math.asin(math.sqrt(h))


def poly_len(pts: list[tuple[float, float]]) -> float:
    return sum(hav_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def seg_dist_km(p, a, b, proj: Proj) -> float:
    px, py = proj.xy(*p)
    ax, ay = proj.xy(*a)
    bx, by = proj.xy(*b)
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


# ----------------------------------------------------------------- OSM download
def _post(url: str, query: str, timeout: int = 240) -> bytes:
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def overpass(query: str, label: str, tries: int = 6) -> bytes:
    delay = 8
    for attempt in range(tries):
        for url in OVERPASS:
            try:
                body = _post(url, query)
                if body.lstrip()[:1] in (b"{", b"<") or b"\t" in body[:200] or len(body) < 5:
                    if b"<html" in body[:200].lower() or b"Gateway" in body[:300]:
                        raise RuntimeError("busy")
                    return body
            except Exception as e:  # noqa: BLE001
                print(f"  [{label}] {url.split('/')[2]} -> {type(e).__name__}: {str(e)[:60]}", flush=True)
        print(f"  [{label}] all mirrors busy, retry in {delay}s", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 90)
    raise RuntimeError(f"Overpass unavailable for {label}")


def fetch_overpass(bbox) -> dict:
    s, w, n, e = bbox
    bb = f"({s},{w},{n},{e})"
    hw = "|".join(HIGHWAY_RANK)
    q_net = f'[out:json][timeout:240];(way["highway"~"^({hw})$"]{bb};way["waterway"~"^(river|canal)$"]{bb};);out geom;'
    q_poi = (
        f'[out:json][timeout:120];(nwr["amenity"~"^({"|".join(SHELTER_AMENITIES | {"fire_station", "police"})})$"]["name"]{bb};'
        f'nwr["amenity"="shelter"]["name"]{bb};node["place"]{bb};);out center;'
    )
    q_bld = f'[out:csv(::lat,::lon;false;"\t")][timeout:240];way["building"]{bb};out center;'
    print("downloading network ...", flush=True)
    net = json.loads(overpass(q_net, "network"))
    print(f"  {len(net['elements'])} ways", flush=True)
    print("downloading POIs ...", flush=True)
    poi = json.loads(overpass(q_poi, "poi"))
    print(f"  {len(poi['elements'])} POIs", flush=True)
    print("downloading buildings ...", flush=True)
    bld: list[tuple[float, float]] = []
    try:
        for line in overpass(q_bld, "buildings").decode().splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                try:
                    bld.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    pass
    except Exception as e:  # noqa: BLE001
        print(f"  buildings unavailable ({e}); falling back to road density", flush=True)
    print(f"  {len(bld)} buildings", flush=True)
    ways, pois = [], []
    for el in net["elements"]:
        if el["type"] == "way" and "geometry" in el:
            ways.append({"id": el["id"], "tags": el.get("tags", {}), "nodes": el["nodes"], "geom": [(g["lat"], g["lon"]) for g in el["geometry"]]})
    for el in poi["elements"]:
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat is not None:
            pois.append({"lat": lat, "lon": lon, "tags": el.get("tags", {})})
    return {"ways": ways, "pois": pois, "buildings": bld, "via": "overpass"}


def fetch_osm_api(bbox, step: float = 0.009) -> dict:
    """Fallback when Overpass is saturated: official OSM API, tiled, filtered client-side."""
    s, w, n, e = bbox
    ways, pois, bld = {}, [], []
    seen_nodes: dict[int, tuple[float, float]] = {}
    lats = np.arange(s, n, step)
    lons = np.arange(w, e, step)
    total = len(lats) * len(lons)
    k = 0
    hw = set(HIGHWAY_RANK)
    for la in lats:
        for lo in lons:
            k += 1
            url = f"https://api.openstreetmap.org/api/0.6/map?bbox={lo:.5f},{la:.5f},{min(lo + step, e):.5f},{min(la + step, n):.5f}"
            xml = None
            for attempt in range(5):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=120) as r:
                        xml = r.read()
                    break
                except Exception as ex:  # noqa: BLE001
                    print(f"  tile {k}/{total}: {ex}; retry", flush=True)
                    time.sleep(5 * (attempt + 1))
            if xml is None:
                continue
            print(f"  tile {k}/{total} ({len(xml) // 1024} KiB)", flush=True)
            root = ET.fromstring(xml)
            nodes: dict[int, tuple[float, float, dict]] = {}
            for nd in root.iter("node"):
                tags = {t.get("k"): t.get("v") for t in nd.findall("tag")}
                nodes[int(nd.get("id"))] = (float(nd.get("lat")), float(nd.get("lon")), tags)
            for wy in root.iter("way"):
                tags = {t.get("k"): t.get("v") for t in wy.findall("tag")}
                refs = [int(r.get("ref")) for r in wy.findall("nd")]
                wid = int(wy.get("id"))
                if "building" in tags:
                    pts = [nodes[r][:2] for r in refs if r in nodes]
                    if pts:
                        bld.append((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)))
                    continue
                is_hw = tags.get("highway") in hw
                is_ww = tags.get("waterway") in ("river", "canal")
                am = tags.get("amenity")
                if is_hw or is_ww:
                    if wid in ways or not all(r in nodes for r in refs):
                        continue
                    ways[wid] = {"id": wid, "tags": tags, "nodes": refs, "geom": [nodes[r][:2] for r in refs]}
                elif am and ("name" in tags) and am in SHELTER_AMENITIES | {"fire_station", "police"}:
                    pts = [nodes[r][:2] for r in refs if r in nodes]
                    if pts:
                        pois.append({"lat": pts[0][0], "lon": pts[0][1], "tags": tags})
            for nid, (la_, lo_, tags) in nodes.items():
                if nid in seen_nodes:
                    continue
                seen_nodes[nid] = (la_, lo_)
                if "place" in tags or (tags.get("amenity") in SHELTER_AMENITIES | {"fire_station", "police"} and "name" in tags):
                    pois.append({"lat": la_, "lon": lo_, "tags": tags})
            time.sleep(1.0)
    return {"ways": list(ways.values()), "pois": pois, "buildings": bld, "via": "osm-api"}


def fetch(bbox, area_id: str, via: str) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{area_id}.json"
    if cache.exists():
        print(f"using cached download {cache}")
        return json.loads(cache.read_text(encoding="utf-8"))
    if via == "api":
        data = fetch_osm_api(bbox)
    else:
        try:
            data = fetch_overpass(bbox)
        except Exception as e:  # noqa: BLE001
            print(f"Overpass failed ({e}); falling back to the OSM API", flush=True)
            data = fetch_osm_api(bbox)
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


# ----------------------------------------------------------------- elevation
def fetch_elevation(points: list[tuple[float, float]]) -> list[float]:
    out: list[float] = []
    for i in range(0, len(points), 100):
        chunk = points[i : i + 100]
        url = "https://api.open-meteo.com/v1/elevation?" + urllib.parse.urlencode(
            {"latitude": ",".join(f"{p[0]:.5f}" for p in chunk), "longitude": ",".join(f"{p[1]:.5f}" for p in chunk)}
        )
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    out += [float(v) for v in json.loads(r.read())["elevation"]]
                break
            except Exception as e:  # noqa: BLE001
                print(f"  elevation retry: {e}", flush=True)
                time.sleep(4 * (attempt + 1))
        else:
            raise RuntimeError("elevation service unavailable")
        time.sleep(0.4)
    return out


def fetch_elevation_glo30(points: list[tuple[float, float]]) -> list[float]:
    """Copernicus GLO-30 at each point: median of the 3x3 cells around it (~90 m footprint).
    GLO-30 is a surface model (roofs, canopy), so the median damps single-building spikes."""
    import rasterio
    from rasterio.windows import from_bounds

    pc = "https://planetarycomputer.microsoft.com/api"
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    bbox = [min(lons) - 0.01, min(lats) - 0.01, max(lons) + 0.01, max(lats) + 0.01]
    req = urllib.request.Request(f"{pc}/stac/v1/search", data=json.dumps({"collections": ["cop-dem-glo-30"], "bbox": bbox, "limit": 10}).encode(), headers={"User-Agent": UA, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        feats = json.loads(r.read())["features"]
    out = [float("nan")] * len(points)
    for f in feats:
        href = f["assets"]["data"]["href"]
        u = urllib.parse.urlparse(href)
        with urllib.request.urlopen(urllib.request.Request(f"{pc}/sas/v1/token/{u.netloc.split('.')[0]}/{u.path.split('/')[1]}", headers={"User-Agent": UA}), timeout=30) as r:
            tok = json.loads(r.read())["token"]
        with rasterio.open(href + "?" + tok) as ds:
            win = from_bounds(*bbox, ds.transform).round_offsets().round_lengths()
            z = ds.read(1, window=win, boundless=True, fill_value=np.nan).astype(float)
            tr = ds.window_transform(win)
            for i, (la, lo) in enumerate(points):
                if out[i] == out[i]:
                    continue
                col, row = ~tr * (lo, la)
                r_, c_ = int(row), int(col)
                if 1 <= r_ < z.shape[0] - 1 and 1 <= c_ < z.shape[1] - 1:
                    blk = z[r_ - 1 : r_ + 2, c_ - 1 : c_ + 2]
                    if np.isfinite(blk).any():
                        out[i] = float(np.nanmedian(blk))
    if any(v != v for v in out):
        raise RuntimeError("GLO-30 did not cover every point")
    return out


def worldpop_tiles(bbox, area_id: str, n: int = 5) -> list[tuple[tuple[float, float, float, float], float]]:
    """WorldPop 2018 (100 m, constrained-free 'wpgppop') totals for an n x n grid of tiles over bbox,
    via the free WorldPop stats API. Results are cached because each task takes about a minute."""
    cache = CACHE_DIR / f"{area_id}_worldpop_{n}.json"
    if cache.exists():
        return [(tuple(t), v) for t, v in json.loads(cache.read_text(encoding="utf-8"))]
    s, w, nn, e = bbox
    tiles = []
    for i in range(n):
        for j in range(n):
            ts, tn = s + (nn - s) * i / n, s + (nn - s) * (i + 1) / n
            tw, te = w + (e - w) * j / n, w + (e - w) * (j + 1) / n
            tiles.append((ts, tw, tn, te))
    tasks = []
    for ts, tw, tn, te in tiles:
        gj = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[tw, ts], [te, ts], [te, tn], [tw, tn], [tw, ts]]]}}]}
        url = "https://api.worldpop.org/v1/services/stats?" + urllib.parse.urlencode({"dataset": "wpgppop", "year": 2018, "geojson": json.dumps(gj), "runasync": "true"})
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
            tasks.append(json.loads(r.read())["taskid"])
        time.sleep(0.5)
    print(f"  {len(tasks)} WorldPop tasks submitted; waiting ...", flush=True)
    results: dict[str, float] = {}
    deadline = time.time() + 900
    while len(results) < len(tasks) and time.time() < deadline:
        time.sleep(10)
        for tid in tasks:
            if tid in results:
                continue
            with urllib.request.urlopen(urllib.request.Request(f"https://api.worldpop.org/v1/tasks/{tid}", headers={"User-Agent": UA}), timeout=60) as r:
                st = json.loads(r.read())
            if st.get("status") == "finished" and not st.get("error"):
                results[tid] = float(st["data"]["total_population"])
            elif st.get("error"):
                raise RuntimeError(st.get("error_message"))
        print(f"  {len(results)}/{len(tasks)} tiles done", flush=True)
    if len(results) < len(tasks):
        raise RuntimeError("WorldPop timed out")
    out = [(t, results[tid]) for t, tid in zip(tiles, tasks)]
    cache.write_text(json.dumps(out), encoding="utf-8")
    return out


# ----------------------------------------------------------------- graph building
def build_road_graph(ways: list[dict], river_lines: list[list[tuple[float, float]]], proj: Proj, max_rank: int) -> nx.Graph:
    g = nx.Graph()
    pos: dict[int, tuple[float, float]] = {}
    for w in ways:
        t = w["tags"]
        hw = t.get("highway")
        if hw not in HIGHWAY_RANK or HIGHWAY_RANK[hw] > max_rank:
            continue
        if t.get("area") == "yes" or t.get("access") in ("private", "no") or t.get("highway") == "service":
            continue
        bridge = t.get("bridge") not in (None, "no")
        for (a, b), pa, pb in zip(zip(w["nodes"], w["nodes"][1:]), w["geom"], w["geom"][1:]):
            if a == b:
                continue
            pos[a], pos[b] = pa, pb
            ln = hav_km(pa, pb)
            if g.has_edge(a, b):
                continue
            g.add_edge(a, b, rank=HIGHWAY_RANK[hw], name=t.get("name") or t.get("ref") or "", ref=t.get("ref", ""), bridge=bridge, length=ln, geom=[pa, pb], bname=t.get("name", ""))
    for n in g.nodes:
        g.nodes[n]["pos"] = pos[n]
    comp = max(nx.connected_components(g), key=len)
    return g.subgraph(comp).copy()


def _orient(geom: list, a_pos: tuple[float, float]) -> list:
    return geom if hav_km(geom[0], a_pos) <= hav_km(geom[-1], a_pos) else geom[::-1]


def simplify_chains(g: nx.Graph) -> nx.Graph:
    """Collapse degree-2 nodes into single edges that keep the full road geometry."""
    g = g.copy()
    changed = True
    while changed:
        changed = False
        for n in list(g.nodes):
            if n not in g or g.degree(n) != 2:
                continue
            a, b = list(g.neighbors(n))
            if a == b or g.has_edge(a, b):
                continue
            e1, e2 = g.edges[a, n], g.edges[n, b]
            if e1["bridge"] != e2["bridge"]:
                continue
            p = g.nodes[n]["pos"]
            geom = _orient(e1["geom"], g.nodes[a]["pos"])
            geom2 = _orient(e2["geom"], p)
            if hav_km(geom[-1], p) > 1e-6:
                geom = geom[::-1] if hav_km(geom[0], p) < hav_km(geom[-1], p) else geom
            new = geom + geom2[1:]
            best = e1 if e1["rank"] <= e2["rank"] else e2
            g.add_edge(a, b, rank=best["rank"], name=best["name"] or e1["name"] or e2["name"], ref=best.get("ref", "") or e1.get("ref", "") or e2.get("ref", ""), bridge=e1["bridge"], length=e1["length"] + e2["length"], geom=new, bname=e1["bname"] or e2["bname"])
            g.remove_node(n)
            changed = True
    return g


def prune_stubs(g: nx.Graph, min_km: float = 0.45) -> nx.Graph:
    g = g.copy()
    changed = True
    while changed:
        changed = False
        for n in list(g.nodes):
            if n in g and g.degree(n) == 1:
                (m,) = list(g.neighbors(n))
                if g.edges[n, m]["length"] < min_km and not g.edges[n, m]["bridge"]:
                    g.remove_node(n)
                    changed = True
    return simplify_chains(g)


def contract(g: nx.Graph, target: int, proj: Proj) -> nx.Graph:
    """Merge the closest junction pairs (never across a bridge) until the graph is small enough."""
    g = g.copy()
    guard = 0
    while g.number_of_nodes() > target and guard < 5000:
        guard += 1
        best = None
        for a, b, d in g.edges(data=True):
            if d["bridge"]:
                continue
            # prefer to merge short edges between low-importance junctions
            score = d["length"] * (1.0 + 0.15 * min(d["rank"], 6))
            if best is None or score < best[0]:
                best = (score, a, b)
        if best is None:
            break
        _, a, b = best
        # keep the node with the higher-rank (more important) incident edges
        ra = min((g.edges[a, x]["rank"] for x in g.neighbors(a)), default=9)
        rb = min((g.edges[b, x]["rank"] for x in g.neighbors(b)), default=9)
        keep, drop = (a, b) if (ra, g.degree(a)) <= (rb, g.degree(b)) else (b, a)
        # place merged node halfway, biased to the keeper
        pk, pd = g.nodes[keep]["pos"], g.nodes[drop]["pos"]
        newpos = (pk[0] * 0.65 + pd[0] * 0.35, pk[1] * 0.65 + pd[1] * 0.35)
        for nb in list(g.neighbors(drop)):
            if nb == keep:
                continue
            d = g.edges[drop, nb]
            geom = _orient(d["geom"], pd)  # drop -> nb
            geom = [newpos] + geom[1:]
            if g.has_edge(keep, nb):
                old = g.edges[keep, nb]
                if old["bridge"] and not d["bridge"]:
                    continue
                if (d["bridge"] and not old["bridge"]) or d["length"] < old["length"]:
                    g.remove_edge(keep, nb)
                else:
                    continue
            g.add_edge(keep, nb, rank=d["rank"], name=d["name"], ref=d.get("ref", ""), bridge=d["bridge"], length=poly_len(geom), geom=geom, bname=d["bname"])
        g.remove_node(drop)
        g.nodes[keep]["pos"] = newpos
        for nb in list(g.neighbors(keep)):
            d = g.edges[keep, nb]
            geom = _orient(d["geom"], pk) if hav_km(d["geom"][0], pk) <= hav_km(d["geom"][-1], pk) else d["geom"]
            # re-anchor the end that touches `keep`
            if hav_km(geom[0], newpos) <= hav_km(geom[-1], newpos):
                geom = [newpos] + geom[1:]
            else:
                geom = geom[:-1] + [newpos]
            d["geom"] = geom
            d["length"] = poly_len(geom)
        g = prune_stubs(g, 0.3)
    return g


# ----------------------------------------------------------------- main build
def build(area_id: str, via: str, with_elevation: bool = True) -> dict:
    area = AREAS[area_id]
    bbox = area["bbox"]
    bb_s, bb_w, bb_n, bb_e = bbox
    proj = Proj((bb_s + bb_n) / 2, (bb_w + bb_e) / 2)
    raw = fetch(bbox, area_id, via)
    ways = raw["ways"]
    in_box = lambda lat, lon: bb_s - 0.002 <= lat <= bb_n + 0.002 and bb_w - 0.002 <= lon <= bb_e + 0.002  # noqa: E731

    def navigable(t: dict) -> bool:  # real rivers only: irrigation canals sit above the floodplain
        nm = (t.get("name") or "").lower()
        return t.get("waterway") == "river" and "canal" not in nm and "irrigation" not in nm

    rivers = [wy for wy in ways if navigable(wy["tags"])]
    river_lines = [wy["geom"] for wy in rivers]
    print(f"rivers: {len(rivers)} ways, {sum(poly_len(r) for r in river_lines):.1f} km")

    # ---- roads
    g = None
    for max_rank in (4, 5, 6):
        g0 = build_road_graph([wy for wy in ways if "highway" in wy["tags"]], river_lines, proj, max_rank)
        g1 = prune_stubs(simplify_chains(g0))
        print(f"road classes<= {max_rank}: {g0.number_of_nodes()} raw -> {g1.number_of_nodes()} junctions")
        if g1.number_of_nodes() >= area["target_land"] * 1.25:
            g = g1
            break
        g = g1
    assert g is not None
    # classify bridges: must cross/touch a river, or be long
    for a, b, d in g.edges(data=True):
        if d["bridge"]:
            near_river = any(seg_dist_km(p, r[i], r[i + 1], proj) < 0.035 for p in d["geom"][:: max(1, len(d["geom"]) // 6)] for r in river_lines for i in range(len(r) - 1))
            d["bridge"] = bool(near_river or d["length"] >= 0.18) and d["length"] >= 0.045
    g = contract(g, area["target_land"], proj)
    g = prune_stubs(g, 0.35)
    comp = max(nx.connected_components(g), key=len)
    g = g.subgraph(comp).copy()
    print(f"final road graph: {g.number_of_nodes()} nodes, {g.number_of_edges()} edges, bridges: {sum(1 for *_, d in g.edges(data=True) if d['bridge'])}")

    # ---- stable ids: order nodes north->south, west->east
    order = sorted(g.nodes, key=lambda k: (round(-g.nodes[k]["pos"][0], 3), g.nodes[k]["pos"][1]))
    nid = {k: f"N{i + 1:02d}" for i, k in enumerate(order)}
    land = {nid[k]: g.nodes[k]["pos"] for k in g.nodes}

    # ---- river channel nodes
    rg = nx.Graph()
    rpos: dict[int, tuple[float, float]] = {}
    for wy in rivers:
        for (a, b), pa, pb in zip(zip(wy["nodes"], wy["nodes"][1:]), wy["geom"], wy["geom"][1:]):
            if a != b:
                rg.add_edge(a, b)
                rpos[a], rpos[b] = pa, pb
    river_nodes: dict[str, tuple[float, float]] = {}
    river_edges: list[tuple[str, str, list]] = []
    if rg.number_of_nodes():
        comps = sorted(nx.connected_components(rg), key=lambda c: -sum(1 for _ in c))
        for k in list(rg.nodes):
            la, lo = rpos[k]
            if not in_box(la, lo):
                rg.remove_node(k)
        rg = rg.subgraph(max(nx.connected_components(rg), key=len)).copy()
        sp = 0.55  # km between channel nodes
        # chain-simplify river graph
        for k in list(rg.nodes):
            rg.nodes[k]["pos"] = rpos[k]
        for a, b in rg.edges:
            rg.edges[a, b].update(geom=[rpos[a], rpos[b]], length=hav_km(rpos[a], rpos[b]), rank=0, name="", ref="", bridge=False, bname="")
        rs = simplify_chains(rg)
        samples: dict[tuple[int, int], list[tuple[float, float]]] = {}
        cnt = 0
        rid_of: dict[int, str] = {}

        def rnode(k: int, p: tuple[float, float]) -> str:
            nonlocal cnt
            if k not in rid_of:
                rid_of[k] = f"R{cnt}"
                river_nodes[rid_of[k]] = p
                cnt += 1
            return rid_of[k]

        for a, b, d in sorted(rs.edges(data=True), key=lambda t: (rpos[t[0]][1], rpos[t[1]][1])):
            geom = _orient(d["geom"], rs.nodes[a]["pos"])
            total = poly_len(geom)
            k_seg = max(1, round(total / sp))
            targets = [total * i / k_seg for i in range(k_seg + 1)]
            pts, acc, j = [], 0.0, 0
            for tdist in targets:
                while j < len(geom) - 2 and acc + hav_km(geom[j], geom[j + 1]) < tdist - 1e-9:
                    acc += hav_km(geom[j], geom[j + 1])
                    j += 1
                seg = hav_km(geom[j], geom[j + 1]) or 1e-9
                f = max(0.0, min(1.0, (tdist - acc) / seg))
                pts.append((geom[j][0] + (geom[j + 1][0] - geom[j][0]) * f, geom[j][1] + (geom[j + 1][1] - geom[j][1]) * f))
            ids = [rnode(a, pts[0])]
            for i in range(1, len(pts) - 1):
                rid = f"R{cnt}"
                cnt += 1
                river_nodes[rid] = pts[i]
                ids.append(rid)
            ids.append(rnode(b, pts[-1]))
            for i in range(len(ids) - 1):
                if ids[i] != ids[i + 1]:
                    river_edges.append((ids[i], ids[i + 1], [pts[i], pts[i + 1]]))
        # renumber west->east for stable ids
        remap = {old: f"R{i}" for i, old in enumerate(sorted(river_nodes, key=lambda k: (river_nodes[k][1], -river_nodes[k][0])))}
        river_nodes = {remap[k]: v for k, v in river_nodes.items()}
        river_edges = [(remap[a], remap[b], gm) for a, b, gm in river_edges]
    print(f"river channel: {len(river_nodes)} nodes, {len(river_edges)} edges")

    # ---- jetties: land nodes within reach of the channel, spaced out
    jetties: list[tuple[str, str]] = []
    if river_nodes:
        rids = list(river_nodes)
        rxy = np.array([proj.xy(*river_nodes[r]) for r in rids])
        rtree = cKDTree(rxy)
        cand = []
        for lid, p in land.items():
            d, j = rtree.query(proj.xy(*p))
            if d <= 0.75:
                cand.append((d, lid, rids[j]))
        cand.sort()
        used_xy: list[tuple[float, float]] = []
        for d, lid, rid in cand:
            xy = proj.xy(*land[lid])
            if all(math.hypot(xy[0] - u[0], xy[1] - u[1]) > 0.9 for u in used_xy) and len(jetties) < 16:
                jetties.append((lid, rid))
                used_xy.append(xy)
        if len(jetties) < 3:
            for d, lid, rid in cand[:5]:
                if (lid, rid) not in jetties:
                    jetties.append((lid, rid))
    print(f"jetties: {len(jetties)}")

    # ---- elevation
    all_ids = list(land) + list(river_nodes)
    all_pos = [land[i] if i in land else river_nodes[i] for i in all_ids]
    dem_source = "Copernicus DEM GLO-30 (Planetary Computer), 3x3 median"
    if with_elevation:
        print(f"sampling Copernicus DEM GLO-30 at {len(all_pos)} points ...")
        try:
            elev = fetch_elevation_glo30(all_pos)
        except Exception as e:  # noqa: BLE001
            print(f"  GLO-30 unavailable ({e}); falling back to GLO-90 via Open-Meteo")
            dem_source = "Copernicus DEM GLO-90 via Open-Meteo"
            elev = fetch_elevation(all_pos)
    else:
        elev = [10.0] * len(all_pos)
    emsl = dict(zip(all_ids, elev))
    # Relative height = relief above the local valley floor (5th percentile of the area). An earlier
    # version blended in height-above-nearest-channel; against the Sentinel-1 flood mask of
    # 21 Aug 2018 that blend ranked flooded areas at chance level while plain low ground from the
    # 30 m DEM did measurably better on both maps (scripts/validate_flood.py, docs/flood_validation.md).
    land_el = np.array([emsl[i] for i in land])
    floor = float(np.quantile(land_el, 0.05))
    hand: dict[str, float] = {}
    if river_nodes:
        rkeys = list(river_nodes)
        rtree = cKDTree(np.array([proj.xy(*river_nodes[r]) for r in rkeys]))
        rel_el = np.array([emsl[r] for r in rkeys])
        for lid, p_ in land.items():
            _, js = rtree.query(proj.xy(*p_), k=min(4, len(rkeys)))
            hand[lid] = max(0.0, emsl[lid] - float(np.min(rel_el[np.atleast_1d(js)])))  # kept for reference
    else:
        hand = {lid: max(0.0, emsl[lid] - floor) for lid in land}
    rel = {lid: max(0.0, emsl[lid] - floor) for lid in land}
    rv = np.array(list(rel.values()))
    q = float(np.quantile(rv, 0.34))
    scale = float(np.clip(2.4 / max(q, 0.4), 0.2, 1.2))
    model_elev = {lid: round(float(min(9.5, 1.0 + rel[lid] * scale)), 2) for lid in land}
    print(f"relief q34={q:.1f} m -> vertical scale {scale:.2f}; flood-prone fraction {np.mean([v < 3.4 for v in model_elev.values()]):.0%}")

    # ---- neighbourhoods from OSM place nodes
    places = []
    # rural OSM often has few place nodes; each OSDMA shelter is named after its village
    for p in osdma_pois(bbox) if area.get("osdma") else []:
        places.append((PLACE_RANK["village"], 0, p["tags"]["village"], (p["lat"], p["lon"])))
    for p in raw["pois"]:
        pl = p["tags"].get("place")
        if pl in PLACE_RANK and p["tags"].get("name") and in_box(p["lat"], p["lon"]):
            pop = 0
            try:
                pop = int(str(p["tags"].get("population", "0")).replace(",", ""))
            except ValueError:
                pass
            places.append((PLACE_RANK[pl], pop, p["tags"]["name"], (p["lat"], p["lon"])))
    places.sort(key=lambda t: (-t[0], -t[1], t[2]))
    chosen: list[tuple[str, tuple[float, float]]] = []
    for rank, pop, name, pt in places:
        if all(hav_km(pt, c[1]) > 1.6 for c in chosen):
            chosen.append((name, pt))
        if len(chosen) >= 6:
            break
    if len(chosen) < 4:  # supplement with spatial clusters
        pts = np.array([proj.xy(*p) for p in land.values()])
        rng = np.random.default_rng(3)
        cent = pts[rng.choice(len(pts), 5, replace=False)]
        for _ in range(25):
            lab = np.argmin(((pts[:, None, :] - cent[None]) ** 2).sum(-1), axis=1)
            for k in range(5):
                if (lab == k).any():
                    cent[k] = pts[lab == k].mean(0)
        for k in range(5):
            if len(chosen) >= 5:
                break
            ll = proj.ll(*cent[k])
            if all(hav_km(ll, c[1]) > 1.2 for c in chosen):
                chosen.append((f"Zone {len(chosen) + 1}", ll))
    codes: dict[str, str] = {}
    nbs = []
    for name, pt in chosen:
        base = "".join(ch for ch in name.upper() if ch.isalpha())[:3] or "ZN"
        code = base
        k = 2
        while code in codes.values():
            code = f"{base[:2]}{k}"
            k += 1
        codes[name] = code
    ctree = cKDTree(np.array([proj.xy(*c[1]) for c in chosen]))
    node_nb: dict[str, str] = {}
    for i, p in {**land, **river_nodes}.items():
        _, j = ctree.query(proj.xy(*p))
        node_nb[i] = codes[chosen[j][0]]
    node_nbname = {i: next(n_ for n_, _ in chosen if codes[n_] == c) for i, c in node_nb.items()}
    mean_el = {codes[nm]: float(np.mean([model_elev[i] for i in land if node_nb[i] == codes[nm]] or [5.0])) for nm, _ in chosen}
    ranked = sorted(mean_el, key=lambda c: mean_el[c])
    for r, c in enumerate(ranked):
        nm = next(n_ for n_, _ in chosen if codes[n_] == c)
        pt = next(p_ for n_, p_ in chosen if n_ == nm)
        # one water level for the whole area: on real terrain the DEM already says which ground is
        # low; per-neighbourhood multipliers (a synthetic-city device) only added noise in validation
        nbs.append({"id": c, "name": nm, "flood_factor": 1.0, "lat": round(pt[0], 5), "lng": round(pt[1], 5)})
    nbs.sort(key=lambda d: d["name"])

    # ---- residents per node: WorldPop tile totals, split inside each tile by OSM buildings
    # (each building goes to its nearest junction). Falls back to building counts alone.
    ltree = cKDTree(np.array([proj.xy(*p) for p in land.values()]))
    lkeys = list(land)
    bld = {lid: 0 for lid in land}
    b_node: list[str] = []
    if raw["buildings"]:
        _, idx = ltree.query(np.array([proj.xy(la, lo) for la, lo in raw["buildings"]]))
        b_node = [lkeys[j] for j in idx]
        for k in b_node:
            bld[k] += 1
    else:
        for a, b, d in g.edges(data=True):
            for k in (a, b):
                bld[nid[k]] += int(d["length"] * 40)
    pop_source = "OSM building count per junction (relative weight)"
    pop: dict[str, float] = {k: float(v) for k, v in bld.items()}
    try:
        tiles = worldpop_tiles(bbox, area_id)
        est = {lid: 0.0 for lid in land}
        for (ts, tw, tn, te), tpop in tiles:
            inside = [k for (la, lo), k in zip(raw["buildings"], b_node) if ts <= la < tn and tw <= lo < te] if b_node else []
            if inside:
                share = tpop / len(inside)
                for k in inside:
                    est[k] += share
            else:
                nodes_in = [k for k in land if ts <= land[k][0] < tn and tw <= land[k][1] < te]
                if nodes_in:
                    for k in nodes_in:
                        est[k] += tpop / len(nodes_in)
                else:
                    _, j = ltree.query(proj.xy((ts + tn) / 2, (tw + te) / 2))
                    est[lkeys[j]] += tpop
        pop = est
        pop_source = "WorldPop 2018 (100 m) tile totals, split across junctions by OSM building count"
        print(f"population: {sum(tpop for _, tpop in tiles):,.0f} residents in the area (WorldPop 2018)")
    except Exception as e:  # noqa: BLE001
        print(f"  WorldPop unavailable ({e}); using building counts", flush=True)
    pop = {k: int(round(v)) for k, v in pop.items()}
    total_b = max(1, sum(pop.values()))

    # ---- banks: components of the road graph without bridges
    road_only = nx.Graph()
    road_only.add_nodes_from(land)
    for a, b, d in g.edges(data=True):
        if not d["bridge"]:
            road_only.add_edge(nid[a], nid[b])
    banks = sorted(nx.connected_components(road_only), key=lambda c: -len(c))
    bank_of = {i: bi for bi, c in enumerate(banks) for i in c}

    # ---- shelters: dry, named, spread out; S1 and S3 on the main bank, S2 on the best other bank
    el_cut = float(np.quantile(list(model_elev.values()), 0.45))
    if river_nodes:
        _rt = cKDTree(np.array([proj.xy(*river_nodes[r]) for r in river_nodes]))
        _rk = list(river_nodes)

        def dist_to_river(i: str) -> float:
            return float(_rt.query(proj.xy(*land[i]))[0])

        def nearest_river(i: str) -> str:
            return _rk[int(_rt.query(proj.xy(*land[i]))[1])]
    else:
        dist_to_river = lambda i: 0.0  # noqa: E731
        nearest_river = lambda i: ""  # noqa: E731
    ltree2 = cKDTree(np.array([proj.xy(*p) for p in land.values()]))
    lkeys2 = list(land)
    official_pois = osdma_pois(bbox) if area.get("osdma") else []
    shelter_pois = official_pois + raw["pois"]
    if official_pois:
        print(f"OSDMA shelters in the area: {len(official_pois)}")
    cands = []
    for p in shelter_pois:
        am = p["tags"].get("amenity")
        if am == "shelter" and any(k in (p["tags"].get("name") or "").lower() for k in DISASTER_SHELTER):
            am = "flood_shelter"
        if am in SHELTER_AMENITIES | {"flood_shelter"} and p["tags"].get("name") and in_box(p["lat"], p["lon"]):
            d, j = ltree2.query(proj.xy(p["lat"], p["lon"]))
            node = lkeys2[j]
            rd = dist_to_river(node)
            # purpose-built shelters stand on raised plinths, so low ground does not rule them out
            official = p["tags"].get("operator") == "OSDMA"
            if (official and d <= 1.2) or (d <= 0.7 and model_elev[node] >= el_cut and rd <= 1.4):
                pri = {"flood_shelter": 5 if official else 4, "school": 3, "community_centre": 3, "college": 2, "university": 2, "townhall": 2, "hospital": 1, "place_of_worship": 1, "social_facility": 1, "marketplace": 0}[am]
                cands.append({"node": node, "name": p["tags"]["name"], "amenity": am, "pri": pri, "pop": pop[node], "official": official, "lat": p["lat"], "lon": p["lon"]})
    seen = set()
    cands = [c for c in sorted(cands, key=lambda c: (-c["pri"], -c["pop"], c["name"])) if not (c["node"] in seen or seen.add(c["node"]))]
    main_bank = bank_of[max(land, key=lambda i: pop[i])]
    max_pop = max(1, max(pop.values()))

    def pick(pool, others):
        best, bd = None, -1.0
        for c in pool:
            if c["node"] in [o["node"] for o in others]:
                continue
            md = min([hav_km(land[c["node"]], land[o["node"]]) for o in others] or [9.0])
            sc = min(md, 2.6) + 0.35 * c["pri"] + 1.4 * math.sqrt(c["pop"] / max_pop) - 0.6 * dist_to_river(c["node"])
            if sc > bd:
                best, bd = c, sc
        return best

    main_pool = [c for c in cands if bank_of[c["node"]] == main_bank] or cands
    s1 = pick(main_pool, [])
    s3 = pick(main_pool, [s1] if s1 else [])
    other_pool = [c for c in cands if bank_of[c["node"]] != main_bank] or [c for c in cands if c not in (s1, s3)]
    s2 = pick(other_pool, [x for x in (s1, s3) if x])
    shelters_sel = [x for x in (s1, s2, s3) if x]
    # top up from the dry land nodes when OSM has too few named facilities
    while len(shelters_sel) < 3:
        dry = [i for i in land if model_elev[i] >= el_cut and i not in {c["node"] for c in shelters_sel}]
        far = max(dry, key=lambda i: min([hav_km(land[i], land[c["node"]]) for c in shelters_sel] or [9.0]))
        shelters_sel.append({"node": far, "name": f"Relief camp near {node_nb[far]}", "amenity": "relief_camp", "pri": 0, "pop": pop[far]})
    # additional relief camps (S4..): same rules, anywhere in the area
    pool = [c for c in cands if c["node"] not in {x["node"] for x in shelters_sel}]
    for _ in range(EXTRA_SHELTERS + (2 if official_pois else 0)):
        c = pick(pool, shelters_sel)
        if not c:
            break
        shelters_sel.append(c)
        pool.remove(c)
    shelter_out = [
        {"id": f"S{i + 1}", "node": c["node"], "name": c["name"], "amenity": c["amenity"], "capacity": SHELTER_CAPACITY.get(c["amenity"], 40)}
        | ({"source": "OSDMA", "lat": round(c["lat"], 5), "lng": round(c["lon"], 5)} if c.get("official") else {})
        for i, c in enumerate(shelters_sel)
    ]
    shelter_nodes = {c["node"] for c in shelters_sel}
    if river_nodes:
        linked = {l for l, _ in jetties}
        for sn in sorted(shelter_nodes - linked):
            jetties.append((sn, nearest_river(sn)))
        print(f"shelter landings added; boat-reachable shelters: {sorted(shelter_nodes)} (river distance km: {[round(dist_to_river(x), 2) for x in sorted(shelter_nodes)]})")

    # ---- flood access: floodwater is connected to the river, so low-lying land near the channel
    # opens a boat route once it is actually under water (see GraphEdge.gated)
    gated: list[tuple[str, str]] = []
    if river_nodes:
        linked = {l for l, _ in jetties}
        for lid in sorted(land):
            if lid in linked or lid in shelter_nodes:
                continue
            if model_elev[lid] < 4.3 and dist_to_river(lid) <= 3.0:
                gated.append((lid, nearest_river(lid)))
        print(f"flood-access links: {len(gated)}")

    # ---- bridges: ranked by criticality (extra weighted travel to the shelters if the bridge is lost)
    bridges = [(a, b, d) for a, b, d in g.edges(data=True) if d["bridge"]]
    gw = nx.Graph()
    for a, b, d in g.edges(data=True):
        gw.add_edge(nid[a], nid[b], w=d["length"])
    sh_nodes = sorted(shelter_nodes)

    def travel_cost(graph: nx.Graph) -> float:
        total = 0.0
        dist = {}
        for sn in sh_nodes:
            if sn in graph:
                dist[sn] = nx.single_source_dijkstra_path_length(graph, sn, weight="w")
        for i_, wgt in pop.items():
            if i_ in shelter_nodes:
                continue
            ds = [dist[sn][i_] for sn in dist if i_ in dist[sn]]
            total += (wgt + 5) * (min(ds) if ds else 25.0)  # unreachable = 25 km penalty
        return total

    base_cost = travel_cost(gw)
    crit: dict[tuple, float] = {}
    for a, b, d in bridges:
        h = gw.copy()
        if h.has_edge(nid[a], nid[b]):
            h.remove_edge(nid[a], nid[b])
        crit[(a, b)] = travel_cost(h) - base_cost
    bridges.sort(key=lambda t: (-crit[(t[0], t[1])], t[2]["rank"], -t[2]["length"]))
    critical_bridge = "BR1" if bridges else None
    edges_out: list[dict] = []
    used_names: dict[str, int] = {}
    for bi, (a, b, d) in enumerate(bridges[:8], start=1):
        geom = _orient(d["geom"], g.nodes[a]["pos"])
        mid = geom[len(geom) // 2]
        rname = ""
        best_d = 9.0
        for wy in rivers:
            if wy["tags"].get("name"):
                dd = min(seg_dist_km(mid, wy["geom"][i], wy["geom"][i + 1], proj) for i in range(len(wy["geom"]) - 1))
                if dd < best_d:
                    best_d, rname = dd, wy["tags"]["name"]
        rname = rname.replace(" River", "").replace(" river", "").strip()
        road = (d.get("ref") or d["name"] or "").strip()
        own = (d["bname"] or "").strip()
        if own and "bridge" in own.lower():
            nm = own
        elif rname and road:
            nm = f"{rname} Bridge · {road if len(road) <= 14 else road.split(' - ')[0].split(',')[0]}"
        elif rname:
            nm = f"{rname} Bridge"
        else:
            nm = f"{road or 'River'} bridge"
        used_names[nm] = used_names.get(nm, 0) + 1
        # deck sits above its lower approach: approach height (DEM, model frame) + 0.6 m, clipped
        clearance = round(float(np.clip(min(model_elev.get(nid[a], 3.0), model_elev.get(nid[b], 3.0)) + 0.6, 2.4, 6.0)), 1)
        edges_out.append({"id": f"BR{bi}", "u": nid[a], "v": nid[b], "kind": "bridge", "name": nm, "clearance": clearance, "hw_rank": d["rank"], "speed": SPEED_BY_RANK.get(d["rank"], 1.0), "geometry": [[round(p[0], 5), round(p[1], 5)] for p in geom]})
    # parallel spans of one crossing get numbered north -> south so each is distinguishable
    by_name: dict[str, list[dict]] = defaultdict(list)
    for e in edges_out:
        by_name[e["name"]].append(e)
    for nm, group in by_name.items():
        if len(group) > 1:
            for k, e in enumerate(sorted(group, key=lambda x: -x["geometry"][len(x["geometry"]) // 2][0]), start=1):
                e["name"] = f"{nm} · span {k}"
    bridge_keys = {(min(e["u"], e["v"]), max(e["u"], e["v"])) for e in edges_out}
    for a, b, d in g.edges(data=True):
        key = (min(nid[a], nid[b]), max(nid[a], nid[b]))
        if key in bridge_keys:
            continue
        u, v = sorted((nid[a], nid[b]))
        geom = _orient(d["geom"], land[u])
        edges_out.append({"id": f"E-{u}-{v}", "u": u, "v": v, "kind": "road", "name": d["name"], "clearance": None, "hw_rank": d["rank"], "speed": SPEED_BY_RANK.get(d["rank"], 1.0), "geometry": [[round(p[0], 5), round(p[1], 5)] for p in geom]})
    for a, b, gm in river_edges:
        edges_out.append({"id": f"E-{a}-{b}", "u": a, "v": b, "kind": "river", "name": "", "clearance": None, "hw_rank": 0, "geometry": [[round(p[0], 5), round(p[1], 5)] for p in gm]})
    for lid, rid in jetties:
        u, v = sorted((lid, rid))
        edges_out.append({"id": f"E-{u}-{v}", "u": u, "v": v, "kind": "jetty", "name": "", "clearance": None, "hw_rank": 0, "geometry": [[round(land[lid][0], 5), round(land[lid][1], 5)], [round(river_nodes[rid][0], 5), round(river_nodes[rid][1], 5)]]})

    for lid, rid in gated:
        u, v = sorted((lid, rid))
        edges_out.append({"id": f"E-{u}-{v}", "u": u, "v": v, "kind": "jetty", "name": "flood access", "clearance": None, "gated": True, "hw_rank": 0, "geometry": [[round(land[lid][0], 5), round(land[lid][1], 5)], [round(river_nodes[rid][0], 5), round(river_nodes[rid][1], 5)]]})

    # ---- node labels from the most important adjacent named road
    label = {}
    for a, b, d in g.edges(data=True):
        for k in (nid[a], nid[b]):
            if d["name"] and (k not in label or d["rank"] < label[k][0]):
                label[k] = (d["rank"], d["name"])

    # ---- unit homes
    def spread(pool: list[str], k: int, weights: dict[str, float] | None = None, pos: dict | None = None) -> list[str]:
        pos = pos or {**land, **river_nodes}
        chosen_: list[str] = []
        cur = list(pool)
        while cur and len(chosen_) < k:
            def sc(i):
                md = min([hav_km(pos[i], pos[c]) for c in chosen_] or [3.0])
                return md * (1.0 + (weights or {}).get(i, 0.0))
            nxt = max(cur, key=lambda i: (sc(i), i))
            chosen_.append(nxt)
            cur.remove(nxt)
        return chosen_

    pw = {i: pop[i] / (total_b / max(1, len(land))) for i in land}
    stations = []
    for p in raw["pois"]:
        if p["tags"].get("amenity") in ("fire_station", "police") and in_box(p["lat"], p["lon"]):
            d, j = ltree2.query(proj.xy(p["lat"], p["lon"]))
            if d < 0.8:
                stations.append(lkeys2[j])
    ground_pool = [i for i in land if i not in shelter_nodes]
    ground_homes = list(dict.fromkeys(stations))[:2]
    ground_homes += spread([i for i in ground_pool if i not in ground_homes], 6 - len(ground_homes), pw)
    boat_homes = spread(list(river_nodes), 7) if river_nodes else []
    shelter_bank = bank_of[shelter_out[0]["node"]]
    other_banks = [c for bi, c in enumerate(banks) if bi != shelter_bank and len(c) >= 5]
    far = sorted(other_banks[0]) if other_banks else []
    far_homes = spread([i for i in far if i not in shelter_nodes], 6, pw) if far else []

    nodes_out = []
    for i in sorted(land):
        kind = "shelter" if i in shelter_nodes else "land"
        la, lo = land[i]
        nodes_out.append({"id": i, "lat": round(la, 5), "lng": round(lo, 5), "elev": model_elev[i] + (2.4 if kind == "shelter" else 0.0), "elev_msl": round(emsl[i], 1), "hand": round(hand[i], 1), "nb": node_nb[i], "kind": kind, "label": label.get(i, (0, ""))[1], "pop": pop[i], "bld": bld[i]})
    for i in river_nodes:
        la, lo = river_nodes[i]
        nodes_out.append({"id": i, "lat": round(la, 5), "lng": round(lo, 5), "elev": 0.0, "elev_msl": round(emsl[i], 1), "hand": 0.0, "nb": node_nb[i], "kind": "river", "label": "River channel", "pop": 0})

    return {
        "id": area_map_id(area_id),
        "name": area["name"],
        "region": area["region"],
        "note": area["note"],
        "disclaimer": "Real road, bridge and river geometry from OpenStreetMap with Copernicus DEM terrain. Incidents, reports and flood depths are simulated. Not an operational system.",
        "source": {
            "osm": "OpenStreetMap contributors (ODbL)",
            "dem": dem_source,
            "download": raw.get("via"),
            "built": time.strftime("%Y-%m-%d"),
            "model_elevation": f"1.0 + {scale:.2f} x relief above the valley floor (5th percentile), mapped into the simulator's water-level frame",
            "population": pop_source,
            "bridge_clearance": "lower approach elevation (DEM, model frame) + 0.6 m deck, clipped to 2.4-6.0",
            "road_speed": "ground-unit speed factor by OSM highway class (trunk 1.4 ... residential 0.75)",
            "shelter_capacity": "S1-S3 set by the scenario; S4+ indicative by facility type",
        }
        | ({"shelters": f"OSDMA shelter register (osdma.org/shelter-locations): {sum(1 for c in shelters_sel if c.get('official'))} of {len(shelters_sel)} camps; {len(official_pois)} OSDMA shelters in the area"} if official_pois else {}),
        "population_total": int(sum(pop.values())),
        "bbox": list(bbox),
        "origin": [proj.lat0, proj.lon0],
        "nodes": nodes_out,
        "edges": edges_out,
        "neighbourhoods": nbs,
        "shelters": shelter_out,
        "boat_homes": boat_homes,
        "ground_homes": ground_homes,
        "far_bank": far,
        "far_bank_homes": far_homes,
        "default_boat_node": boat_homes[min(2, len(boat_homes) - 1)] if boat_homes else None,
        "default_ground_node": ground_homes[0] if ground_homes else None,
        "critical_bridge": critical_bridge,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("area", nargs="?", default="aluva")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--via", choices=["overpass", "api"], default="overpass")
    ap.add_argument("--no-elevation", action="store_true")
    a = ap.parse_args()
    if a.list:
        for k, v in AREAS.items():
            print(k, v["name"], v["bbox"])
        return
    snap = build(a.area, a.via, not a.no_elevation)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{area_map_id(a.area)}.json"
    out.write_text(json.dumps(snap, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    kinds = defaultdict(int)
    for e in snap["edges"]:
        kinds[e["kind"]] += 1
    print(f"wrote {out} ({out.stat().st_size // 1024} KiB): {len(snap['nodes'])} nodes, edges {dict(kinds)}, shelters {[s['name'] for s in snap['shelters']]}")


if __name__ == "__main__":
    sys.exit(main())
