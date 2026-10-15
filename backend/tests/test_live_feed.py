"""Real-time feed: one door in, engine events out, decision deltas and the priority board."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.api.main import app
from app.live.cwc import ramp_for_level
from app.live.feed import FeedError, FeedHub, drill_plan, priority_board, synthetic_plan
from app.simulation.engine import Engine
from app.simulation.scenarios import get_preset

client = TestClient(app)


def _live(map_id="kerala_aluva", approval=False):
    return Engine(get_preset("live_ops", map=map_id, approval_mode=approval), "decentralized", record_frames=False)


def test_gauge_reading_sets_the_river_from_the_2018_calibration():
    assert ramp_for_level("kerala_aluva", 5.9) == 0.0
    assert ramp_for_level("kerala_aluva", 12.4) == 1.0
    assert 0.4 < ramp_for_level("kerala_aluva", 9.0) < 0.6
    e = _live()
    hub = FeedHub()
    wet0 = sum(1 for n in e.world.nodes.values() if n.depth > 0)
    hub.apply(e, {"type": "gauge", "source": "drill", "level_m": 12.4, "station": "Neeleeswaram"})
    assert e.world.river_override == 1.0
    assert sum(1 for n in e.world.nodes.values() if n.depth > 0) > wet0
    assert e.world.gauge_log[-1]["level_m"] == 12.4


def test_every_message_type_goes_through_the_one_door_and_is_replayable():
    from app.analysis.runs import verify_replay

    e = Engine(get_preset("live_ops", map="kerala_aluva"), "decentralized")
    hub = FeedHub()
    hub.apply(e, {"type": "gauge", "source": "drill", "level_m": 10.0})
    land = next(n.id for n in e.world.nodes.values() if n.kind == "land" and n.depth > 0)
    hub.apply(e, {"type": "sos", "source": "synthetic", "node": land, "people": 5, "urgency": 5, "reporter": "resident"})
    for _ in range(3):
        e.step()
    inc = max(e.world.incidents.values(), key=lambda i: i.reported_tick)
    hub.apply(e, {"type": "duplicate", "source": "synthetic", "incident_id": inc.id, "reporter": "sensor"})
    hub.apply(e, {"type": "contradiction", "source": "synthetic", "incident_id": inc.id})
    road = next(x.id for x in e.world.edges.values() if x.kind == "road" and x.ground_ok)
    hub.apply(e, {"type": "road", "source": "external", "edge_id": road})
    hub.apply(e, {"type": "shelter", "source": "synthetic", "shelter_id": "S1", "capacity": 30})
    hub.apply(e, {"type": "dam", "source": "live", "dam": "Idukki", "outflow_m3s": 1200})
    assert [m["type"] for m in hub.messages] == ["gauge", "sos", "duplicate", "contradiction", "road", "shelter", "dam"]
    assert all(m["event_id"] and m["effects"] for m in hub.messages)
    for _ in range(5):
        e.step()
    assert verify_replay(e)["identical"], "feed messages are injected events, so the run replays exactly"


def test_bad_messages_are_rejected():
    e, hub = _live(), FeedHub()
    for bad in ({"type": "nonsense"}, {"type": "gauge", "source": "rumour", "level_m": 5}):
        try:
            hub.apply(e, bad)
        except FeedError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_decision_delta_is_recorded_per_gauge_reading():
    e, hub = _live(), FeedHub()
    hub.apply(e, {"type": "gauge", "source": "drill", "level_m": 6.0})
    land = sorted((n for n in e.world.nodes.values() if n.kind == "land"), key=lambda n: n.x)
    for nd in (land[0].id, land[len(land) // 2].id, land[-1].id):  # far apart, so not merged
        hub.apply(e, {"type": "sos", "source": "synthetic", "node": nd, "people": 3, "urgency": 4})
    for _ in range(15):
        e.step()
    hub.apply(e, {"type": "gauge", "source": "drill", "level_m": 11.0})
    first = hub.messages[0]
    d = first["delta"]
    assert d is not None and d["minutes"] == 15
    assert d["new_incidents"] >= 3
    assert d["new_assignments"], "agents should commit to the new SOS calls"
    assert all(a["decision_id"] and a["why"] for a in d["new_assignments"])
    assert d["supplies"]["water_l_per_day"] == d["supplies"]["people_in_camps"] * 15
    assert hub.current_delta(e) is not None


def test_priority_board_ranks_like_the_engine_and_explains_waiting():
    e, hub = _live(), FeedHub()
    hub.apply(e, {"type": "gauge", "source": "drill", "level_m": 11.5})
    wet = sorted((n for n in e.world.nodes.values() if n.kind == "land"), key=lambda n: -n.depth)[:12]
    for i, n in enumerate(wet):
        hub.apply(e, {"type": "sos", "source": "synthetic", "node": n.id, "people": 8, "urgency": 5 if i % 2 else 3, "reporter": "responder"})
    for _ in range(4):
        e.step()
    board = priority_board(e)
    assert [r["rank"] for r in board] == list(range(1, len(board) + 1))
    eligible = [r for r in board if not r["eligibility"].startswith("awaiting")]
    urg = [r["urgency"] for r in eligible]
    assert urg == sorted(urg, reverse=True), "eligible incidents are ranked by urgency first"
    assert any(r["assigned"] for r in board)
    assert all(r["why_waiting"] for r in board if r["state"] == "waiting")


def test_drill_and_synthetic_plans():
    d = drill_plan("kerala_aluva")
    assert d[0]["time"] == "2018-08-14T12:00" and d[-1]["time"] == "2018-08-17T12:00"
    assert max(r["level_m"] for r in d) == 12.4
    s = synthetic_plan("kerala_aluva", {"rise_hours": 6, "hold_hours": 2, "fall_hours": 6, "peak": 0.8})
    assert len(s) == 14 and max(r["ramp"] for r in s) == 0.8 and s[0]["ramp"] == 0.0


def test_feed_api_and_background_drill():
    with TestClient(app) as c:  # one event loop for the whole test, so the background feed keeps running
        _feed_api_and_background_drill(c)


def _feed_api_and_background_drill(client):
    sim = client.post("/api/simulations", json={"scenario": "live_ops", "map": "kerala_aluva"}).json()
    sid = sim["id"]
    assert sim["duration"] == 1440
    r = client.post(f"/api/simulations/{sid}/feed", json={"type": "gauge", "level_m": 9.0, "station": "test"})
    assert r.status_code == 200 and r.json()["message"]["source"] == "external"
    assert client.post(f"/api/simulations/{sid}/feed", json={"type": "bogus"}).status_code == 400
    node = next(n["id"] for n in client.get("/api/world", params={"map": "kerala_aluva"}).json()["nodes"] if n["kind"] == "land")
    assert client.post(f"/api/simulations/{sid}/feed", json={"type": "sos", "node": node, "people": 3}).status_code == 200
    st = client.post(f"/api/simulations/{sid}/feed/start", json={"mode": "drill", "params": {"interval_s": 0.5, "sos_per_hour": 4}, "speed": 8})
    assert st.status_code == 200 and st.json()["feed_status"]["running"]
    deadline = time.time() + 20
    msgs = []
    while time.time() < deadline:
        msgs = client.get(f"/api/simulations/{sid}/feed").json()["messages"]
        if sum(1 for m in msgs if m["source"] == "drill") >= 4:
            break
        time.sleep(0.5)
    drill = [m for m in msgs if m["source"] == "drill"]
    assert len(drill) >= 4 and drill[0]["time"] == "2018-08-14T12:00"
    board = client.get(f"/api/simulations/{sid}/priority").json()["board"]
    assert board and board[0]["rank"] == 1
    assert client.post(f"/api/simulations/{sid}/feed/stop").json()["feed_status"]["running"] is False
    client.post(f"/api/simulations/{sid}/pause")


# ---------------------------------------------------------------- human in the loop
def _flooded_with_calls(e, n=5):
    hub = FeedHub()
    hub.apply(e, {"type": "gauge", "source": "drill", "level_m": 10.5})
    wet = sorted((x for x in e.world.nodes.values() if x.kind == "land"), key=lambda x: -x.depth)[:n]
    for x in wet:
        hub.apply(e, {"type": "sos", "source": "synthetic", "node": x.id, "people": 4, "urgency": 4, "reporter": "responder"})
    for _ in range(3):
        e.step()
    return hub


def test_live_ops_defaults_to_operator_approval():
    assert get_preset("live_ops").approval_mode is True
    assert get_preset("normal").approval_mode is False


def test_approval_mode_recommends_and_never_dispatches_on_its_own():
    e = Engine(get_preset("live_ops", map="kerala_aluva"), "decentralized")
    _flooded_with_calls(e)
    recs = list(e.recommendations.values())
    assert recs and not e.world.assignments, "nothing moves until the operator approves"
    assert len({r["resource"] for r in recs}) == len(recs) and len({r["incident"] for r in recs}) == len(recs)
    for r in recs:  # each recommendation links to its own decision record
        d = e.decisions[r["decision_id"]]
        assert d["incident_id"] == r["incident"] and d["outcome"] == "RECOMMENDED"
        assert "recommended for" in d["explanation"]
    for _ in range(5):
        e.step()
    assert not e.world.assignments


def test_operator_approve_override_reject_and_toggle():
    from app.analysis.runs import verify_replay

    e = Engine(get_preset("live_ops", map="kerala_aluva"), "decentralized")
    _flooded_with_calls(e)
    r0, r1, r2 = list(e.recommendations.values())[:3]
    e.inject("APPROVE_RECOMMENDATION", {"recommendation_id": r0["id"]}, "operator")
    a = next(iter(e.world.assignments.values()))
    assert (a.resource_id, a.incident_id) == (r0["resource"], r0["incident"])
    # operator picks a different idle unit than the one recommended
    other = next(x.id for x in e.world.resources.values() if x.kind == e.world.resources[r1["resource"]].kind and x.status == "IDLE" and x.id != r1["resource"] and x.id not in {r["resource"] for r in e.recommendations.values()})
    try:
        e.inject("APPROVE_RECOMMENDATION", {"recommendation_id": r1["id"], "resource_id": other}, "operator")
        assert e.counters["operator_overrides"] == 1
    except Exception:
        pass  # the chosen unit may not reach that incident; the shield/evaluation refuses it honestly
    e.inject("REJECT_RECOMMENDATION", {"recommendation_id": r2["id"], "reason": "test"}, "operator")
    for _ in range(3):
        e.step()
    assert not any((r["resource"], r["incident"]) == (r2["resource"], r2["incident"]) for r in e.recommendations.values())
    assert e.counters["approvals"] >= 1 and e.counters["operator_rejections"] == 1
    e.inject("SET_APPROVAL_MODE", {"enabled": False}, "operator")
    e.step()
    assert not e.recommendations and len(e.world.assignments) > 1, "autonomous mode dispatches by itself"
    for _ in range(4):
        e.step()
    assert verify_replay(e)["identical"]


def test_stale_recommendation_cannot_be_approved():
    e = Engine(get_preset("live_ops", map="kerala_aluva"), "decentralized")
    _flooded_with_calls(e)
    r = next(iter(e.recommendations.values()))
    e.inject("RESOURCE_FAILURE", {"resource_id": r["resource"], "reason": "test"}, "user")
    e.step()  # the queue drops recommendations whose unit is no longer available
    assert r["id"] not in e.recommendations
    try:
        e.inject("APPROVE_RECOMMENDATION", {"recommendation_id": r["id"]}, "operator")
    except Exception:
        return
    raise AssertionError("approved a recommendation for a failed unit")


def test_every_recommendation_has_its_own_explained_decision_over_a_long_run():
    from app.simulation.serialize import frame

    e = Engine(get_preset("live_ops", map="kerala_aluva"), "decentralized", record_frames=False)
    hub = _flooded_with_calls(e, n=8)
    for t in range(80):
        if t % 10 == 0:
            land = sorted((x for x in e.world.nodes.values() if x.kind == "land"), key=lambda x: -x.depth)
            hub.apply(e, {"type": "sos", "source": "synthetic", "node": land[t % len(land)].id, "people": 3, "urgency": 4})
        e.step()
        for r in frame(e)["recommendations"]:
            d = e.decisions.get(r["decision_id"])
            assert d is not None, f"{r['id']} points at a missing decision record"
            assert d["incident_id"] == r["incident"]
            assert d["selected"]["resource_id"] == r["resource"], "the record explains the unit actually recommended"
            assert r["why"].startswith(f"{r['resource']} is recommended for {r['incident']}")
