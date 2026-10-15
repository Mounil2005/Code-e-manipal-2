"""Shared coordination machinery used by every strategy.

All strategies (decentralised negotiation, centralised optimiser, greedy) evaluate
options with the same cost function, reserve shelter capacity through the same
shelter agents and pass through the same safety shield. They differ only in *who*
evaluates options, *what information* they have, and *how conflicts are resolved*.

Dispatch cost (lower is better), every component normalised to [0, 1]:

    C_ij = wT*T + wR*R + wU*U + wF*F
    T = min(1, (eta_pickup + eta_dropoff) / 90 min)
    R = max route-risk attribute over pickup + dropoff edges
    U = 0.5 * (5 - urgency) / 4 + 0.5 * clamp((now + eta_pickup - deadline) / 30, 0, 1)
    F = 1 - W_n / W_max  (W_n: mean wait of open incidents in the incident's
        neighbourhood; W_max: the worst neighbourhood) — serving starved areas is cheaper
        F is shifted by (priority_n - 1) * 0.5 when a neighbourhood priority is set.
    +0.05 provisional penalty when the incident is still UNVERIFIED.

Hard constraints (routes, capacities, versions) are enforced before cost is considered.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from ..routing.router import path_risk, plan_from_resource, sssp
from ..safety.shield import validate_commit
from ..simulation.state import (
    ACTIVE_ASSIGNMENT,
    Assignment,
    AssignmentStatus,
    CapacityReservation,
    Incident,
    IncidentStatus,
    ReservationStatus,
    RescueResource,
    ResourceStatus,
    WorldState,
)

PROVISIONAL_THRESHOLD = 0.5
VERIFIED_THRESHOLD = 0.8
UNVERIFIED_WAIT_RULE = 20  # ticks before an unverified report gets a verification visit
RESERVATION_SLACK = 20


@dataclass
class Message:
    seq: int
    tick: int
    type: str  # REQUEST BID PROVISIONAL_ACCEPT REJECT RESERVE ACCEPT_RESERVATION REJECT_RESERVATION SAFETY_REJECT COMMIT CANCEL EXPIRE RENEGOTIATE DIRECTIVE OFFER
    frm: str
    to: str
    incident: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "tick": self.tick, "type": self.type, "from": self.frm, "to": self.to, "incident": self.incident, "payload": self.payload, "reason": self.reason}


@dataclass
class NegotiationRound:
    id: str
    tick: int
    strategy: str
    trigger: str
    messages: list[Message] = field(default_factory=list)
    iterations: int = 0
    commits: list[str] = field(default_factory=list)
    rejections: int = 0
    safety_rejections: int = 0
    duration_ms: float = 0.0
    incidents: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    matching: list[dict[str, Any]] = field(default_factory=list)

    def msg(self, type_: str, frm: str, to: str, incident: str = "", reason: str = "", **payload: Any) -> Message:
        m = Message(len(self.messages) + 1, self.tick, type_, frm, to, incident, payload, reason)
        self.messages.append(m)
        if type_ in ("REJECT", "REJECT_RESERVATION", "SAFETY_REJECT"):
            self.rejections += 1
        if type_ == "SAFETY_REJECT":
            self.safety_rejections += 1
        return m

    def to_dict(self, with_messages: bool = True) -> dict[str, Any]:
        d = {
            "id": self.id,
            "tick": self.tick,
            "strategy": self.strategy,
            "trigger": self.trigger,
            "iterations": self.iterations,
            "commits": self.commits,
            "rejections": self.rejections,
            "safety_rejections": self.safety_rejections,
            "duration_ms": round(self.duration_ms, 2),
            "incidents": self.incidents,
            "resources": self.resources,
            "message_count": len(self.messages),
            "matching": self.matching,
        }
        if with_messages:
            d["messages"] = [m.to_dict() for m in self.messages]
        return d


@dataclass
class Candidate:
    resource_id: str
    incident_id: str
    feasible: bool
    reason: str = ""
    people: int = 0
    eta_pickup: float = math.inf
    eta_dropoff: float = math.inf
    pickup_path: list[str] = field(default_factory=list)
    shelter_options: list[tuple[str, float, list[str]]] = field(default_factory=list)
    cost: float = math.inf
    components: dict[str, float] = field(default_factory=dict)
    res_version: int = 0
    inc_version: int = 0
    knowledge: str = "truth"

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "feasible": self.feasible,
            "reason": self.reason,
            "people": self.people,
            "eta_pickup": None if math.isinf(self.eta_pickup) else round(self.eta_pickup, 1),
            "eta_total": None if math.isinf(self.eta_dropoff) else round(self.eta_dropoff, 1),
            "cost": None if math.isinf(self.cost) else round(self.cost, 4),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "shelter": self.shelter_options[0][0] if self.shelter_options else None,
            "pickup_path": self.pickup_path,
            "versions": {"resource": self.res_version, "incident": self.inc_version},
            "knowledge": self.knowledge,
        }


# ---------------------------------------------------------------- incident helpers


def committed_people(world: WorldState, inc_id: str) -> int:
    return sum(a.people for a in world.assignments.values() if a.incident_id == inc_id and a.status == AssignmentStatus.COMMITTED)


def unassigned_people(world: WorldState, inc: Incident) -> int:
    return max(0, inc.people_estimate - committed_people(world, inc.id))


def is_eligible(world: WorldState, inc: Incident) -> tuple[bool, str]:
    if inc.status in (IncidentStatus.RESCUED, IncidentStatus.FALSE_ALARM):
        return False, inc.status
    if inc.confidence >= VERIFIED_THRESHOLD or inc.visited:
        return True, "E1 verified"
    if inc.confidence >= PROVISIONAL_THRESHOLD:
        return True, "E2 suspected — provisional dispatch"
    if inc.urgency >= 4:
        return True, "E3 unverified but urgency>=4 — life-safety provisional dispatch"
    if world.tick - inc.reported_tick >= UNVERIFIED_WAIT_RULE:
        return True, "E4 unverified for 20+ min — verification visit"
    return False, f"awaiting corroboration (confidence {inc.confidence:.2f})"


def open_requests(world: WorldState) -> list[Incident]:
    out = []
    for inc in world.incidents.values():
        ok, _ = is_eligible(world, inc)
        if ok and unassigned_people(world, inc) > 0:
            out.append(inc)
    out.sort(key=priority_key)
    return out


def priority_key(inc: Incident) -> tuple:
    """Deterministic incident priority: urgency, then deadline, then confidence, then id."""
    return (-inc.urgency, inc.deadline_tick, -round(inc.confidence, 3), inc.id)


def available_resources(world: WorldState) -> list[RescueResource]:
    return sorted(
        (r for r in world.resources.values() if r.status == ResourceStatus.IDLE and r.blackout_until < world.tick and r.load == 0),
        key=lambda r: r.id,
    )


def neighbourhood_waits(world: WorldState) -> dict[str, float]:
    waits: dict[str, list[int]] = {n: [] for n in world.neighbourhoods}
    for inc in world.incidents.values():
        if inc.status in (IncidentStatus.RESCUED, IncidentStatus.FALSE_ALARM):
            continue
        if inc.first_pickup_tick is None:
            waits.setdefault(inc.neighbourhood, []).append(world.tick - inc.reported_tick)
    return {n: (sum(v) / len(v) if v else 0.0) for n, v in waits.items()}


def shelter_offers(world: WorldState) -> dict[str, int]:
    return {s.id: max(0, s.effective_free(world)) for s in sorted(world.shelters.values(), key=lambda s: s.id)}


# ---------------------------------------------------------------- evaluation


def evaluate(
    world: WorldState,
    cfg: Any,
    res: RescueResource,
    inc_view: dict[str, Any],
    offers: dict[str, int],
    waits: dict[str, float],
    closed: Optional[frozenset] = None,
    plan_cache: Optional[dict] = None,
    knowledge: str = "truth",
) -> Candidate:
    """Evaluate one (resource, incident) option. `inc_view` is the incident as the
    evaluator knows it (a delayed publication for decentralised agents)."""
    inc_id = inc_view["id"]
    cand = Candidate(res.id, inc_id, False, res_version=res.version, inc_version=inc_view["version"], knowledge=knowledge)
    people = min(inc_view["unassigned"], res.capacity)
    cand.people = people
    if people <= 0:
        cand.reason = "nothing left to cover"
        return cand
    key = (res.id, closed)
    if plan_cache is not None and key in plan_cache:
        reach = plan_cache[key]
    else:
        reach = plan_from_resource(world, res, closed)
        if plan_cache is not None:
            plan_cache[key] = reach
    target = inc_view["node"]
    if target not in reach:
        cand.reason = f"no {res.kind} route to {target}" + (" (believed network)" if closed is not None else "")
        return cand
    eta_p, ppath = reach[target]
    cand.eta_pickup, cand.pickup_path = eta_p, ppath
    dkey = ("drop", res.kind, target, closed)
    if plan_cache is not None and dkey in plan_cache:
        dist, paths = plan_cache[dkey]
    else:
        dist, paths = sssp(world, res.kind, res.speed_kmh, target, closed)
        if plan_cache is not None:
            plan_cache[dkey] = (dist, paths)
    opts = []
    for s in sorted(world.shelters.values(), key=lambda s: s.id):
        if s.node in dist and offers.get(s.id, 0) >= people:
            opts.append((s.id, dist[s.node], paths[s.node]))
    if not opts:
        reachable = [s.id for s in world.shelters.values() if s.node in dist]
        cand.reason = "no reachable shelter with capacity" if reachable else "no shelter reachable from incident"
        return cand
    opts.sort(key=lambda o: (o[1], o[0]))
    cand.shelter_options = opts
    cand.eta_dropoff = eta_p + 2 + opts[0][1]  # +2 ticks loading
    w = cfg.weights
    T = min(1.0, cand.eta_dropoff / 90.0)
    R = max(path_risk(world, ppath), path_risk(world, opts[0][2]))
    urg = inc_view["urgency"]
    slack = inc_view["deadline"] - (world.tick + eta_p)
    U = 0.5 * (5 - urg) / 4 + 0.5 * min(1.0, max(0.0, -slack / 30))
    wmax = max(waits.values()) if waits else 0.0
    F = 0.5 if wmax <= 0 else 1.0 - waits.get(inc_view["nbhd"], 0.0) / wmax
    prio = cfg.neighbourhood_priority.get(inc_view["nbhd"], 1.0) if cfg.neighbourhood_priority else 1.0
    F = min(1.0, max(0.0, F - (prio - 1.0) * 0.5))
    penalty = 0.05 if inc_view.get("confidence", 1) < PROVISIONAL_THRESHOLD else 0.0
    cost = w["T"] * T + w["R"] * R + w["U"] * U + w["F"] * F + penalty
    cand.components = {"T": T, "R": R, "U": U, "F": F, "penalty": penalty}
    cand.cost = cost
    cand.feasible = True
    return cand


def incident_view(world: WorldState, inc: Incident) -> dict[str, Any]:
    return {
        "id": inc.id,
        "version": inc.version,
        "node": inc.node,
        "urgency": inc.urgency,
        "deadline": inc.deadline_tick,
        "unassigned": unassigned_people(world, inc),
        "nbhd": inc.neighbourhood,
        "confidence": inc.confidence,
        "status": inc.status,
        "tick": world.tick,
    }


# ---------------------------------------------------------------- shelter agent


def request_reservation(world: WorldState, shelter_id: str, people: int, res_id: str, inc_id: str, eta_dropoff_tick: int, assignment_id: str) -> tuple[Optional[CapacityReservation], str]:
    sh = world.shelters[shelter_id]
    free = sh.effective_free(world)
    if free < people:
        return None, f"{sh.id} effective capacity {free} < {people} requested"
    rid = world.next_id("RSV")
    rsv = CapacityReservation(rid, sh.id, assignment_id, res_id, inc_id, people, world.tick, eta_dropoff_tick + RESERVATION_SLACK)
    world.reservations[rid] = rsv
    sh.version += 1
    return rsv, f"{sh.id} reserved {people} seats until t={rsv.expires_tick} (free after: {free - people})"


def release_reservation(world: WorldState, rsv_id: str, status: str = ReservationStatus.RELEASED) -> None:
    rsv = world.reservations.get(rsv_id)
    if rsv and rsv.status == ReservationStatus.ACTIVE:
        rsv.status = status
        rsv.version += 1
        world.shelters[rsv.shelter_id].version += 1


# ---------------------------------------------------------------- commit


def try_commit(engine: Any, cand: Candidate, rnd: NegotiationRound, decision: dict[str, Any]) -> Optional[Assignment]:
    """Shelter reservation -> safety validation -> atomic commit.
    In approval mode the agents' choice becomes a recommendation for the operator instead."""
    if getattr(engine.cfg, "approval_mode", False) and rnd.strategy != "operator":
        return engine.recommend(cand, rnd, decision)
    world: WorldState = engine.world
    res = world.resources[cand.resource_id]
    inc = world.incidents[cand.incident_id]
    aid = world.next_id("A")
    shelter_log = []
    for sid, mins, dpath in cand.shelter_options:
        eta_drop_tick = world.tick + int(math.ceil(cand.eta_pickup + 2 + mins))
        rnd.msg("RESERVE", res.id, sid, inc.id, people=cand.people, eta=eta_drop_tick)
        rsv, why = request_reservation(world, sid, cand.people, res.id, inc.id, eta_drop_tick, aid)
        if rsv is None:
            rnd.msg("REJECT_RESERVATION", sid, res.id, inc.id, reason=why)
            shelter_log.append({"shelter": sid, "accepted": False, "reason": why})
            continue
        rnd.msg("ACCEPT_RESERVATION", sid, res.id, inc.id, reservation=rsv.id, expires=rsv.expires_tick, reason=why)
        shelter_log.append({"shelter": sid, "accepted": True, "reason": why, "reservation": rsv.id})
        verdict = validate_commit(
            world,
            resource_id=res.id,
            incident_id=inc.id,
            people=cand.people,
            pickup_path=cand.pickup_path,
            dropoff_path=dpath,
            reservation_id=rsv.id,
            resource_version=cand.res_version,
            incident_version=cand.inc_version,
        )
        decision["safety_checks"] = [c.to_dict() for c in verdict.checks]
        decision["shelter_log"] = shelter_log
        if not verdict.ok:
            release_reservation(world, rsv.id)
            rnd.msg("SAFETY_REJECT", "safety-shield", res.id, inc.id, reason=verdict.reason, code=verdict.code)
            engine.record_safety_rejection(res.id, inc.id, verdict, rnd.id)
            if verdict.bad_edge and verdict.bad_edge in world.edges:
                res.known_closed[verdict.bad_edge] = world.tick  # agent learns from the rejection
            decision["outcome"] = "SAFETY_REJECTED"
            decision["rejection_code"] = verdict.code
            return None
        a = Assignment(
            id=aid,
            resource_id=res.id,
            incident_id=inc.id,
            shelter_id=sid,
            reservation_id=rsv.id,
            people=cand.people,
            created_tick=world.tick,
            strategy=world.strategy,
            round_id=rnd.id,
            decision_id=decision["id"],
            eta_pickup_tick=world.tick + int(math.ceil(cand.eta_pickup)),
            eta_dropoff_tick=eta_drop_tick,
            pickup_route=list(cand.pickup_path),
            dropoff_route=list(dpath),
        )
        world.assignments[aid] = a
        res.assignment_id = aid
        res.status = ResourceStatus.EN_ROUTE_PICKUP
        res.version += 1
        engine.set_route(res, cand.pickup_path)
        inc.version += 1
        engine.refresh_incident_status(inc)
        rnd.msg("COMMIT", "coordination", res.id, inc.id, assignment=aid, shelter=sid, people=cand.people, eta_pickup=a.eta_pickup_tick, eta_dropoff=a.eta_dropoff_tick)
        rnd.commits.append(aid)
        decision["outcome"] = "COMMITTED"
        decision["assignment_id"] = aid
        decision["shelter"] = sid
        engine.audit("assignment", f"{res.id} committed to {inc.id} ({cand.people} people) → {sid}; ETA pickup t={a.eta_pickup_tick}, dropoff t={a.eta_dropoff_tick}", {"assignment": aid, "resource": res.id, "incident": inc.id, "shelter": sid, "round": rnd.id, "decision": decision["id"]}, "ok")
        return a
    decision["shelter_log"] = shelter_log
    decision["outcome"] = "NO_SHELTER"
    rnd.msg("REJECT", "coordination", res.id, inc.id, reason="no shelter accepted a reservation")
    return None


def active_assignment_count(world: WorldState) -> int:
    return sum(1 for a in world.assignments.values() if a.status in ACTIVE_ASSIGNMENT)
