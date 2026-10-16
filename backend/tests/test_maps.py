"""Real-geography map sources (Kerala snapshots) alongside the synthetic Riverbend."""
from __future__ import annotations

import math

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from app.analysis.runs import verify_replay
from app.api.main import app
from app.safety.shield import audit_invariants
from app.simulation.engine import Engine
from app.simulation.maps import available_maps, map_ids
from app.simulation.scenarios import PRESETS, get_preset
from app.simulation.state import EdgeKind

REAL = [m for m in map_ids() if m != "riverbend"]
STRATS = ["decentralized", "centralized", "greedy"]


def test_kerala_snapshots_are_present():
    assert "kerala_aluva" in REAL
    assert {m["id"] for m in available_maps()} >= {"riverbend", "kerala_aluva"}


@pytest.mark.parametrize("map_id", REAL)
def test_snapshot_graph_is_sound(map_id):
    w = Engine(get_preset("normal", map=map_id), record_frames=False).world
    assert w.meta["real"] and w.map_id == map_id
    for e in w.edges.values():
        assert e.u in w.nodes and e.v in w.nodes
        assert e.length_km > 0
        # polyline starts and ends exactly on its nodes
        a, b = w.nodes[e.u], w.nodes[e.v]
        assert math.hypot(e.geometry[0][0] - a.x, e.geometry[0][1] - a.y) < 1e-6
        assert math.hypot(e.geometry[-1][0] - b.x, e.geometry[-1][1] - b.y) < 1e-6
    for n in w.nodes.values():
        assert n.neighbourhood in w.neighbourhoods
        assert n.lat and n.lng
    ground = nx.Graph()
    for e in w.edges.values():
        if e.kind in (EdgeKind.ROAD, EdgeKind.BRIDGE):
            ground.add_edge(e.u, e.v)
    assert nx.is_connected(ground), "road network must be one component"
    # every shelter can be reached by road and by boat, so both unit kinds are useful
    boat = nx.Graph()
    for e in w.edges.values():
        if e.kind in (EdgeKind.RIVER, EdgeKind.JETTY):
            boat.add_edge(e.u, e.v)
    for s in w.shelters.values():
        assert s.node in ground
        assert s.node in boat, f"{s.id} has no landing"
    assert w.meta["critical_bridge"] in w.edges
    assert w.edges[w.meta["critical_bridge"]].kind == EdgeKind.BRIDGE
    for rid, r in w.resources.items():
        assert r.node in w.nodes
        assert (w.nodes[r.node].kind == "river") == (r.kind == "boat")


@pytest.mark.parametrize("map_id", REAL)
@pytest.mark.parametrize("key", list(PRESETS))
def test_every_scenario_runs_clean_on_real_map(map_id, key):
    for strat in STRATS:
        e = Engine(get_preset(key, map=map_id, duration=70), strat, record_frames=False)
        while not e.finished:
            e.step()
            assert audit_invariants(e.world) == []


@pytest.mark.parametrize("map_id", REAL)
def test_real_map_is_deterministic_and_replayable(map_id):
    a = Engine(get_preset("triple_failure", map=map_id), "decentralized", record_frames=False)
    b = Engine(get_preset("triple_failure", map=map_id), "decentralized", record_frames=False)
    for _ in range(60):
        a.step()
        b.step()
    assert a.hashes == b.hashes
    e = Engine(get_preset("normal", map=map_id), "decentralized")
    for _ in range(25):
        e.step()
    e.inject("CLOSE_EDGE", {"edge_id": e.world.meta["critical_bridge"], "reason": "test"})
    for _ in range(15):
        e.step()
    assert verify_replay(e)["identical"]


@pytest.mark.parametrize("map_id", REAL)
def test_units_travel_along_real_road_geometry(map_id):
    from app.simulation.serialize import frame

    e = Engine(get_preset("normal", map=map_id), "decentralized", record_frames=False)
    w = e.world
    moved = 0
    for _ in range(60):
        e.step()
        f = frame(e)
        for r in f["resources"]:
            if not r["next_node"]:
                continue
            res = w.resources[r["id"]]
            geom = w.edges[res.edge_id].geometry
            best = min(
                _dist_to_segment((r["x"], r["y"]), a, b) for a, b in zip(geom, geom[1:])
            )
            assert best < 0.002, f"{r['id']} is {best:.4f} km off its road"
            moved += 1
    assert moved > 20


def _dist_to_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def test_incidents_follow_where_people_live():
    # real maps weight incident placement by building density
    w = Engine(get_preset("normal", map="kerala_aluva"), record_frames=False).world
    assert sum(n.pop for n in w.nodes.values()) > 1000
    placed = [w.truth[t]["node"] for t in w.truth]
    assert placed
    assert all(w.nodes[n].kind == "land" for n in placed)


# ---------------------------------------------------------------- API
client = TestClient(app)


def test_api_lists_maps_and_serves_geometry():
    r = client.get("/api/maps")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["maps"]]
    assert "riverbend" in ids and "kerala_aluva" in ids
    w = client.get("/api/world", params={"map": "kerala_aluva"}).json()
    assert w["map"]["real"] is True
    assert w["map"]["projection"]["kx"] > 100
    land = [n for n in w["nodes"] if n["kind"] != "river"]
    assert all("lat" in n and "lng" in n for n in land)
    assert any(e["kind"] == "bridge" and len(e["geometry"]) >= 2 for e in w["edges"])
    syn = client.get("/api/world").json()
    assert syn["map"]["real"] is False
    assert "geometry" not in syn["edges"][0]


def test_api_create_simulation_on_real_map_and_reject_unknown():
    bad = client.post("/api/simulations", json={"scenario": "normal", "map": "atlantis"})
    assert bad.status_code == 400
    r = client.post("/api/simulations", json={"scenario": "normal", "map": "kerala_aluva"})
    assert r.status_code == 200
    body = r.json()
    assert body["map"] == "kerala_aluva"
    sid = body["id"]
    s = client.post(f"/api/simulations/{sid}/step", json={"ticks": 5})
    assert s.status_code == 200 and s.json()["tick"] == 5
    ev = client.post(f"/api/simulations/{sid}/events", json={"type": "CLOSE_EDGE", "params": {"edge_id": "BR1", "reason": "test"}})
    assert ev.status_code == 200


# ---------------------------------------------------------------- real flood event replay
def _levels(map_id, key="kerala_2018", ticks=180):
    from app.simulation.flood import river_level
    from app.simulation.serialize import frame

    e = Engine(get_preset(key, map=map_id), "decentralized", record_frames=False)
    out = []
    for _ in range(ticks):
        e.step()
        f = frame(e)
        out.append((f["river_level"], f["flood_discharge"], f["flood_real_time"]))
    return e, out


@pytest.mark.parametrize("map_id", [m for m in REAL if m.startswith("kerala_")])  # the 2018 replay is Kerala's
def test_flood_event_drives_river_level_from_real_discharge(map_id):
    e, rows = _levels(map_id)
    assert e.world.flood_curve is not None
    levels = [r[0] for r in rows]
    q = [r[1] for r in rows]
    assert all(x is not None for x in q)
    # river level is a monotone function of the replayed discharge
    pairs = sorted(zip(q, levels))
    assert all(a[1] <= b[1] + 1e-6 for a, b in zip(pairs, pairs[1:]))
    # the real peak (16 Aug) is inside the window, with a rise before it and a recession after
    peak = max(range(len(q)), key=q.__getitem__)
    assert 60 < peak < 170
    assert q[peak] > 1.3 * q[0] and q[-1] < q[peak]
    assert rows[0][2].startswith("2018-08-14") and rows[peak][2].startswith("2018-08-16")


def test_flood_event_falls_back_to_synthetic_ramp_without_data():
    e, rows = _levels("riverbend", ticks=20)
    assert e.world.flood_curve is None
    assert rows[0][1] is None and rows[0][2] is None


@pytest.mark.parametrize("map_id", REAL)
def test_flood_access_links_open_only_when_land_is_flooded(map_id):
    e = Engine(get_preset("kerala_2018", map=map_id), "decentralized", record_frames=False)
    gated = [x for x in e.world.edges.values() if x.gated]
    assert gated
    for _ in range(110):
        e.step()
    for x in gated:
        land_depth = max(e.world.nodes[x.u].depth, e.world.nodes[x.v].depth)
        assert x.boat_ok == (land_depth >= 0.5)
    assert any(x.boat_ok for x in gated), "flood should have opened some access links by the peak"


def test_sos_arrivals_follow_rainfall():
    # arrivals are drawn proportional to ERA5 rainfall, so the heaviest-rain third gets the most
    cfg = get_preset("kerala_2018", map="kerala_aluva", arrival_incidents=60, seed=11)
    e = Engine(cfg, "decentralized", record_frames=False)
    ticks = [r.tick for r in e.world.pending_reports if r.tick > 0]
    from app.simulation.events import load_event

    rain = load_event("kerala_2018", "kerala_aluva", cfg.duration)["rain"]
    heavy = sum(rain[t] for t in ticks) / len(ticks)
    uniform = sum(rain[3 : cfg.arrival_window + 1]) / len(rain[3 : cfg.arrival_window + 1])
    assert heavy > uniform


# ---------------------------------------------------------------- operational realism
@pytest.mark.parametrize("map_id", REAL)
def test_road_class_sets_ground_speed(map_id):
    from app.routing.router import edge_minutes

    w = Engine(get_preset("normal", map=map_id), record_frames=False).world
    roads = [e for e in w.edges.values() if e.kind == "road"]
    assert len({e.speed for e in roads}) >= 3, "several road classes expected"
    fast = max(roads, key=lambda e: e.speed)
    slow = min(roads, key=lambda e: e.speed)
    fast.depth = slow.depth = 0.0  # compare class alone, without flood slow-down
    # same length, different class -> different time
    per_km = lambda e: edge_minutes(e, "ground", 28.0) / e.length_km  # noqa: E731
    assert per_km(fast) < per_km(slow)


@pytest.mark.parametrize("map_id", REAL)
def test_bridge_clearances_come_from_terrain(map_id):
    w = Engine(get_preset("normal", map=map_id), record_frames=False).world
    br = [e for e in w.edges.values() if e.kind == "bridge"]
    assert all(2.4 <= e.clearance <= 6.0 for e in br)
    ground = lambda n: n.elevation - (2.4 if n.kind == "shelter" else 0.0)  # noqa: E731  (shelters sit on raised ground)
    for e in br:  # deck above its lower approach
        assert e.clearance >= min(ground(w.nodes[e.u]), ground(w.nodes[e.v])) + 0.5 or e.clearance == 6.0
    assert len({e.clearance for e in br}) >= 2 or len(br) < 2


@pytest.mark.parametrize("map_id", REAL)
def test_extra_shelters_open_except_in_capacity_stress_tests(map_id):
    normal = Engine(get_preset("normal", map=map_id), record_frames=False).world
    assert len(normal.shelters) >= 4
    assert all(s.capacity > 0 for s in normal.shelters.values())
    for key in ("bridge_dependency", "shelter_overflow"):
        assert len(Engine(get_preset(key, map=map_id), record_frames=False).world.shelters) == 3


@pytest.mark.parametrize("map_id", REAL)
def test_population_is_worldpop_scale(map_id):
    from app.simulation.maps import load_snapshot

    snap = load_snapshot(map_id)
    assert "WorldPop" in snap["source"]["population"]
    assert 20_000 < snap["population_total"] < 2_000_000
    w = Engine(get_preset("normal", map=map_id), record_frames=False).world
    assert abs(sum(n.pop for n in w.nodes.values()) - snap["population_total"]) <= len(w.nodes)
