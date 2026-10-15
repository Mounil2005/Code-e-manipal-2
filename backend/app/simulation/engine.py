"""Deterministic tick-based simulation engine (1 tick = 1 simulated minute).

Tick pipeline:
   1 apply scheduled events           8 safety validation (inside try_commit)
   2 update flood / graph availability 9 atomic commit + shelter reservation
   3 ingest newly arrived reports      10 advance units along validated routes
   4 reconcile contradictory evidence  11 pickup / dropoff / deadline outcomes
   5 maintain reservations             12 metrics
   6 invalidate / reroute / cancel     13 invariant sweep + audit
   7 negotiation round (strategy)      14 frame snapshot + state hash

User-injected events are applied between ticks, logged with (tick, seq) and
followed by an immediate replanning micro-step, so the run can be replayed
exactly from (scenario, seed, strategy, injected-event log).
"""
from __future__ import annotations

import copy
import math
import time
from typing import Any, Callable, Optional

from ..negotiation import common
from ..negotiation import decentralized
from ..negotiation.common import (
    NegotiationRound,
    release_reservation,
    request_reservation,
    unassigned_people,
)
from ..optimization import centralized
from ..routing.router import (
    believed_closed,
    edge_between,
    edge_minutes,
    path_valid,
    plan_from_resource,
    sssp,
    truly_traversable,
)
from ..safety.shield import ShieldResult, audit_invariants, validate_move
from . import reports as report_mod
from .flood import snapshot_traversability, update_flood
from .scenarios import ScenarioConfig
from .state import (
    ACTIVE_ASSIGNMENT,
    Assignment,
    AssignmentStatus,
    AuditRecord,
    EdgeKind,
    Incident,
    IncidentReport,
    IncidentStatus,
    ReservationStatus,
    RescueResource,
    ResourceKind,
    ResourceStatus,
    SimulationEvent,
    WorldState,
)
from .world import build_world, generate_reports

EVENT_TYPES = (
    "APPROVE_RECOMMENDATION",
    "REJECT_RECOMMENDATION",
    "SET_APPROVAL_MODE",
    "GAUGE_READING",
    "DAM_RELEASE",
    "REPORT_DUPLICATE",
    "CLOSE_EDGE",
    "OPEN_EDGE",
    "RESOURCE_FAILURE",
    "RESOURCE_RESTORE",
    "SHELTER_CAPACITY",
    "NEW_INCIDENT",
    "CONTRADICTORY_REPORT",
    "FLOOD_SURGE",
    "COMM_BLACKOUT",
    "COORDINATOR_OUTAGE",
    "MANUAL_ASSIGN",
    "CANCEL_ASSIGNMENT",
    "REQUEST_REASSIGN",
    "TRIPLE_FAILURE",
    "ADD_RESOURCE",
    "SET_POLICY",
)

RELIABILITY = {"resident": 0.6, "sensor": 0.75, "responder": 0.9}


class EventError(ValueError):
    pass


class Engine:
    def __init__(self, cfg: ScenarioConfig, strategy: str = "decentralized", record_frames: bool = True):
        self.base_cfg = copy.deepcopy(cfg)  # immutable: replay/fork always start from this
        self.cfg = cfg
        self.strategy = strategy
        self.record_frames = record_frames
        self.world: WorldState = build_world(cfg, strategy)
        self.world.pending_reports = generate_reports(self.world, cfg)
        self.schedule = sorted(cfg.schedule, key=lambda e: e.get("tick", 0))
        self.injected: list[dict[str, Any]] = []
        self.events: list[SimulationEvent] = []
        self.audit_log: list[AuditRecord] = []
        self.rounds: list[NegotiationRound] = []
        self.decisions: dict[str, dict[str, Any]] = {}
        self.safety_log: list[dict[str, Any]] = []
        self.frames: dict[int, dict[str, Any]] = {}
        self.hashes: dict[int, str] = {}
        self.metric_series: list[dict[str, Any]] = []
        self.disruptions: list[dict[str, Any]] = []
        self.route_ghosts: list[dict[str, Any]] = []
        self.counters: dict[str, int] = {
            "reassignments": 0,
            "reroutes": 0,
            "cancellations": 0,
            "route_violations": 0,
            "blocked_moves": 0,
            "safety_rejections": 0,
            "false_alarm_visits": 0,
            "provisional_dispatches": 0,
            "invariant_violations": 0,
            "stale_rejections": 0,
        }
        # approval mode: AI recommendations awaiting an operator, and pairs the operator rejected
        self.recommendations: dict[str, dict[str, Any]] = {}
        self.rejected_pairs: dict[tuple[str, str], int] = {}
        self.approval_latency: list[int] = []
        self.counters["approvals"] = 0
        self.counters["operator_rejections"] = 0
        self.counters["operator_overrides"] = 0
        self.needs_reassign: set[str] = set()
        self._last_outcome: dict[str, str] = {}
        self._round_seq = 0
        self._decision_seq = 0
        self._event_seq = 0
        self.finished = False
        self.listeners: list[Callable[[str, Any], None]] = []
        self.compute_ms = 0.0
        # initial state (tick 0): flood picture + reports already known at t=0
        update_flood(self.world, cfg.flood_severity, cfg.flood_peak_tick)
        snapshot_traversability(self.world)
        self._ingest_reports()
        self._publish_all()
        self._snapshot()

    # ================================================================ utilities
    def next_round_id(self) -> str:
        self._round_seq += 1
        return f"NR-{self._round_seq:04d}"

    def audit(self, category: str, message: str, refs: Optional[dict[str, Any]] = None, severity: str = "info") -> None:
        w = self.world
        rec = AuditRecord(f"AU-{len(self.audit_log) + 1:05d}", w.tick, category, message, refs or {}, w.version, severity)
        self.audit_log.append(rec)

    def people_taken(self, inc_id: str) -> int:
        return sum(
            a.loaded_people
            for a in self.world.assignments.values()
            if a.incident_id == inc_id and a.loaded_people and a.status in (AssignmentStatus.LOADED, AssignmentStatus.COMPLETED, AssignmentStatus.CANCELLED)
        )

    def publish(self, inc: Incident) -> None:
        ok, rule = common.is_eligible(self.world, inc)
        snap = {
            "tick": self.world.tick,
            "id": inc.id,
            "version": inc.version,
            "node": inc.node,
            "urgency": inc.urgency,
            "deadline": inc.deadline_tick,
            "unassigned": unassigned_people(self.world, inc),
            "nbhd": inc.neighbourhood,
            "confidence": inc.confidence,
            "status": inc.status,
            "eligible": ok,
            "rule": rule,
        }
        if inc.publications:
            last = {k: v for k, v in inc.publications[-1].items() if k != "tick"}
            if last == {k: v for k, v in snap.items() if k != "tick"}:
                return
            if inc.publications[-1]["tick"] == snap["tick"]:
                inc.publications[-1] = snap
                return
        inc.publications.append(snap)
        if len(inc.publications) > 12:
            del inc.publications[:-12]

    def _publish_all(self) -> None:
        for inc in self.world.incidents.values():
            self.publish(inc)

    def refresh_incident_status(self, inc: Incident) -> None:
        w = self.world
        if inc.status == IncidentStatus.FALSE_ALARM:
            return
        active = [a for a in w.assignments.values() if a.incident_id == inc.id and a.status in ACTIVE_ASSIGNMENT]
        if inc.people_estimate <= 0 and not active and (inc.people_rescued > 0 or inc.visited):
            if inc.people_rescued == 0 and not self._has_real_truth(inc):
                inc.status = IncidentStatus.FALSE_ALARM
            else:
                inc.status = IncidentStatus.RESCUED
                if inc.completed_tick is None:
                    inc.completed_tick = w.tick
            return
        if active and unassigned_people(w, inc) == 0:
            inc.status = IncidentStatus.ASSIGNED
        elif inc.visited or inc.confidence >= common.VERIFIED_THRESHOLD:
            inc.status = IncidentStatus.VERIFIED
        elif inc.confidence >= common.PROVISIONAL_THRESHOLD:
            inc.status = IncidentStatus.SUSPECTED
        else:
            inc.status = IncidentStatus.UNVERIFIED
        inc.provisional = inc.confidence < common.VERIFIED_THRESHOLD and not inc.visited

    def _has_real_truth(self, inc: Incident) -> bool:
        return any(self.world.truth.get(t, {}).get("exists") for t in inc.truth_ids)

    def is_decentralized(self) -> bool:
        return self.strategy == "decentralized"

    def belief_for(self, res: RescueResource) -> Optional[frozenset]:
        if not self.is_decentralized():
            return None
        return believed_closed(self.world, res, max(0, int(self.cfg.comm_delay)))

    # ================================================================ routes & movement
    def set_route(self, res: RescueResource, path: list[str]) -> None:
        if not path:
            res.route = []
            return
        if res.next_node and res.edge_id:
            if path[0] == res.next_node:
                res.route = list(path[1:])
            elif path[0] == res.node:
                # turn around on the current edge
                res.node, res.next_node = res.next_node, res.node
                res.edge_progress = 1.0 - res.edge_progress
                res.route = list(path[1:])
            else:
                res.route = list(path)
        else:
            res.route = list(path[1:]) if path[0] == res.node else list(path)

    def remaining_path(self, res: RescueResource) -> list[str]:
        head = res.next_node if res.next_node else res.node
        return [head] + list(res.route)

    def route_target(self, res: RescueResource) -> Optional[str]:
        a = self.world.assignments.get(res.assignment_id or "")
        if not a:
            return None
        if res.status == ResourceStatus.EN_ROUTE_PICKUP:
            return self.world.incidents[a.incident_id].node
        if res.status == ResourceStatus.EN_ROUTE_SHELTER:
            return self.world.shelters[a.shelter_id].node
        return None

    def _advance(self, res: RescueResource) -> None:
        if res.status in (ResourceStatus.DISABLED, ResourceStatus.LOADING, ResourceStatus.UNLOADING):
            return
        w = self.world
        budget = 1.0
        guard = 0
        while budget > 1e-9 and guard < 50:
            guard += 1
            if not res.next_node:
                if not res.route:
                    break
                nxt = res.route[0]
                e = edge_between(w, res.node, nxt)
                if e is None:
                    res.route = []
                    break
                gate = validate_move(w, res.id, e.id)
                if not gate.passed:
                    self.counters["blocked_moves"] += 1
                    res.known_closed[e.id] = w.tick
                    self.safety_log.append({"tick": w.tick, "type": "MOVEMENT_BLOCKED", "resource": res.id, "edge": e.id, "detail": gate.detail})
                    self.audit("safety", f"Movement gate stopped {res.id} entering {e.name or e.id}: {gate.detail}", {"resource": res.id, "edge": e.id}, "warn")
                    self._replan(res, f"discovered {e.name or e.id} impassable on arrival", cause_edge=e.id)
                    break
                res.next_node, res.edge_id, res.edge_progress = nxt, e.id, 0.0
                res.route = res.route[1:]
            e = w.edges[res.edge_id]  # type: ignore[index]
            if not truly_traversable(e, res.kind) and res.retreat_edge != e.id:
                # any movement along a prohibited edge other than a sanctioned retreat is a violation
                self.counters["route_violations"] += 1
                self.safety_log.append({"tick": w.tick, "type": "ROUTE_VIOLATION", "resource": res.id, "edge": e.id})
                self.audit("safety", f"ROUTE VIOLATION: {res.id} moved on prohibited edge {e.id}", {"resource": res.id, "edge": e.id}, "critical")
            total = edge_minutes(e, res.kind, res.speed_kmh)
            remaining = (1.0 - res.edge_progress) * total
            if budget >= remaining:
                budget -= remaining
                res.distance_km += e.length_km * (1.0 - res.edge_progress)
                res.node = res.next_node  # type: ignore[assignment]
                res.next_node, res.edge_id, res.edge_progress = None, None, 0.0
                res.retreat_edge = None
            else:
                frac = budget / total
                res.edge_progress += frac
                res.distance_km += e.length_km * frac
                budget = 0.0

    def _retreat_from_closed_edges(self) -> None:
        w = self.world
        for res in sorted(w.resources.values(), key=lambda r: r.id):
            if res.status == ResourceStatus.DISABLED or not res.edge_id:
                continue
            e = w.edges[res.edge_id]
            if truly_traversable(e, res.kind) or res.retreat_edge == e.id:
                continue
            # reverse direction: back to the node it came from (permitted retreat)
            old_next = res.next_node
            res.node, res.next_node = res.next_node, res.node  # type: ignore[assignment]
            res.edge_progress = 1.0 - res.edge_progress
            res.route = []
            res.retreat_edge = e.id
            self.audit("reroute", f"{res.id} turned back on {e.name or e.id} ({e.closed_reason or 'impassable'}) — retreating to {res.next_node}", {"resource": res.id, "edge": e.id, "abandoned": old_next}, "warn")
            if res.assignment_id:
                self._replan(res, f"{e.name or e.id} closed while crossing", cause_edge=e.id)

    def _replan(self, res: RescueResource, cause: str, cause_edge: Optional[str] = None) -> None:
        """Re-route the unit to its current target; cancel or renegotiate if impossible."""
        w = self.world
        a = w.assignments.get(res.assignment_id or "")
        if not a or a.status not in ACTIVE_ASSIGNMENT:
            return
        target = self.route_target(res)
        if target is None:
            return
        closed = self.belief_for(res)
        old_eta = a.eta_pickup_tick if res.status == ResourceStatus.EN_ROUTE_PICKUP else a.eta_dropoff_tick
        old_path = self.remaining_path(res)
        reach = plan_from_resource(w, res, closed)
        if target in reach:
            mins, path = reach[target]
            ok, _bad = path_valid(w, res.kind, path)
            if ok or closed is not None:
                self.set_route(res, path)
                new_eta = w.tick + int(math.ceil(mins))
                if res.status == ResourceStatus.EN_ROUTE_PICKUP:
                    a.eta_pickup_tick = new_eta
                    a.pickup_route = path
                else:
                    a.eta_dropoff_tick = new_eta
                    a.dropoff_route = path
                a.reroutes += 1
                a.version += 1
                self.counters["reroutes"] += 1
                via = " → ".join(path[:6]) + (" …" if len(path) > 6 else "")
                self.audit(
                    "reroute",
                    f"{res.id} rerouted to {target}: {cause}. ETA t={old_eta} → t={new_eta} via {via}",
                    {"resource": res.id, "assignment": a.id, "edge": cause_edge, "old_path": old_path, "new_path": path, "old_eta": old_eta, "new_eta": new_eta},
                    "warn",
                )
                return
        if a.status == AssignmentStatus.COMMITTED:
            self.cancel_assignment(a, f"no feasible {res.kind} route to {target} ({cause})")
        else:
            self._renegotiate_shelter(a, res, f"shelter unreachable ({cause})")

    # ================================================================ assignment lifecycle
    def cancel_assignment(self, a: Assignment, reason: str) -> None:
        w = self.world
        if a.status not in ACTIVE_ASSIGNMENT:
            return
        a.status = AssignmentStatus.CANCELLED
        a.cancel_reason = reason
        a.version += 1
        release_reservation(w, a.reservation_id)
        self.counters["cancellations"] += 1
        res = w.resources[a.resource_id]
        if res.assignment_id == a.id:
            res.assignment_id = None
            res.route = []
            if res.status != ResourceStatus.DISABLED:
                res.status = ResourceStatus.IDLE
            res.version += 1
        inc = w.incidents[a.incident_id]
        inc.version += 1
        if inc.status not in (IncidentStatus.RESCUED, IncidentStatus.FALSE_ALARM):
            self.needs_reassign.add(inc.id)
        self.refresh_incident_status(inc)
        self.publish(inc)
        self.audit("cancel", f"Cancelled {a.id} ({a.resource_id} → {a.incident_id}): {reason}", {"assignment": a.id, "resource": a.resource_id, "incident": a.incident_id, "decision": a.decision_id}, "critical")

    def _renegotiate_shelter(self, a: Assignment, res: RescueResource, reason: str) -> bool:
        """Shelter agents are asked again for capacity, nearest reachable first."""
        w = self.world
        origin = w.incidents[a.incident_id].node if a.status == AssignmentStatus.COMMITTED else None
        closed = self.belief_for(res)
        rnd = NegotiationRound(self.next_round_id(), w.tick, self.strategy, f"shelter renegotiation: {reason}")
        rnd.resources = [res.id]
        rnd.incidents = [a.incident_id]
        rnd.msg("RENEGOTIATE", res.id, "shelters", a.incident_id, reason=reason, people=a.loaded_people or a.people)
        if origin is not None:
            dist, paths = sssp(w, res.kind, res.speed_kmh, origin, closed)
            reach = {k: (dist[k], paths[k]) for k in dist}
        else:
            reach = plan_from_resource(w, res, closed)
        people = a.loaded_people or a.people
        options = sorted(((reach[s.node][0], s.id) for s in w.shelters.values() if s.node in reach), key=lambda t: (t[0], t[1]))
        old = a.shelter_id
        release_reservation(w, a.reservation_id)
        for mins, sid in options:
            base_eta = (a.eta_pickup_tick - w.tick + 2) if origin is not None else 0
            eta = w.tick + max(0, base_eta) + int(math.ceil(mins))
            rnd.msg("RESERVE", res.id, sid, a.incident_id, people=people)
            rsv, why = request_reservation(w, sid, people, res.id, a.incident_id, eta, a.id)
            if rsv is None:
                rnd.msg("REJECT_RESERVATION", sid, res.id, a.incident_id, reason=why)
                continue
            rnd.msg("ACCEPT_RESERVATION", sid, res.id, a.incident_id, reservation=rsv.id, reason=why)
            a.shelter_id, a.reservation_id = sid, rsv.id
            a.eta_dropoff_tick = eta
            a.version += 1
            path = reach[w.shelters[sid].node][1]
            a.dropoff_route = path
            if a.status == AssignmentStatus.LOADED and res.status != ResourceStatus.LOADING:
                self.set_route(res, path)
                res.status = ResourceStatus.EN_ROUTE_SHELTER
            rnd.msg("COMMIT", "coordination", res.id, a.incident_id, assignment=a.id, shelter=sid)
            rnd.commits.append(a.id)
            self.rounds.append(rnd)
            self.audit("shelter", f"{a.id}: shelter renegotiated {old} → {sid} ({reason}); {why}", {"assignment": a.id, "resource": res.id, "shelter": sid, "round": rnd.id}, "warn")
            return True
        self.rounds.append(rnd)
        if a.status == AssignmentStatus.COMMITTED:
            self.cancel_assignment(a, f"no shelter capacity reachable ({reason})")
        else:
            if res.status != ResourceStatus.HOLDING:
                self.audit("shelter", f"{res.id} HOLDING with {people} aboard: no shelter has capacity ({reason})", {"resource": res.id, "assignment": a.id}, "critical")
            res.status = ResourceStatus.HOLDING
            res.route = []
            a.reservation_id = ""
        return False

    def on_false_alarm(self, inc: Incident, why: str) -> None:
        self.audit("incident", f"{inc.id} classified FALSE_ALARM: {why}", {"incident": inc.id}, "warn")
        for a in sorted(self.world.assignments.values(), key=lambda a: a.id):
            if a.incident_id == inc.id and a.status == AssignmentStatus.COMMITTED:
                self.cancel_assignment(a, f"evidence reconciliation: {why}")
        self.needs_reassign.discard(inc.id)

    def _strand(self, res: RescueResource, a: Assignment, why: str) -> Optional[str]:
        """A loaded unit failed: its passengers become a new life-safety incident."""
        w = self.world
        node = res.node if (not res.next_node or res.edge_progress < 0.5) else res.next_node
        tid = f"STR-{res.id}-{w.tick}"
        w.truth[tid] = {"id": tid, "tick": w.tick, "node": node, "people": res.load, "urgency": 5, "exists": True, "remaining": res.load}
        rep = IncidentReport(w.next_id("RPT"), w.tick, "responder", 0.9, node, res.load, 5, "PRESENT", 0.0, tid, note=f"{res.id} crew stranded with {res.load} evacuees")
        a.status = AssignmentStatus.CANCELLED
        a.cancel_reason = why
        release_reservation(w, a.reservation_id)
        moved = res.load
        a.loaded_people = 0  # passengers are re-registered under the new incident
        res.load = 0
        res.assignment_id = None
        self.counters["cancellations"] += 1
        self.audit("cancel", f"{res.id} failed carrying {moved} evacuees — raising urgent incident at {node}", {"resource": res.id, "assignment": a.id}, "critical")
        inc = report_mod.ingest(self, rep)
        if inc is not None:
            self.needs_reassign.add(inc.id)
        return inc.id if inc is not None else None

    # ================================================================ events
    def inject(self, type_: str, params: dict[str, Any], source: str = "user") -> SimulationEvent:
        """Apply an event now (between ticks), log it for replay, then replan."""
        if self.finished:
            raise EventError("simulation has finished; reset or fork to continue")
        ev = self._apply_event(type_, params, source)
        self._event_seq += 1
        self.injected.append({"tick": self.world.tick, "seq": self._event_seq, "type": type_, "params": params, "source": source})
        self._microstep(f"event {ev.id} {type_}")
        self._update_disruptions()
        self._snapshot()
        return ev

    def _apply_event(self, type_: str, params: dict[str, Any], source: str) -> SimulationEvent:
        w = self.world
        if type_ not in EVENT_TYPES:
            raise EventError(f"unknown event type {type_}")
        ev = SimulationEvent(f"EV-{len(self.events) + 1:04d}", w.tick, type_, dict(params), source)
        handler = getattr(self, f"_ev_{type_.lower()}")
        handler(ev, params)
        self.events.append(ev)
        w.bump()
        self.audit(
            "event",
            f"{type_} ({source}): " + ("; ".join(ev.effects) or "no effect"),
            {"event": ev.id, **{k: v for k, v in params.items() if isinstance(v, (str, int, float))}},
            "critical" if type_ in ("RESOURCE_FAILURE", "TRIPLE_FAILURE", "CLOSE_EDGE", "SHELTER_CAPACITY") else "info" if type_ in ("GAUGE_READING", "DAM_RELEASE", "REPORT_DUPLICATE") else "warn",
        )
        for fn in self.listeners:
            fn("event", ev)
        return ev

    def _need(self, params: dict[str, Any], key: str, table: dict[str, Any], label: str) -> Any:
        val = params.get(key)
        if val not in table:
            raise EventError(f"unknown {label} '{val}'")
        return table[val]

    def _track_disruption(self, ev: SimulationEvent, incidents: set[str], holds: set[str]) -> None:
        self.disruptions.append({"event": ev.id, "type": ev.type, "tick": self.world.tick, "incidents": sorted(incidents), "holds": sorted(holds), "resolved_tick": None, "recovery": None})

    def _ev_close_edge(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        w = self.world
        if p.get("edge_id") == "@critical":  # scenario presets name the map's most critical bridge
            p = dict(p, edge_id=w.meta.get("critical_bridge") or "BR2")
        e = self._need(p, "edge_id", w.edges, "edge")
        for r in sorted(w.resources.values(), key=lambda r: r.id):
            path = self.remaining_path(r)
            if r.assignment_id and any({a, b} == {e.u, e.v} for a, b in zip(path, path[1:])):
                self.route_ghosts.append({"resource": r.id, "tick": w.tick, "edge": e.id, "path": list(path)})
        self.route_ghosts = self.route_ghosts[-30:]
        e.manual_closed = True
        e.closed_reason = str(p.get("reason") or "closed by operator")
        update_flood(w, self.cfg.flood_severity, self.cfg.flood_peak_tick)
        snapshot_traversability(w)
        w.closure_log.append({"tick": w.tick, "edge": e.id, "closed": True})
        affected: set[str] = set()
        for r in w.resources.values():
            path = self.remaining_path(r)
            uses = r.edge_id == e.id or any(edge_between(w, x, y) is e for x, y in zip(path, path[1:]))
            if uses and r.assignment_id:
                affected.add(w.assignments[r.assignment_id].incident_id)
        ev.effects.append(f"{e.name or e.id} closed ({e.closed_reason}); {len(affected)} active commitments route through it")
        self._track_disruption(ev, affected, set())

    def _ev_open_edge(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        w = self.world
        e = self._need(p, "edge_id", w.edges, "edge")
        e.manual_closed = False
        e.closed_reason = ""
        update_flood(w, self.cfg.flood_severity, self.cfg.flood_peak_tick)
        snapshot_traversability(w)
        w.closure_log.append({"tick": w.tick, "edge": e.id, "closed": False})
        ev.effects.append(f"{e.name or e.id} re-opened")

    def _ev_resource_failure(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        w = self.world
        r: RescueResource = self._need(p, "resource_id", w.resources, "resource")
        if r.status == ResourceStatus.DISABLED:
            ev.effects.append(f"{r.id} already disabled")
            return
        affected: set[str] = set()
        a = w.assignments.get(r.assignment_id or "")
        why = f"{r.id} {p.get('reason') or 'breakdown'}"
        if a and a.status == AssignmentStatus.COMMITTED:
            affected.add(a.incident_id)
            self.cancel_assignment(a, why)
        elif a and a.status == AssignmentStatus.LOADED:
            sid = self._strand(r, a, why)
            if sid:
                affected.add(sid)
        r.status = ResourceStatus.DISABLED
        r.route = []
        r.version += 1
        ev.effects.append(f"{r.id} ({r.name}) disabled: {p.get('reason') or 'breakdown'}" + (f"; commitment to {sorted(affected)} released" if affected else ""))
        self._track_disruption(ev, affected, set())

    def _ev_resource_restore(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        r: RescueResource = self._need(p, "resource_id", self.world.resources, "resource")
        if r.status == ResourceStatus.DISABLED:
            r.status = ResourceStatus.IDLE
            r.version += 1
            ev.effects.append(f"{r.id} back in service")
        else:
            ev.effects.append(f"{r.id} was not disabled")

    def _ev_shelter_capacity(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        w = self.world
        s = self._need(p, "shelter_id", w.shelters, "shelter")
        try:
            cap = s.occupancy if p.get("capacity") == "occupancy" else int(p.get("capacity"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise EventError("capacity must be an integer")
        if cap < 0 or cap > 1000:
            raise EventError("capacity must be between 0 and 1000")
        old = s.capacity
        s.capacity = cap
        s.version += 1
        revoked = []
        # revoke most-recent reservations first until the invariant holds (LIFO, documented)
        active = sorted((r for r in w.reservations.values() if r.shelter_id == s.id and r.status == ReservationStatus.ACTIVE), key=lambda r: (r.created_tick, r.id), reverse=True)
        for rsv in active:
            if s.occupancy + s.reserved(w) <= s.capacity:
                break
            release_reservation(w, rsv.id, ReservationStatus.REVOKED)
            revoked.append(rsv)
        holds: set[str] = set()
        incs: set[str] = set()
        for rsv in revoked:
            a = w.assignments.get(rsv.assignment_id)
            if a and a.status == AssignmentStatus.LOADED:
                holds.add(a.id)
            elif a and a.status == AssignmentStatus.COMMITTED:
                incs.add(a.incident_id)
        ev.effects.append(f"{s.name} capacity {old} → {cap} ({p.get('reason') or 'operator'}); revoked {len(revoked)} reservations" + (f" ({', '.join(r.id for r in revoked)})" if revoked else ""))
        self._track_disruption(ev, incs, holds)

    def _ev_new_incident(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        w = self.world
        node = self._need(p, "node", w.nodes, "node")
        if node.kind == "river":
            raise EventError("incidents must be placed on land nodes")
        try:
            people = int(p.get("people") or 4)
            urgency = int(p.get("urgency") or 3)
        except (TypeError, ValueError):
            raise EventError("people and urgency must be integers")
        if not (1 <= people <= 40 and 1 <= urgency <= 5):
            raise EventError("people must be 1..40 and urgency 1..5")
        source = p.get("source") or "responder"
        if source not in RELIABILITY:
            raise EventError("source must be resident|sensor|responder")
        tid = f"U{len(w.truth) + 1:03d}"
        w.truth[tid] = {"id": tid, "tick": w.tick, "node": node.id, "people": people, "urgency": urgency, "exists": True, "remaining": people}
        rep = IncidentReport(w.next_id("RPT"), w.tick, source, RELIABILITY[source], node.id, people, urgency, "PRESENT", 0.0, tid, note="operator-injected SOS")
        inc = report_mod.ingest(self, rep)
        ev.effects.append(f"SOS at {node.id}: {people} people, urgency {urgency} → {inc.id if inc else '-'}")

    def _ev_contradictory_report(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        w = self.world
        inc = self._need(p, "incident_id", w.incidents, "incident")
        claim = p.get("claim") or "NO_VICTIMS"
        source = p.get("source") or "resident"
        if claim not in ("NO_VICTIMS", "PRESENT") or source not in RELIABILITY:
            raise EventError("claim must be NO_VICTIMS|PRESENT and source resident|sensor|responder")
        people = p.get("people")
        rep = IncidentReport(
            w.next_id("RPT"), w.tick, source, RELIABILITY[source], inc.node,
            int(people) if people else (0 if claim == "NO_VICTIMS" else None), p.get("urgency"), claim, 0.0, "", note="operator-injected report",
        )
        report_mod.ingest(self, rep)
        ev.effects.append(f"{source} report '{claim}' on {inc.id} → status {inc.status}, confidence {inc.confidence:.2f}")

    def _ev_flood_surge(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        w = self.world
        delta = float(p.get("delta") or 0.5)
        if not (-2 <= delta <= 2):
            raise EventError("delta must be within ±2 m")
        before = {e.id for e in w.edges.values() if e.ground_ok}
        w.flood_offset += delta
        update_flood(w, self.cfg.flood_severity, self.cfg.flood_peak_tick)
        snapshot_traversability(w)
        closed = sorted(before - {e.id for e in w.edges.values() if e.ground_ok})
        ev.effects.append(f"water level {'+' if delta >= 0 else ''}{delta:.1f} m; {len(closed)} road segments newly impassable for ground units")

    # ---------------------------------------------------------------- human in the loop
    def recommend(self, cand: Any, rnd: Any, decision: dict[str, Any]) -> None:
        """Approval mode: keep the agents' choice as a recommendation (one per unit and per
        incident) instead of committing it. Nothing is reserved or dispatched until approval."""
        w = self.world
        pair = (cand.resource_id, cand.incident_id)
        if self.rejected_pairs.get(pair, -1) >= w.tick:
            decision["outcome"] = "OPERATOR_REJECTED"
            return None
        if decision.get("recommendation_id"):
            # a later matching pass offered this incident another unit: the record keeps the one recommended
            decision["selected"] = decision["recommended"]
            return None
        decision["outcome"] = "RECOMMENDED"
        if any(r["resource"] == cand.resource_id or r["incident"] == cand.incident_id for r in self.recommendations.values()):
            return None
        if cand.shelter_options:
            decision["shelter"] = cand.shelter_options[0][0]
        rid = w.next_id("REC")
        feasible = [c for c in decision.get("candidates", []) if c.get("feasible") and c.get("resource_id") != cand.resource_id]
        self.recommendations[rid] = {
            "id": rid,
            "tick": w.tick,
            "resource": cand.resource_id,
            "incident": cand.incident_id,
            "people": cand.people,
            "shelter": cand.shelter_options[0][0] if cand.shelter_options else None,
            "eta_pickup": round(cand.eta_pickup, 1),
            "eta_total": round(cand.eta_dropoff, 1),
            "cost": round(cand.cost, 3),
            "components": {k: round(v, 3) for k, v in cand.components.items()},
            "decision_id": decision["id"],
            "round": rnd.id,
            "alternatives": [{"resource": c["resource_id"], "cost": c.get("cost")} for c in feasible[:4]],
        }
        decision["recommendation_id"] = rid
        decision["recommended"] = cand.to_dict()
        decision["selected"] = decision["recommended"]
        rnd.msg("RECOMMEND", "coordination", cand.resource_id, cand.incident_id, recommendation=rid)
        self.audit("assignment", f"AI recommends {cand.resource_id} → {cand.incident_id} ({cand.people} people); awaiting operator", {"recommendation": rid, "resource": cand.resource_id, "incident": cand.incident_id, "decision": decision["id"]}, "info")
        return None

    def _prune_recommendations(self) -> None:
        w = self.world
        for rid, rec in list(self.recommendations.items()):
            r = w.resources.get(rec["resource"])
            inc = w.incidents.get(rec["incident"])
            unit_gone = not r or r.status != ResourceStatus.IDLE or r.assignment_id or r.load or r.blackout_until >= w.tick
            need_gone = not inc or inc.status in (IncidentStatus.RESCUED, IncidentStatus.FALSE_ALARM) or common.unassigned_people(w, inc) <= 0
            if unit_gone or need_gone or w.tick - rec["tick"] > 30 or not self.cfg.approval_mode:
                del self.recommendations[rid]
        for pair, until in list(self.rejected_pairs.items()):
            if until < w.tick:
                del self.rejected_pairs[pair]

    def _ev_approve_recommendation(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        rec = self.recommendations.get(p.get("recommendation_id") or "")
        if rec is None:
            raise EventError("recommendation not found or no longer valid")
        res_id = p.get("resource_id") or rec["resource"]
        self._ev_manual_assign(ev, {"resource_id": res_id, "incident_id": rec["incident"]})  # fresh evaluation + safety shield
        self.recommendations.pop(rec["id"], None)
        self.counters["approvals"] += 1
        self.approval_latency.append(self.world.tick - rec["tick"])
        if res_id != rec["resource"]:
            self.counters["operator_overrides"] += 1
            ev.effects.append(f"operator chose {res_id} instead of the recommended {rec['resource']}")
        ev.effects.append(f"operator approved {rec['id']} after {self.world.tick - rec['tick']} min")

    def _ev_reject_recommendation(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        rec = self.recommendations.pop(p.get("recommendation_id") or "", None)
        if rec is None:
            raise EventError("recommendation not found or no longer valid")
        self.rejected_pairs[(rec["resource"], rec["incident"])] = self.world.tick + 20
        self.counters["operator_rejections"] += 1
        ev.effects.append(f"operator rejected {rec['resource']} → {rec['incident']} ({p.get('reason') or 'no reason given'}); the agents will propose another option")

    def _ev_set_approval_mode(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        on = bool(p.get("enabled"))
        self.cfg.approval_mode = on
        if not on:
            self.recommendations.clear()
        ev.effects.append("operator approves every dispatch; agents recommend" if on else "autonomous: agents commit dispatches themselves")

    def _ev_gauge_reading(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        """A river gauge reading from the feed. Sets the river ramp (0 = calm, 1 = the 2018 peak at
        that gauge) and recomputes depths and traversability immediately."""
        from ..live.cwc import ramp_for_level

        w = self.world
        try:
            level = float(p["level_m"])
        except (KeyError, TypeError, ValueError):
            raise EventError("level_m (number) is required")
        if not (-5 <= level <= 60):
            raise EventError("level_m out of range")
        before_ground = {e.id for e in w.edges.values() if e.ground_ok}
        before_boat = {e.id for e in w.edges.values() if e.boat_ok and e.kind == EdgeKind.ROAD}
        ramp = ramp_for_level(w.map_id, level) if "ramp" not in p else max(0.0, min(1.0, float(p["ramp"])))
        prev = w.river_override
        w.river_override = ramp
        update_flood(w, self.cfg.flood_severity, self.cfg.flood_peak_tick)
        snapshot_traversability(w)
        closed = sorted(before_ground - {e.id for e in w.edges.values() if e.ground_ok})
        reopened = sorted({e.id for e in w.edges.values() if e.ground_ok} - before_ground)
        boat = sorted({e.id for e in w.edges.values() if e.boat_ok and e.kind == EdgeKind.ROAD} - before_boat)
        w.gauge_log.append({"tick": w.tick, "level_m": level, "ramp": round(ramp, 3), "time": p.get("time"), "station": p.get("station"), "source": p.get("source", "feed")})
        trend = "" if prev is None else (" rising" if ramp > prev + 1e-6 else " falling" if ramp < prev - 1e-6 else " steady")
        ev.effects.append(f"{p.get('station') or 'gauge'} {level:.2f} m{trend}: {len(closed)} road segments newly impassable, {len(reopened)} reopened, {len(boat)} streets now boat-navigable")

    def _ev_dam_release(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        """An upstream reservoir report. Recorded and broadcast as an early warning; the river
        itself only changes through gauge readings."""
        try:
            q = float(p["outflow_m3s"])
        except (KeyError, TypeError, ValueError):
            raise EventError("outflow_m3s (number) is required")
        ev.effects.append(f"{p.get('dam') or 'reservoir'} outflow {q:.0f} m3/s: downstream rise expected")

    def _ev_report_duplicate(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        """A second call about an incident already known (same people, another source)."""
        w = self.world
        inc = self._need(p, "incident_id", w.incidents, "incident")
        source = p.get("source") or "resident"
        if source not in RELIABILITY:
            raise EventError("source must be resident|sensor|responder")
        tid = inc.truth_ids[0] if inc.truth_ids else ""
        rep = IncidentReport(w.next_id("RPT"), w.tick, source, RELIABILITY[source], inc.node, inc.people_estimate, inc.urgency, "PRESENT", 0.0, tid, note="duplicate report")
        report_mod.ingest(self, rep)
        ev.effects.append(f"duplicate {source} report on {inc.id} merged: confidence {inc.confidence:.2f}, status {inc.status}")

    def _ev_comm_blackout(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        r: RescueResource = self._need(p, "resource_id", self.world.resources, "resource")
        dur = int(p.get("duration") or 15)
        if not (1 <= dur <= 120):
            raise EventError("duration must be 1..120 ticks")
        r.blackout_start = self.world.tick
        r.blackout_until = self.world.tick + dur
        ev.effects.append(f"{r.id} loses communications until t={r.blackout_until}; continues its last valid commitment")

    def _ev_manual_assign(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        w = self.world
        res: RescueResource = self._need(p, "resource_id", w.resources, "resource")
        inc: Incident = self._need(p, "incident_id", w.incidents, "incident")
        if res.status != ResourceStatus.IDLE:
            raise EventError(f"{res.id} is {res.status}; only idle units can be directed")
        rnd = NegotiationRound(self.next_round_id(), w.tick, "operator", f"manual directive {res.id} → {inc.id}")
        rnd.incidents, rnd.resources = [inc.id], [res.id]
        view = common.incident_view(w, inc)
        cand = common.evaluate(w, self.cfg, res, view, common.shelter_offers(w), common.neighbourhood_waits(w), None, {})
        dec = self.new_decision(rnd, inc, [cand], view)
        rnd.msg("DIRECTIVE", "operator", res.id, inc.id)
        if not cand.feasible:
            self.rounds.append(rnd)
            dec["outcome"] = "INFEASIBLE"
            self.finalize_decision(dec)
            raise EventError(f"rejected: {cand.reason}")
        dec["selected"] = cand.to_dict()
        a = common.try_commit(self, cand, rnd, dec)
        self.rounds.append(rnd)
        self.finalize_decision(dec)
        if a is None:
            raise EventError(f"rejected by safety/shelter validation: {dec.get('outcome')}")
        ev.effects.append(f"operator directed {res.id} → {inc.id} ({a.id})")

    def _ev_cancel_assignment(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        a: Assignment = self._need(p, "assignment_id", self.world.assignments, "assignment")
        if a.status == AssignmentStatus.LOADED:
            raise EventError("unit is carrying evacuees; a loaded mission cannot be cancelled")
        if a.status not in ACTIVE_ASSIGNMENT:
            raise EventError(f"{a.id} is {a.status}")
        self.cancel_assignment(a, f"operator cancelled ({p.get('reason') or 'manual'})")
        ev.effects.append(f"{a.id} cancelled; {a.incident_id} re-enters negotiation")

    def _ev_request_reassign(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        res: RescueResource = self._need(p, "resource_id", self.world.resources, "resource")
        a = self.world.assignments.get(res.assignment_id or "")
        if not a or a.status != AssignmentStatus.COMMITTED:
            raise EventError(f"{res.id} has no reassignable (unloaded) commitment")
        self.cancel_assignment(a, "operator requested reassignment")
        ev.effects.append(f"{res.id} released from {a.incident_id}; open for renegotiation")

    def _ev_add_resource(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        w = self.world
        kind = p.get("kind") or ResourceKind.BOAT
        if kind not in (ResourceKind.BOAT, ResourceKind.GROUND):
            raise EventError("kind must be boat|ground")
        node = p.get("node") or w.meta.get("default_boat_node" if kind == ResourceKind.BOAT else "default_ground_node")
        if node not in w.nodes:
            raise EventError(f"unknown node '{node}'")
        if (w.nodes[node].kind == "river") != (kind == ResourceKind.BOAT):
            raise EventError("boats start on the river, ground teams on land")
        cap = int(p.get("capacity") or (6 if kind == ResourceKind.BOAT else 8))
        prefix = "B" if kind == ResourceKind.BOAT else "G"
        n = 1 + max([int(r.id[1:]) for r in w.resources.values() if r.id.startswith(prefix)] or [0])
        rid = f"{prefix}{n}"
        w.resources[rid] = RescueResource(rid, f"{'Boat' if kind == ResourceKind.BOAT else 'Team'} Reserve-{n}", kind, cap, 16.0 if kind == ResourceKind.BOAT else 28.0, node, node)
        ev.effects.append(f"{rid} ({kind}, {cap} seats) deployed at {node}")

    def _ev_set_policy(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        from .scenarios import POLICIES

        pol = p.get("policy")
        if pol is not None:
            if pol not in POLICIES:
                raise EventError(f"policy must be one of {list(POLICIES)}")
            self.cfg.policy = pol
            self.cfg.weights = dict(POLICIES[pol])
            ev.effects.append(f"priority policy → {pol} {self.cfg.weights}")
        prio = p.get("neighbourhood_priority")
        if prio is not None:
            try:
                bad = not isinstance(prio, dict) or any(k not in self.world.neighbourhoods or not (0.5 <= float(v) <= 3) for k, v in prio.items())
            except (TypeError, ValueError):
                bad = True
            if bad:
                raise EventError("neighbourhood_priority must map neighbourhood id → 0.5..3")
            self.cfg.neighbourhood_priority = {k: float(v) for k, v in prio.items()}
            ev.effects.append(f"neighbourhood priority → {self.cfg.neighbourhood_priority}")
        if not ev.effects:
            raise EventError("nothing to change")
        if p.get("renegotiate"):
            n = 0
            for a in sorted(self.world.assignments.values(), key=lambda a: a.id):
                if a.status == AssignmentStatus.COMMITTED:
                    self.cancel_assignment(a, "renegotiation forced by policy change")
                    n += 1
            ev.effects.append(f"{n} unloaded commitments re-opened for negotiation")

    def _ev_coordinator_outage(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        duration = p.get("duration", 40)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 300:
            raise EventError("duration must be an integer from 1 to 300")
        self.world.coordinator_down_until = max(self.world.coordinator_down_until, self.world.tick + duration)
        ev.effects.append(
            f"Central dispatch unavailable until t={self.world.coordinator_down_until}; "
            "existing missions and local safety continue. Peer communication remains available."
        )

    def _ev_triple_failure(self, ev: SimulationEvent, p: dict[str, Any]) -> None:
        """Deterministically picks the most disruptive targets from the *current* state."""
        w = self.world
        usage: dict[str, int] = {}
        for r in sorted(w.resources.values(), key=lambda r: r.id):
            if not r.assignment_id:
                continue
            path = self.remaining_path(r)
            for x, y in zip(path, path[1:]):
                e = edge_between(w, x, y)
                if e and e.kind == EdgeKind.BRIDGE and e.ground_ok:
                    usage[e.id] = usage.get(e.id, 0) + 1
        bridges = [e for e in w.edges.values() if e.kind == EdgeKind.BRIDGE and not e.manual_closed]
        if not usage:
            # no live route crosses a bridge: pick the open bridge that most pending
            # ground rescues would need (shortest ground paths unit -> open incident)
            for r in sorted(w.resources.values(), key=lambda r: r.id):
                if r.kind != ResourceKind.GROUND or r.status == ResourceStatus.DISABLED:
                    continue
                _dist, paths = sssp(w, r.kind, r.speed_kmh, r.node)
                for inc in sorted(w.incidents.values(), key=lambda i: i.id):
                    if inc.status in (IncidentStatus.RESCUED, IncidentStatus.FALSE_ALARM) or inc.node not in paths:
                        continue
                    pth = paths[inc.node]
                    for x, y in zip(pth, pth[1:]):
                        e = edge_between(w, x, y)
                        if e and e.kind == EdgeKind.BRIDGE and e.ground_ok:
                            usage[e.id] = usage.get(e.id, 0) + 1
        fallback = sorted(bridges, key=lambda e: e.id)
        bridge = p.get("edge_id") or (max(usage.items(), key=lambda kv: (kv[1], kv[0]))[0] if usage else (fallback[min(1, len(fallback) - 1)].id if fallback else None))
        boats = sorted(
            (r for r in w.resources.values() if r.kind == ResourceKind.BOAT and r.status != ResourceStatus.DISABLED),
            key=lambda r: (0 if r.assignment_id and w.assignments[r.assignment_id].status == AssignmentStatus.COMMITTED else 1 if r.assignment_id else 2, r.id),
        )
        boat = p.get("resource_id") or (boats[0].id if boats else None)
        pend: dict[str, int] = {}
        for rsv in w.reservations.values():
            if rsv.status == ReservationStatus.ACTIVE:
                pend[rsv.shelter_id] = pend.get(rsv.shelter_id, 0) + rsv.people
        shelter = p.get("shelter_id") or (max(pend.items(), key=lambda kv: (kv[1], kv[0]))[0] if pend else w.meta.get("default_shelter", "S2"))
        sub = []
        if bridge:
            sub.append(self._apply_event("CLOSE_EDGE", {"edge_id": bridge, "reason": "triple failure: bridge collapse"}, "system"))
        if boat:
            sub.append(self._apply_event("RESOURCE_FAILURE", {"resource_id": boat, "reason": "engine failure (triple failure)"}, "system"))
        sh = w.shelters[shelter]
        sub.append(self._apply_event("SHELTER_CAPACITY", {"shelter_id": shelter, "capacity": sh.occupancy, "reason": "overflow (triple failure)"}, "system"))
        ev.effects.extend(f"[{s.type}] " + "; ".join(s.effects) for s in sub)

    # ================================================================ tick
    def step(self) -> None:
        if self.finished:
            return
        t0 = time.perf_counter()
        w = self.world
        w.tick += 1
        # 1. scheduled scenario events
        for sev in [e for e in self.schedule if int(e.get("tick", -1)) == w.tick]:
            try:
                self._apply_event(sev["type"], dict(sev.get("params") or {}), "scenario")
            except EventError as exc:
                self.audit("event", f"scheduled {sev['type']} skipped: {exc}", {}, "warn")
        # 2. flood + graph availability
        changed = update_flood(w, self.cfg.flood_severity, self.cfg.flood_peak_tick)
        for eid, cause, g_before, _b_before in changed:
            e = w.edges[eid]
            if g_before and not e.ground_ok and e.kind in (EdgeKind.ROAD, EdgeKind.BRIDGE):
                self.audit("event", f"{e.name or e.id} impassable for ground units ({cause}, depth {e.depth:.2f} m)", {"edge": eid}, "warn" if e.kind == EdgeKind.ROAD else "critical")
            elif not g_before and e.ground_ok:
                self.audit("event", f"{e.name or e.id} passable again for ground units", {"edge": eid}, "info")
        snapshot_traversability(w)
        # 3-4. reports + reconciliation
        self._ingest_reports()
        # 5. reservation maintenance
        self._maintain_reservations()
        # 6-9. invalidation + negotiation (in approval mode: refresh the recommendation queue)
        self._prune_recommendations()
        self._microstep("tick")
        # 10. movement
        for res in sorted(w.resources.values(), key=lambda r: r.id):
            self._advance(res)
        # 11. outcomes
        self._process_arrivals()
        self._account_idle()
        # 12-13. invariants + recovery tracking
        violations = audit_invariants(w)
        if violations:
            self.counters["invariant_violations"] += len(violations)
            for v in violations:
                self.safety_log.append({"tick": w.tick, "type": "INVARIANT_VIOLATION", "detail": v})
                self.audit("safety", f"INVARIANT VIOLATION {v}", {}, "critical")
        self._update_disruptions()
        self._publish_all()
        w.bump()
        self.compute_ms += (time.perf_counter() - t0) * 1000
        if w.tick >= self.cfg.duration:
            self.finished = True
            self.audit("event", f"Simulation finished at t={w.tick}", {}, "ok")
        self._snapshot()
        m = self._last_frame["metrics"]
        self.metric_series.append({"tick": w.tick, **{k: m[k] for k in ("people_rescued", "open_incidents", "mean_time_to_service", "unmet_people", "fairness_jain", "utilisation", "deadline_misses", "active_assignments")}})

    def _ingest_reports(self) -> None:
        w = self.world
        while w.pending_reports and w.pending_reports[0].tick <= w.tick:
            rep = w.pending_reports.pop(0)
            report_mod.ingest(self, rep)

    def _maintain_reservations(self) -> None:
        w = self.world
        for rsv in sorted(w.reservations.values(), key=lambda r: r.id):
            if rsv.status != ReservationStatus.ACTIVE:
                continue
            a = w.assignments.get(rsv.assignment_id)
            res = w.resources.get(rsv.resource_id)
            if not a or a.status not in ACTIVE_ASSIGNMENT:
                release_reservation(w, rsv.id)
                continue
            connected = res is not None and res.blackout_until < w.tick
            if rsv.expires_tick < w.tick:
                release_reservation(w, rsv.id, ReservationStatus.EXPIRED)
                self.audit("shelter", f"{rsv.id} at {rsv.shelter_id} EXPIRED (holder {rsv.resource_id} could not renew)", {"reservation": rsv.id}, "warn")
                continue
            if connected and rsv.expires_tick - w.tick <= 3:
                rsv.expires_tick = max(a.eta_dropoff_tick, w.tick) + common.RESERVATION_SLACK
                rsv.version += 1

    def _microstep(self, trigger: str) -> None:
        """Invalidate affected commitments, then run one negotiation round."""
        w = self.world
        self._retreat_from_closed_edges()
        for a in sorted([a for a in w.assignments.values() if a.status in ACTIVE_ASSIGNMENT], key=lambda a: a.id):
            if a.status not in ACTIVE_ASSIGNMENT:
                continue
            res = w.resources[a.resource_id]
            inc = w.incidents[a.incident_id]
            if res.status == ResourceStatus.DISABLED:
                if a.status == AssignmentStatus.COMMITTED:
                    self.cancel_assignment(a, f"{res.id} disabled")
                continue
            if inc.status == IncidentStatus.FALSE_ALARM and a.status == AssignmentStatus.COMMITTED:
                self.cancel_assignment(a, "incident reclassified as false alarm")
                continue
            in_blackout = res.blackout_until >= w.tick
            rsv = w.reservations.get(a.reservation_id)
            if (rsv is None or rsv.status != ReservationStatus.ACTIVE) and not in_blackout and res.status != ResourceStatus.UNLOADING:
                self._renegotiate_shelter(a, res, "retrying for capacity" if res.status == ResourceStatus.HOLDING else f"reservation {rsv.status.lower() if rsv else 'missing'}")
                if a.status not in ACTIVE_ASSIGNMENT:
                    continue
            if res.status not in (ResourceStatus.EN_ROUTE_PICKUP, ResourceStatus.EN_ROUTE_SHELTER):
                continue
            target = self.route_target(res)
            if not res.next_node and not res.route and res.node != target:
                self._replan(res, "route missing")
                continue
            path = self.remaining_path(res)
            if target and path[-1] != target and (res.route or res.next_node):
                self._replan(res, f"target moved to {target} (location revised)")
                continue
            closed = self.belief_for(res)
            if closed is None:
                ok, bad = path_valid(w, res.kind, path)
            else:
                bad = next((e.id for e in self._edges_of(path) if e.id in closed), None)
                ok = bad is None
            if not ok:
                be = w.edges.get(bad or "")
                self._replan(res, f"{(be.name or be.id) if be else bad} no longer traversable" + (" (learned via broadcast)" if closed is not None else ""), cause_edge=bad)
        self._run_strategy(trigger)

    def _edges_of(self, path: list[str]) -> list:
        out = []
        for x, y in zip(path, path[1:]):
            e = edge_between(self.world, x, y)
            if e:
                out.append(e)
        return out

    def _run_strategy(self, trigger: str) -> None:
        if self.strategy != "decentralized" and self.world.tick < self.world.coordinator_down_until:
            return  # central and greedy dispatch need the coordinator; existing missions continue
        if self.strategy == "decentralized":
            rnd = decentralized.run(self, trigger)
        elif self.strategy == "centralized":
            rnd = centralized.run_centralized(self, trigger)
        else:
            rnd = centralized.run_greedy(self, trigger)
        if rnd is not None:
            self.rounds.append(rnd)
            for aid in rnd.commits:
                a = self.world.assignments[aid]
                if a.incident_id in self.needs_reassign:
                    self.needs_reassign.discard(a.incident_id)
                    self.counters["reassignments"] += 1
                if self.world.incidents[a.incident_id].provisional:
                    self.counters["provisional_dispatches"] += 1
            for fn in self.listeners:
                fn("round", rnd)

    def _process_arrivals(self) -> None:
        w = self.world
        for res in sorted(w.resources.values(), key=lambda r: r.id):
            a = w.assignments.get(res.assignment_id or "")
            if res.status == ResourceStatus.LOADING:
                res.timer -= 1
                if res.timer <= 0 and a:
                    self._depart_to_shelter(res, a)
                continue
            if res.status == ResourceStatus.UNLOADING:
                res.timer -= 1
                if res.timer <= 0 and a:
                    self._unload(res, a)
                continue
            if res.next_node or res.route or not a:
                continue
            if res.status == ResourceStatus.EN_ROUTE_PICKUP and res.node == w.incidents[a.incident_id].node:
                self._pickup(res, a)
            elif res.status == ResourceStatus.EN_ROUTE_SHELTER and res.node == w.shelters[a.shelter_id].node:
                res.status = ResourceStatus.UNLOADING
                res.timer = 1

    def _pickup(self, res: RescueResource, a: Assignment) -> None:
        w = self.world
        inc = w.incidents[a.incident_id]
        truths = [w.truth[t] for t in inc.truth_ids if t in w.truth and w.truth[t]["exists"]]
        available = sum(t["remaining"] for t in truths)
        inc.visited = True
        if available <= 0:
            if not truths:
                self.counters["false_alarm_visits"] += 1
                inc.status = IncidentStatus.FALSE_ALARM
                self.audit("incident", f"{res.id} on scene at {inc.id}: nobody found — confirmed false alarm", {"incident": inc.id, "resource": res.id}, "warn")
            else:
                self.audit("incident", f"{res.id} on scene at {inc.id}: everyone already evacuated", {"incident": inc.id}, "info")
            inc.people_estimate = 0
            self.cancel_assignment(a, "on scene: no one left to rescue")
            for other in sorted(w.assignments.values(), key=lambda x: x.id):
                if other.incident_id == inc.id and other.status == AssignmentStatus.COMMITTED:
                    self.cancel_assignment(other, "on-scene unit reports no one left")
            self.needs_reassign.discard(inc.id)
            inc.confidence = 1.0
            inc.version += 1
            self.refresh_incident_status(inc)
            return
        load = min(available, res.capacity)
        rsv = w.reservations.get(a.reservation_id)
        if rsv and rsv.status == ReservationStatus.ACTIVE:
            if load > rsv.people:
                sh = w.shelters[rsv.shelter_id]
                grant = min(load - rsv.people, max(0, sh.effective_free(w)))
                rsv.people += grant
                rsv.version += 1
                load = rsv.people
            elif load < rsv.people:
                rsv.people = load
                rsv.version += 1
        left = load
        for t in truths:
            take = min(t["remaining"], left)
            t["remaining"] -= take
            left -= take
        a.loaded_people = load
        a.status = AssignmentStatus.LOADED
        a.version += 1
        res.load = load
        res.status = ResourceStatus.LOADING
        res.timer = 2
        res.version += 1
        if inc.first_pickup_tick is None:
            inc.first_pickup_tick = w.tick
        inc.people_estimate = available - load
        inc.confidence = 1.0
        inc.version += 1
        delta = load - a.people
        self.audit(
            "incident",
            f"{res.id} picked up {load} at {inc.id}" + (f" (planned {a.people}; on-scene count differs by {delta:+d})" if delta else "") + (f"; {inc.people_estimate} still waiting" if inc.people_estimate else ""),
            {"incident": inc.id, "resource": res.id, "assignment": a.id, "decision": a.decision_id},
            "ok",
        )
        if inc.people_estimate == 0:
            for other in sorted(w.assignments.values(), key=lambda x: x.id):
                if other.incident_id == inc.id and other.status == AssignmentStatus.COMMITTED:
                    self.cancel_assignment(other, f"{res.id} evacuated everyone on scene")
            self.needs_reassign.discard(inc.id)
        self.refresh_incident_status(inc)
        self.publish(inc)

    def _depart_to_shelter(self, res: RescueResource, a: Assignment) -> None:
        w = self.world
        res.status = ResourceStatus.EN_ROUTE_SHELTER
        target = w.shelters[a.shelter_id].node
        reach = plan_from_resource(w, res, self.belief_for(res))
        rsv = w.reservations.get(a.reservation_id)
        if target in reach and rsv and rsv.status == ReservationStatus.ACTIVE:
            mins, path = reach[target]
            self.set_route(res, path)
            a.dropoff_route = path
            a.eta_dropoff_tick = w.tick + int(math.ceil(mins))
        else:
            self._renegotiate_shelter(a, res, "planned shelter unavailable after loading")

    def _unload(self, res: RescueResource, a: Assignment) -> None:
        w = self.world
        sh = w.shelters[a.shelter_id]
        rsv = w.reservations.get(a.reservation_id)
        if rsv and rsv.status == ReservationStatus.ACTIVE and rsv.shelter_id == sh.id:
            rsv.status = ReservationStatus.CONSUMED
            rsv.version += 1
        elif sh.effective_free(w) >= res.load:
            self.audit("shelter", f"{sh.id} admitted {res.load} from {res.id} without a live reservation (capacity available)", {"shelter": sh.id}, "warn")
        else:
            res.status = ResourceStatus.EN_ROUTE_SHELTER
            self._renegotiate_shelter(a, res, f"{sh.id} refused admission (no capacity)")
            return
        sh.occupancy += res.load
        sh.version += 1
        inc = w.incidents[a.incident_id]
        inc.people_rescued += res.load
        a.status = AssignmentStatus.COMPLETED
        a.version += 1
        self.audit("assignment", f"{res.id} delivered {res.load} to {sh.name} ({sh.occupancy}/{sh.capacity}); {a.id} complete", {"assignment": a.id, "resource": res.id, "shelter": sh.id, "incident": inc.id}, "ok")
        res.load = 0
        res.assignment_id = None
        res.status = ResourceStatus.IDLE
        res.version += 1
        inc.version += 1
        self.refresh_incident_status(inc)
        self.publish(inc)

    def _account_idle(self) -> None:
        w = self.world
        demand = any(common.is_eligible(w, i)[0] and unassigned_people(w, i) > 0 for i in w.incidents.values())
        for r in w.resources.values():
            if r.status == ResourceStatus.DISABLED:
                continue
            if r.status == ResourceStatus.IDLE:
                r.idle_ticks += 1
                if demand:
                    r.idle_with_demand_ticks += 1
            else:
                r.busy_ticks += 1

    def _update_disruptions(self) -> None:
        w = self.world
        for d in self.disruptions:
            if d["resolved_tick"] is not None:
                continue
            ok = True
            for iid in d["incidents"]:
                inc = w.incidents.get(iid)
                if inc is None or inc.status in (IncidentStatus.RESCUED, IncidentStatus.FALSE_ALARM):
                    continue
                if common.is_eligible(w, inc)[0] and unassigned_people(w, inc) > 0:
                    ok = False  # affected demand is still uncovered
            for aid in d["holds"]:
                a = w.assignments.get(aid)
                if a and a.status == AssignmentStatus.LOADED and w.resources[a.resource_id].status == ResourceStatus.HOLDING:
                    ok = False
            if ok:
                d["resolved_tick"] = w.tick
                d["recovery"] = w.tick - d["tick"]
                self.audit("event", f"Recovered from {d['event']} ({d['type']}) in {d['recovery']} min", {"event": d["event"]}, "ok")

    # ================================================================ decisions
    def new_decision(self, rnd: NegotiationRound, inc: Incident, cands: list, view: dict[str, Any]) -> dict[str, Any]:
        w = self.world
        _ok, rule = common.is_eligible(w, inc)
        known_ids = {c.resource_id for c in cands}
        others = []
        for r in sorted(w.resources.values(), key=lambda r: r.id):
            if r.id in known_ids:
                continue
            if r.status == ResourceStatus.DISABLED:
                why = "disabled"
            elif r.blackout_until >= w.tick:
                why = f"comms blackout until t={r.blackout_until}"
            elif r.assignment_id:
                why = f"busy with {r.assignment_id} ({r.status.lower()})"
            else:
                why = "not participating"
            others.append({"resource_id": r.id, "feasible": False, "reason": why, "cost": None})
        return {
            "id": self._next_decision_id(),
            "tick": w.tick,
            "round_id": rnd.id,
            "strategy": rnd.strategy,
            "incident_id": inc.id,
            "state_version": w.version,
            "incident": {
                "status": inc.status,
                "confidence": inc.confidence,
                "people": inc.people_estimate,
                "unassigned": view.get("unassigned"),
                "urgency": inc.urgency,
                "deadline": inc.deadline_tick,
                "node": inc.node,
                "neighbourhood": inc.neighbourhood,
                "version": inc.version,
                "known_version": view.get("version"),
                "eligibility": rule,
            },
            "evidence": [
                {"id": r.id, "source": r.source, "reliability": r.reliability, "claim": r.claim, "people": r.people, "urgency": r.urgency, "tick": r.tick, "node": r.node, "note": r.note}
                for r in (w.reports[x] for x in inc.report_ids)
            ],
            "candidates": [c.to_dict() for c in sorted(cands, key=lambda c: (not c.feasible, c.cost if c.feasible else 9, c.resource_id))] + others,
            "rejections": [],
            "selected": None,
            "shelter_log": [],
            "safety_checks": [],
            "outcome": "UNMATCHED",
            "weights": dict(self.cfg.weights),
        }

    def _next_decision_id(self) -> str:
        # a counter, not len(self.decisions): several decisions are opened in one round before
        # any is finalised, and a length-based id made them collide and overwrite each other
        self._decision_seq += 1
        return f"D-{self._decision_seq:05d}"

    def finalize_decision(self, dec: dict[str, Any]) -> None:
        iid = dec["incident_id"]
        outcome = dec["outcome"]
        if outcome in ("UNMATCHED", "RECOMMENDED", "OPERATOR_REJECTED") and self._last_outcome.get(iid) == outcome and not dec.get("recommendation_id"):
            return  # avoid recording identical "still waiting" / "awaiting operator" decisions every tick
        self._last_outcome[iid] = outcome
        dec["explanation"], dec["what_would_change"] = explain(dec)
        self.decisions[dec["id"]] = dec

    def record_safety_rejection(self, res_id: str, inc_id: str, verdict: ShieldResult, round_id: str) -> None:
        self.counters["safety_rejections"] += 1
        if verdict.code == "STALE_STATE":
            self.counters["stale_rejections"] += 1
        self.safety_log.append({"tick": self.world.tick, "type": "COMMIT_REJECTED", "resource": res_id, "incident": inc_id, "code": verdict.code, "detail": verdict.reason, "round": round_id, "checks": [c.to_dict() for c in verdict.checks]})
        self.audit("safety", f"Shield rejected {res_id} → {inc_id}: {verdict.reason}", {"resource": res_id, "incident": inc_id, "round": round_id}, "critical")

    # ================================================================ snapshots
    def _snapshot(self) -> None:
        from .serialize import frame, state_hash

        f = frame(self)
        self.hashes[self.world.tick] = state_hash(self)
        if self.record_frames:
            self.frames[self.world.tick] = f
        self._last_frame = f
        for fn in self.listeners:
            fn("frame", f)

    def last_frame(self) -> dict[str, Any]:
        return self._last_frame

    def fork(self) -> "Engine":
        listeners, self.listeners = self.listeners, []
        try:
            clone = copy.deepcopy(self)
        finally:
            self.listeners = listeners
        return clone


def explain(dec: dict[str, Any]) -> tuple[str, str]:
    """Natural-language explanation generated strictly from the structured record."""
    inc = dec["incident_id"]
    feas = [c for c in dec["candidates"] if c.get("feasible")]
    infeas = [c for c in dec["candidates"] if not c.get("feasible")]
    sel = dec.get("selected")
    parts = []
    change = ""
    if dec["outcome"] in ("COMMITTED", "RECOMMENDED") and sel:
        comp = sel.get("components", {})
        verb = "was committed to" if dec["outcome"] == "COMMITTED" else "is recommended for"
        parts.append(
            f"{sel['resource_id']} {verb} {inc} with the lowest accepted cost {sel['cost']:.3f} among {len(feas)} feasible option(s): "
            f"pickup ETA {sel['eta_pickup']} min, {sel['eta_total']} min door-to-shelter via {dec.get('shelter')} "
            f"(T={comp.get('T', 0):.2f}, R={comp.get('R', 0):.2f}, U={comp.get('U', 0):.2f}, F={comp.get('F', 0):.2f})."
        )
        runner = next((c for c in feas if c["resource_id"] != sel["resource_id"]), None)
        if runner and runner.get("cost") is not None:
            gap = runner["cost"] - sel["cost"]
            wT = max(1e-6, dec["weights"].get("T", 0.35))
            mins = gap / wT * 90
            change = f"{runner['resource_id']} (cost {runner['cost']:.3f}) would have been chosen if its cost were {gap:.3f} lower — roughly {mins:.0f} min faster door-to-shelter, all else equal."
        else:
            change = "No feasible alternative existed; the decision only changes if another unit becomes free or a route opens."
    elif dec["outcome"] == "SAFETY_REJECTED":
        parts.append(f"The proposal for {inc} was rejected by the safety shield ({dec.get('rejection_code')}); the reservation was released and the agent updated its knowledge.")
        change = "Fresh state (re-bid on the current version / updated network) is required."
    elif dec["outcome"] == "NO_SHELTER":
        parts.append(f"A unit was matched to {inc}, but no shelter agent accepted a reservation for the required seats.")
        change = "More shelter capacity, or a smaller load, would allow commitment."
    elif dec["outcome"] == "INFEASIBLE":
        parts.append(f"The operator directive for {inc} was infeasible.")
    else:
        parts.append(f"{inc} remained unassigned this round ({len(feas)} feasible option(s)).")
        change = "A unit becoming idle, a route re-opening, or shelter capacity freeing up would trigger a new round."
    for r in dec.get("rejections", [])[:3]:
        parts.append(f"{r['resource']} declined: {r['reason']}.")
    for c in infeas[:4]:
        parts.append(f"{c['resource_id']}: {c['reason']}.")
    if dec["incident"]["eligibility"].startswith(("E2", "E3", "E4")):
        parts.append(f"Dispatch is provisional — {dec['incident']['eligibility']}.")
    return " ".join(parts), change
