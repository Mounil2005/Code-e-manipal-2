"""Real-time feed pipeline.

Every message (river gauge reading, SOS call, duplicate or contradictory report, road report,
camp update, dam release) enters through one door, `FeedHub.apply`, and becomes an ordinary
engine event. The product therefore cannot tell a drill from a live day, and every run stays
event-sourced and replayable.

Sources that feed the door:
  * live   - CWC telemetry, polled (river gauge + upstream reservoirs); real
  * drill  - the real August 2018 CWC gauge record, replayed one hourly reading at a time
  * synthetic - a parameterised flood curve
SOS calls in drill/synthetic mode are synthetic (no public live SOS feed exists), generated from
the flooded population and the rainfall; every message carries its source label.

After each gauge reading the hub snapshots the state; the "decision delta" for that reading is
what changed until the next one: roads closed or reopened, routes re-planned (old vs new km),
commitments made, cancelled or reassigned, camp occupancy, and the supplies implied.
"""
from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path
from typing import Any, Optional

from ..negotiation.common import is_eligible, priority_key, unassigned_people
from ..routing.router import path_edges
from ..simulation.state import ACTIVE_ASSIGNMENT, AssignmentStatus, EdgeKind, IncidentStatus, ResourceStatus
from .cwc import STATIONS, latest as cwc_latest

DATA = Path(__file__).resolve().parent.parent / "data" / "events"
# Sphere Handbook minimum standards: 15 L of water per person per day (all uses); one dry-ration
# kit stands in for ~2,100 kcal per person per day.
WATER_L_PER_DAY = 15
KITS_PER_DAY = 1

MESSAGE_TYPES = ("gauge", "sos", "duplicate", "contradiction", "road", "shelter", "dam")
SOURCES = ("live", "drill", "synthetic", "external")


def _km(world: Any, path: list[str]) -> float:
    return round(sum(e.length_km for e in path_edges(world, path)), 2)


class FeedError(ValueError):
    pass


class FeedHub:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.seq = 0
        self.mode: Optional[str] = None  # live | drill | synthetic
        self.running = False
        self.params: dict[str, Any] = {}
        self.status_note = ""
        self._snap: Optional[dict[str, Any]] = None
        self._snap_msg: Optional[int] = None
        self._sent = 0
        self._dirty: set[int] = set()  # messages whose decision delta was filled in after sending

    # ------------------------------------------------------------------ the one door
    def apply(self, engine: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """Validate a feed message, turn it into an engine event, log it. Raises FeedError."""
        t = msg.get("type")
        src = msg.get("source") or "external"
        if t not in MESSAGE_TYPES:
            raise FeedError(f"type must be one of {MESSAGE_TYPES}")
        if src not in SOURCES:
            raise FeedError(f"source must be one of {SOURCES}")
        w = engine.world
        if t == "gauge":
            # close the previous reading's decision window before the river changes again
            self._close_window(engine)
            snap = snapshot(engine)
            ev = engine.inject("GAUGE_READING", {k: msg[k] for k in ("level_m", "ramp", "station", "time") if k in msg} | {"source": src}, source=f"feed:{src}")
            self._snap = snap
        elif t == "sos":
            node = msg.get("node") or nearest_land_node(w, msg.get("x"), msg.get("y"))
            ev = engine.inject("NEW_INCIDENT", {"node": node, "people": msg.get("people", 3), "urgency": msg.get("urgency", 3), "source": msg.get("reporter", "resident")}, source=f"feed:{src}")
        elif t == "duplicate":
            ev = engine.inject("REPORT_DUPLICATE", {"incident_id": msg.get("incident_id"), "source": msg.get("reporter", "resident")}, source=f"feed:{src}")
        elif t == "contradiction":
            ev = engine.inject("CONTRADICTORY_REPORT", {"incident_id": msg.get("incident_id"), "claim": "NO_VICTIMS", "source": msg.get("reporter", "resident")}, source=f"feed:{src}")
        elif t == "road":
            ev = engine.inject("CLOSE_EDGE", {"edge_id": msg.get("edge_id"), "reason": msg.get("reason") or "field report"}, source=f"feed:{src}")
        elif t == "shelter":
            ev = engine.inject("SHELTER_CAPACITY", {"shelter_id": msg.get("shelter_id"), "capacity": msg.get("capacity"), "reason": msg.get("reason") or "camp report"}, source=f"feed:{src}")
        else:  # dam
            ev = engine.inject("DAM_RELEASE", {"dam": msg.get("dam"), "outflow_m3s": msg.get("outflow_m3s")}, source=f"feed:{src}")
        self.seq += 1
        rec = {
            "seq": self.seq,
            "received_at": time.time(),
            "tick": w.tick,
            "type": t,
            "source": src,
            "label": msg.get("label") or "",
            "time": msg.get("time"),
            "payload": {k: v for k, v in msg.items() if k not in ("type", "source", "label")},
            "event_id": ev.id,
            "effects": list(ev.effects),
            "delta": None,
        }
        self.messages.append(rec)
        self.messages = self.messages[-600:]
        if t == "gauge":
            self._snap_msg = rec["seq"]
        return rec

    def _close_window(self, engine: Any) -> None:
        if self._snap is None or self._snap_msg is None:
            return
        for m in reversed(self.messages):
            if m["seq"] == self._snap_msg:
                m["delta"] = diff(engine, self._snap)
                if m["seq"] <= self._sent:
                    self._dirty.add(m["seq"])
                break
        self._snap = None
        self._snap_msg = None

    def current_delta(self, engine: Any) -> Optional[dict[str, Any]]:
        """Changes since the latest gauge reading (still accumulating)."""
        return diff(engine, self._snap) if self._snap is not None else None

    def unsent(self) -> list[dict[str, Any]]:
        out = [m for m in self.messages if m["seq"] > self._sent or m["seq"] in self._dirty]
        self._dirty.clear()
        if out:
            self._sent = max(self._sent, out[-1]["seq"])
        return out

    def status(self, engine: Any) -> dict[str, Any]:
        return {"mode": self.mode, "running": self.running, "params": self.params, "note": self.status_note, "count": self.seq, "current_delta": self.current_delta(engine), "gauge": engine.world.gauge_log[-1] if engine.world.gauge_log else None}


# ---------------------------------------------------------------------- what changed
def snapshot(engine: Any) -> dict[str, Any]:
    w = engine.world
    routes = {}
    for r in w.resources.values():
        if r.assignment_id:
            routes[r.assignment_id] = _km(w, engine.remaining_path(r))
    return {
        "tick": w.tick,
        "ground_closed": {e.id for e in w.edges.values() if e.kind in (EdgeKind.ROAD, EdgeKind.BRIDGE) and not e.ground_ok},
        "boat_streets": {e.id for e in w.edges.values() if e.kind == EdgeKind.ROAD and e.boat_ok},
        "assign": {a.id: (a.status, a.resource_id, a.incident_id, a.shelter_id, a.reroutes) for a in w.assignments.values()},
        "routes": routes,
        "shelters": {s.id: (s.occupancy, s.reserved(w), s.capacity) for s in w.shelters.values()},
        "rescued": sum(i.people_rescued for i in w.incidents.values()),
        "incidents": set(w.incidents),
    }


def diff(engine: Any, snap: dict[str, Any]) -> dict[str, Any]:
    w = engine.world
    name = lambda eid: (w.edges[eid].name or eid)  # noqa: E731
    closed_now = {e.id for e in w.edges.values() if e.kind in (EdgeKind.ROAD, EdgeKind.BRIDGE) and not e.ground_ok}
    boat_now = {e.id for e in w.edges.values() if e.kind == EdgeKind.ROAD and e.boat_ok}
    new_closed = sorted(closed_now - snap["ground_closed"])
    reopened = sorted(snap["ground_closed"] - closed_now)
    rerouted, new_assign, cancelled = [], [], []
    for a in w.assignments.values():
        before = snap["assign"].get(a.id)
        if before is None:
            dec = engine.decisions.get(a.decision_id) or {}
            new_assign.append({"assignment": a.id, "resource": a.resource_id, "incident": a.incident_id, "shelter": a.shelter_id, "people": a.people, "decision_id": a.decision_id, "why": (dec.get("explanation") or "")[:260]})
            continue
        if a.reroutes > before[4]:
            r = w.resources.get(a.resource_id)
            new_km = _km(w, engine.remaining_path(r)) if r and r.assignment_id == a.id else None
            rerouted.append({"assignment": a.id, "resource": a.resource_id, "incident": a.incident_id, "old_km": snap["routes"].get(a.id), "new_km": new_km})
        if before[0] in ACTIVE_ASSIGNMENT and a.status == AssignmentStatus.CANCELLED:
            cancelled.append({"assignment": a.id, "resource": a.resource_id, "incident": a.incident_id, "reason": a.cancel_reason})
    reassigned = []
    for c in cancelled:
        took = [x for x in new_assign if x["incident"] == c["incident"] and x["resource"] != c["resource"]]
        for x in took:
            reassigned.append({"incident": c["incident"], "from": c["resource"], "to": x["resource"]})
    camps, arrivals, in_camps = [], 0, 0
    for s in w.shelters.values():
        occ0, res0, cap0 = snap["shelters"].get(s.id, (s.occupancy, 0, s.capacity))
        rsv = s.reserved(w)
        in_camps += s.occupancy
        d_occ = s.occupancy - occ0
        arrivals += max(0, d_occ)
        if d_occ or rsv != res0 or s.capacity != cap0:
            camps.append({"shelter": s.id, "name": s.name, "occupancy": s.occupancy, "occ_delta": d_occ, "reserved": rsv, "reserved_delta": rsv - res0, "capacity": s.capacity, "full": s.occupancy + rsv >= s.capacity})
    return {
        "since_tick": snap["tick"],
        "minutes": w.tick - snap["tick"],
        "roads_closed": [name(x) for x in new_closed][:12],
        "roads_closed_count": len(new_closed),
        "roads_reopened_count": len(reopened),
        "boat_streets_opened": len(boat_now - snap["boat_streets"]),
        "rerouted": rerouted,
        "new_assignments": new_assign,
        "cancelled": cancelled,
        "reassigned": reassigned,
        "camps": camps,
        "supplies": {
            "people_in_camps": in_camps,
            "new_arrivals": arrivals,
            "water_l_per_day": in_camps * WATER_L_PER_DAY,
            "ration_kits_per_day": in_camps * KITS_PER_DAY,
            "added_water_l_per_day": arrivals * WATER_L_PER_DAY,
            "added_kits_per_day": arrivals * KITS_PER_DAY,
            "standard": "Sphere minimum: 15 L water and ~2,100 kcal (one ration kit) per person per day",
        },
        "rescued": sum(i.people_rescued for i in w.incidents.values()) - snap["rescued"],
        "new_incidents": len(set(w.incidents) - snap["incidents"]),
    }


# ---------------------------------------------------------------------- priority board
def priority_board(engine: Any, limit: int = 40) -> list[dict[str, Any]]:
    """Every open incident ranked the way the engine ranks it, with who is helping and, for the
    rest, why they are still waiting (taken from the engine's own decision records)."""
    w = engine.world
    latest_dec: dict[str, dict[str, Any]] = {}
    for d in reversed(list(engine.decisions.values())):
        latest_dec.setdefault(d["incident_id"], d)
    idle = [r for r in w.resources.values() if r.status == ResourceStatus.IDLE and r.load == 0]
    rows = []
    for inc in w.incidents.values():
        if inc.status in (IncidentStatus.RESCUED, IncidentStatus.FALSE_ALARM):
            continue
        ok, rule = is_eligible(w, inc)
        active = [a for a in w.assignments.values() if a.incident_id == inc.id and a.status in ACTIVE_ASSIGNMENT]
        left = unassigned_people(w, inc)
        reports = [w.reports[x] for x in inc.report_ids if x in w.reports]
        dec = latest_dec.get(inc.id)
        if active and left == 0:
            state, why = "assigned", ""
        elif not ok:
            state, why = "waiting", f"not dispatched yet: {rule}"
        elif not idle:
            nxt = min((a.eta_dropoff_tick for a in w.assignments.values() if a.status in ACTIVE_ASSIGNMENT), default=None)
            state, why = "waiting", "every boat and team is busy" + (f"; the next frees up around minute {nxt}" if nxt else "")
        elif dec and dec.get("outcome") != "COMMITTED":
            state, why = "waiting", dec.get("explanation") or "no feasible unit yet"
        else:
            state, why = "waiting", "eligible; being negotiated this round"
        sel = (dec or {}).get("selected") or {}
        rows.append({
            "incident": inc.id,
            "node": inc.node,
            "neighbourhood": w.neighbourhoods[inc.neighbourhood].name if inc.neighbourhood in w.neighbourhoods else inc.neighbourhood,
            "people": inc.people_estimate,
            "unassigned": left,
            "urgency": inc.urgency,
            "deadline_in": inc.deadline_tick - w.tick,
            "confidence": round(inc.confidence, 2),
            "status": inc.status,
            "eligibility": rule,
            "reports": len(reports),
            "sources": sorted({r.source for r in reports}),
            "contradicted": any(r.claim == "NO_VICTIMS" for r in reports),
            "state": state if not (active and left > 0) else "partly assigned",
            "why_waiting": why,
            "assigned": [
                {
                    "resource": a.resource_id,
                    "shelter": a.shelter_id,
                    "people": a.people,
                    "phase": "to pickup" if a.status == AssignmentStatus.COMMITTED else "carrying to camp",
                    "eta_min": max(0, (a.eta_pickup_tick if a.status == AssignmentStatus.COMMITTED else a.eta_dropoff_tick) - w.tick),
                    "decision_id": a.decision_id,
                }
                for a in active
            ],
            "cost": sel.get("components") if dec and dec.get("outcome") == "COMMITTED" else None,
            "decision_id": dec["id"] if dec else None,
            "_key": priority_key(inc),
            "_eligible": ok,
        })
    rows.sort(key=lambda r: (not r["_eligible"], r["_key"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
        r.pop("_key")
        r.pop("_eligible")
    return rows[:limit]


# ---------------------------------------------------------------------- synthetic SOS
def nearest_land_node(world: Any, x: Optional[float], y: Optional[float]) -> str:
    land = [n for n in world.nodes.values() if n.kind == "land"]
    if x is None or y is None:
        raise FeedError("sos needs node, or x and y")
    return min(land, key=lambda n: (n.x - float(x)) ** 2 + (n.y - float(y)) ** 2).id


def synthetic_messages(engine: Any, rng: random.Random, ramp: float, rain_factor: float, p: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """Synthetic field traffic for one hour of the flood: SOS calls from where people are and the
    water is, plus duplicates, contradictions, road reports and camp updates at the set rates."""
    w = engine.world
    out: list[dict[str, Any]] = []
    lam = float(p.get("sos_per_hour", 1.5)) * (0.2 + 0.8 * ramp) * rain_factor
    n = _poisson(rng, lam)
    land = [x for x in w.nodes.values() if x.kind == "land"]
    weights = [(max(x.pop, 1) ** 0.5) * (0.15 + min(x.depth, 2.0)) for x in land]
    for _ in range(n):
        node = rng.choices(land, weights=weights)[0]
        urg = 5 if node.depth > 1.2 else 4 if node.depth > 0.6 else 3 if node.depth > 0.2 else 2
        urg = max(1, min(5, urg + rng.choice([-1, 0, 0, 0, 1])))
        out.append({"type": "sos", "source": source, "node": node.id, "people": rng.choices([1, 2, 3, 4, 5, 6, 8, 10], [5, 12, 14, 14, 11, 8, 5, 2])[0], "urgency": urg, "reporter": rng.choices(["resident", "sensor", "responder"], [55, 20, 25])[0]})
    open_inc = sorted((i for i in w.incidents.values() if i.status not in (IncidentStatus.RESCUED, IncidentStatus.FALSE_ALARM)), key=lambda i: i.id)
    if open_inc and rng.random() < float(p.get("duplicate_rate", 0.2)) * min(1.0, n + 0.5):
        out.append({"type": "duplicate", "source": source, "incident_id": rng.choice(open_inc[-8:]).id, "reporter": rng.choice(["resident", "sensor", "responder"])})
    if open_inc and rng.random() < float(p.get("contradiction_rate", 0.05)):
        out.append({"type": "contradiction", "source": source, "incident_id": rng.choice(open_inc).id, "reporter": "resident"})
    if rng.random() < float(p.get("road_report_rate", 0.04)) * (0.3 + ramp):
        roads = sorted((e for e in w.edges.values() if e.kind == EdgeKind.ROAD and e.ground_ok and not e.manual_closed and (w.nodes[e.u].depth > 0 or w.nodes[e.v].depth > 0)), key=lambda e: e.id)
        if roads:
            out.append({"type": "road", "source": source, "edge_id": rng.choice(roads).id, "reason": rng.choice(["field report: fallen trees", "field report: landslide debris", "field report: road washed out"])})
    if rng.random() < float(p.get("shelter_update_rate", 0.02)):
        sh = sorted(w.shelters.values(), key=lambda s: s.id)
        s = rng.choice(sh)
        out.append({"type": "shelter", "source": source, "shelter_id": s.id, "capacity": max(s.occupancy, s.capacity - rng.choice([5, 8, 10])), "reason": "camp report: part of the building unusable"})
    return out


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    L, k, prod = math.exp(-lam), 0, 1.0
    while True:
        prod *= rng.random()
        if prod <= L:
            return k
        k += 1


# ---------------------------------------------------------------------- source plans
def drill_plan(map_id: str) -> list[dict[str, Any]]:
    """The real hourly August 2018 gauge record at the map's CWC station: 14 Aug 12:00 (the main
    rise) to 17 Aug 12:00 (after the peak)."""
    path = DATA / f"cwc_2018__{map_id}.json"
    if not path.exists():
        raise FeedError(f"no 2018 gauge record for map {map_id}")
    d = json.loads(path.read_text(encoding="utf-8"))
    rows = [r for r in d["readings"] if "2018-08-14T12:00" <= r["time"] <= "2018-08-17T12:00"]
    rain = {}
    ev = DATA / f"kerala_2018__{map_id}.json"
    if ev.exists():
        e = json.loads(ev.read_text(encoding="utf-8"))
        # hourly IMD-scaled rain, aligned to the event window start (14 Aug 00:00)
        for i, mm in enumerate(e.get("rain_mm_h", [])):
            h = f"2018-08-{14 + i // 24:02d}T{i % 24:02d}:00"
            rain[h] = mm
    peak_rain = max(rain.values()) if rain else 1.0
    return [{"time": r["time"], "level_m": r["level_m"], "station": d["station"]["name"], "rain_factor": 0.5 + (rain.get(r["time"], 0.0) / peak_rain if peak_rain else 0.0)} for r in rows]


def synthetic_plan(map_id: str, p: dict[str, Any]) -> list[dict[str, Any]]:
    """A parameterised flood: rise, hold at the peak, recede (hourly readings)."""
    g = STATIONS.get(map_id, {}).get("gauge") or {"base_m": 0.0, "peak_m": 1.0, "name": "synthetic gauge"}
    rise, hold, fall = int(p.get("rise_hours", 24)), int(p.get("hold_hours", 8)), int(p.get("fall_hours", 24))
    peak = max(0.05, min(1.3, float(p.get("peak", 0.9))))  # fraction of the 2018 peak
    out = []
    for h in range(rise + hold + fall):
        if h < rise:
            c = peak * (0.5 - 0.5 * math.cos(math.pi * h / max(1, rise)))
        elif h < rise + hold:
            c = peak
        else:
            c = peak * (0.5 + 0.5 * math.cos(math.pi * (h - rise - hold) / max(1, fall)))
        out.append({"time": f"+{h:02d}h", "level_m": round(g["base_m"] + c * (g["peak_m"] - g["base_m"]), 2), "ramp": round(min(1.0, c), 3), "station": f"{g['name']} (synthetic)", "rain_factor": 0.6 + 0.8 * (c / peak)})
    return out


def live_reading(map_id: str) -> Optional[dict[str, Any]]:
    d = cwc_latest(map_id)
    g = d.get("gauge") if d.get("available") else None
    if not g or g.get("level_m") is None:
        return None
    return {"time": g["time"], "level_m": g["level_m"], "station": g["name"], "dams": d.get("dams", []), "stale": d.get("stale", False)}
