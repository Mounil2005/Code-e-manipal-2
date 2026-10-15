"""RESQNET AI — FastAPI application (REST + WebSocket).

The backend is the single authoritative source of simulation state. The UI only
renders frames it receives here; every user action is an event validated by the
engine and logged for exact replay.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..analysis.runs import adversarial, benchmark, counterfactual, rebuild, verify_replay
from ..metrics.metrics import compute_metrics
from ..persistence import store
from ..routing.router import believed_closed
from ..simulation.engine import EVENT_TYPES, Engine, EventError
from ..simulation.scenarios import POLICIES, PRESETS, STRATEGIES, ScenarioConfig, get_preset
from ..live import cwc as cwc_mod
from ..live.feed import FeedError, FeedHub, drill_plan, live_reading, priority_board, synthetic_messages, synthetic_plan
from ..simulation.maps import available_maps, is_known
from ..simulation.serialize import static_world
from ..simulation.state import ResourceStatus
from .report import build_report, report_pdf

app = FastAPI(title="RESQNET AI", version="1.0.0", description="Multi-agent flood rescue coordination digital twin (synthetic research simulation).")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SPEEDS = (0.5, 1, 2, 4, 8, 16)


# ================================================================ errors
def err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@app.exception_handler(HTTPException)
async def http_error(_req: Any, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "ERROR", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


# ================================================================ sessions
class Session:
    def __init__(self, sim_id: str, cfg: ScenarioConfig, strategy: str):
        self.id = sim_id
        self.cfg = cfg
        self.strategy = strategy
        self.engine = Engine(copy.deepcopy(cfg), strategy)
        self.status = "ready"
        self.speed = 2.0
        self.task: Optional[asyncio.Task] = None
        self.clients: set[WebSocket] = set()
        self.msg_seq = 0
        self._audit_sent = 0
        self._round_sent = 0
        self.created = time.time()
        self.feed = FeedHub()
        self.feed_task: Optional[asyncio.Task] = None

    def summary(self) -> dict[str, Any]:
        e = self.engine
        return {
            "id": self.id,
            "scenario": self.cfg.key,
            "scenario_name": self.cfg.name,
            "strategy": self.strategy,
            "status": self.status,
            "speed": self.speed,
            "tick": e.world.tick,
            "duration": self.cfg.duration,
            "version": e.world.version,
            "seed": self.cfg.seed,
            "map": self.cfg.map,
            "map_name": e.world.meta.get("name", self.cfg.map),
            "flood_event": e.world.meta.get("flood_event"),
            "config": self.cfg.to_dict(),
            "injected": e.injected,
        }

    def persist(self) -> None:
        try:
            store.save_simulation(self.id, self.cfg.key, self.strategy, self.cfg.to_dict(), self.engine.injected, self.engine.world.tick, self.status, compute_metrics(self.engine) if self.engine.finished else None)
        except Exception:  # persistence must never break the live simulation
            pass

    def delta_message(self, kind: str = "frame") -> dict[str, Any]:
        e = self.engine
        self.msg_seq += 1
        audit = e.audit_log[self._audit_sent :]
        self._audit_sent = len(e.audit_log)
        rounds = e.rounds[self._round_sent :]
        self._round_sent = len(e.rounds)
        return {
            "type": kind,
            "seq": self.msg_seq,
            "sim_id": self.id,
            "status": self.status,
            "speed": self.speed,
            "state_version": e.world.version,
            "event_id": e.events[-1].id if e.events else None,
            "frame": e.last_frame(),
            "audit": [_audit_dict(a) for a in audit[-200:]],
            "rounds": [r.to_dict(with_messages=False) for r in rounds[-50:]],
            "feed": self.feed.unsent(),
            "feed_status": self.feed.status(e),
        }

    def full_message(self, kind: str) -> dict[str, Any]:
        """Non-advancing snapshot used for (re)connects; broadcasts stay incremental."""
        e = self.engine
        return {
            "type": kind,
            "seq": self.msg_seq,
            "sim_id": self.id,
            "status": self.status,
            "speed": self.speed,
            "state_version": e.world.version,
            "event_id": e.events[-1].id if e.events else None,
            "frame": e.last_frame(),
            "summary": self.summary(),
            "audit": [_audit_dict(a) for a in e.audit_log[-200:]],
            "rounds": [r.to_dict(with_messages=False) for r in e.rounds[-40:]],
            "feed": self.feed.messages[-120:],
            "feed_status": self.feed.status(e),
            "full": True,
        }

    async def broadcast(self, kind: str = "frame") -> None:
        if not self.clients:
            self._audit_sent = len(self.engine.audit_log)
            self._round_sent = len(self.engine.rounds)
            return
        msg = json.dumps(self.delta_message(kind), default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def run_loop(self) -> None:
        try:
            while self.status == "running":
                self.engine.step()
                if self.engine.finished:
                    self.status = "finished"
                    self.persist()
                await self.broadcast()
                if self.engine.world.tick % 10 == 0:
                    self.persist()
                if self.status != "running":
                    break
                await asyncio.sleep(1.0 / self.speed)
        except asyncio.CancelledError:
            pass


async def run_feed(s: Session, mode: str, p: dict[str, Any]) -> None:
    """Background feed: replays the 2018 gauge record, a synthetic flood, or polls CWC live, and
    pushes every message through the one door (FeedHub.apply)."""
    import random

    hub, e = s.feed, s.engine
    rng = random.Random(e.cfg.seed * 1009 + 7)
    interval = max(0.5, min(30.0, float(p.get("interval_s", 3.0))))
    src = {"drill": "drill", "synthetic": "synthetic", "live": "live"}[mode]

    def push(msg: dict[str, Any]) -> None:
        try:
            hub.apply(e, msg)
        except (FeedError, EventError):
            pass  # a stale target (e.g. a road already closed) is skipped, never fatal

    try:
        if mode == "live":
            last = None
            hub.status_note = "polling CWC every 60 s (stations update hourly)"
            while hub.running and not e.finished:
                r = await asyncio.to_thread(live_reading, e.world.map_id)
                if r is None:
                    hub.status_note = "CWC unreachable; holding the last reading"
                elif r["time"] != last:
                    last = r["time"]
                    push({"type": "gauge", "source": "live", "level_m": r["level_m"], "station": r["station"], "time": r["time"], "label": "stale" if r.get("stale") else ""})
                    for d in r.get("dams", []):
                        if d.get("outflow_m3s") is not None:
                            push({"type": "dam", "source": "live", "dam": d["name"], "outflow_m3s": d["outflow_m3s"], "time": d.get("time")})
                    hub.status_note = f"live: {r['station']} {r['level_m']} m at {r['time']}"
                if p.get("synthetic_sos"):
                    ramp = e.world.river_override or 0.0
                    for m in synthetic_messages(e, rng, ramp, 1.0, p, "synthetic"):
                        push(m)
                await s.broadcast("feed")
                for _ in range(int(max(5, float(p.get("poll_s", 60))))):
                    if not hub.running:
                        break
                    await asyncio.sleep(1)
        else:
            plan = drill_plan(e.world.map_id) if mode == "drill" else synthetic_plan(e.world.map_id, p)
            hub.status_note = f"{len(plan)} hourly readings, one every {interval:g} s"
            last_level = None
            for i, row in enumerate(plan):
                if not hub.running or e.finished:
                    break
                msg = {"type": "gauge", "source": src, "level_m": row["level_m"], "station": row["station"], "time": row["time"]}
                if "ramp" in row:
                    msg["ramp"] = row["ramp"]
                push(msg)
                last_level = row["level_m"]
                ramp = e.world.river_override or 0.0
                extra = synthetic_messages(e, rng, ramp, row.get("rain_factor", 1.0), p, "synthetic")
                gap = interval / (len(extra) + 1)
                await s.broadcast("feed")
                for m in extra:
                    await asyncio.sleep(gap)
                    if not hub.running:
                        break
                    push(m)
                    await s.broadcast("feed")
                await asyncio.sleep(gap)
                hub.status_note = f"reading {i + 1}/{len(plan)}: {row['station']} {row['level_m']} m ({row['time']})"
            if mode == "drill" and hub.running and e.world.map_id == "kerala_chengannur":
                hub.status_note = f"gauge went silent at the 2018 peak ({last_level} m, 16 Aug 08:00); holding the last reading"
            elif hub.running:
                hub.status_note = "feed complete"
    finally:
        hub.running = False
        await s.broadcast("feed")


SESSIONS: dict[str, Session] = {}
JOBS: dict[str, dict[str, Any]] = {}


def _audit_dict(a: Any) -> dict[str, Any]:
    return {"id": a.id, "tick": a.tick, "category": a.category, "message": a.message, "refs": a.refs, "state_version": a.state_version, "severity": a.severity}


def get_session(sim_id: str) -> Session:
    s = SESSIONS.get(sim_id)
    if s:
        return s
    rec = store.load_simulation(sim_id)
    if not rec:
        raise err(404, "NOT_FOUND", f"simulation {sim_id} not found")
    # event-sourced restore: rebuild the run from its config + injected-event log
    cfg = ScenarioConfig.from_dict(rec["config"])
    s = Session(sim_id, cfg, rec["strategy"])
    s.engine = rebuild(cfg, rec["strategy"], rec["injected"], rec["tick"], record_frames=True)
    s.status = "finished" if s.engine.finished else "paused"
    SESSIONS[sim_id] = s
    return s


# ================================================================ models
class CreateSimulation(BaseModel):
    scenario: str = "normal"
    strategy: str = "decentralized"
    seed: Optional[int] = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    map: Optional[str] = None  # map source id; see GET /api/maps


class FeedStartIn(BaseModel):
    mode: str = "drill"  # drill | synthetic | live
    params: dict[str, Any] = Field(default_factory=dict)
    speed: Optional[float] = None


class EventIn(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class StepIn(BaseModel):
    ticks: int = 1


class SpeedIn(BaseModel):
    speed: float = 2


class ForkIn(BaseModel):
    tick: Optional[int] = None
    modifications: dict[str, Any] = Field(default_factory=dict)
    horizon: Optional[int] = None


class BenchmarkIn(BaseModel):
    scenario: str = "triple_failure"
    strategies: list[str] = Field(default_factory=lambda: ["decentralized", "centralized", "greedy"])
    seeds: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    overrides: dict[str, Any] = Field(default_factory=dict)


class AdversarialIn(BaseModel):
    scenario: str = "normal"
    strategy: str = "decentralized"
    n_failures: int = 3
    categories: list[str] = Field(default_factory=lambda: ["bridge", "boat", "shelter"])
    trials: int = 12
    seed: int = 42
    duration: Optional[int] = None
    map: Optional[str] = None


ALLOWED_OVERRIDES = {
    "seed", "duration", "flood_severity", "flood_peak_tick", "initial_incidents", "arrival_incidents", "arrival_window", "boats", "ground_units",
    "shelter_capacity", "shelter_occupancy", "report_noise", "duplicate_rate", "delayed_report_rate", "false_alarm_rate", "contradiction_rate",
    "comm_delay", "policy", "weights", "neighbourhood_priority", "schedule", "name", "map", "flood_event", "extra_shelters",
}


def make_config(scenario: str, seed: Optional[int], overrides: dict[str, Any]) -> ScenarioConfig:
    if scenario not in PRESETS:
        raise err(400, "BAD_SCENARIO", f"unknown scenario '{scenario}'")
    bad = set(overrides) - ALLOWED_OVERRIDES
    if bad:
        raise err(400, "BAD_OVERRIDE", f"unsupported override(s): {sorted(bad)}")
    ov = dict(overrides)
    if seed is not None:
        ov["seed"] = seed
    if "map" in ov and ov["map"] is not None and not is_known(str(ov["map"])):
        raise err(400, "BAD_MAP", f"unknown map '{ov['map']}'")
    cfg = get_preset(scenario, **ov)
    checks = [
        (1 <= cfg.duration <= 1440, "duration must be 1..1440"),
        (0.2 <= cfg.flood_severity <= 2.5, "flood_severity must be 0.2..2.5"),
        (0 <= cfg.boats <= 7, "boats must be 0..7"),
        (0 <= cfg.ground_units <= 6, "ground_units must be 0..6"),
        (0 <= cfg.initial_incidents <= 40 and 0 <= cfg.arrival_incidents <= 60, "incident counts out of range"),
        (0 <= cfg.comm_delay <= 20, "comm_delay must be 0..20"),
        (all(0 <= x <= 1 for x in (cfg.report_noise, cfg.duplicate_rate, cfg.delayed_report_rate, cfg.false_alarm_rate, cfg.contradiction_rate)), "rates must be 0..1"),
        (cfg.policy in POLICIES, f"policy must be one of {list(POLICIES)}"),
        (set(cfg.weights) >= {"T", "R", "U", "F"} and all(0 <= float(v) <= 1 for v in cfg.weights.values()), "weights need T,R,U,F in 0..1"),
        (len(cfg.shelter_capacity) == 3 and all(0 <= int(c) <= 500 for c in cfg.shelter_capacity), "shelter_capacity needs 3 values 0..500"),
    ]
    for ok, msg in checks:
        if not ok:
            raise err(422, "INVALID_CONFIG", msg)
    for ev in cfg.schedule:
        if ev.get("type") not in EVENT_TYPES:
            raise err(422, "INVALID_CONFIG", f"scheduled event type {ev.get('type')} unknown")
    return cfg


# ================================================================ meta
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "sessions": len(SESSIONS), "time": time.time()}


@app.get("/api/scenarios")
def scenarios() -> dict[str, Any]:
    return {
        "scenarios": [c.to_dict() for c in PRESETS.values()],
        "policies": POLICIES,
        "strategies": list(STRATEGIES),
        "event_types": list(EVENT_TYPES),
    }


@app.get("/api/maps")
def maps() -> dict[str, Any]:
    return {"maps": available_maps()}


@app.get("/api/world")
def world(map: str = "riverbend") -> dict[str, Any]:
    if not is_known(map):
        raise err(400, "BAD_MAP", f"unknown map '{map}'")
    return static_world(Engine(get_preset("normal", map=map), record_frames=False).world)


# ================================================================ simulations
@app.post("/api/simulations")
async def create_simulation(body: CreateSimulation) -> dict[str, Any]:
    if body.strategy not in STRATEGIES:
        raise err(400, "BAD_STRATEGY", f"strategy must be one of {list(STRATEGIES)}")
    overrides = dict(body.overrides)
    if body.map:
        overrides["map"] = body.map
    cfg = make_config(body.scenario, body.seed, overrides)
    sim_id = uuid.uuid4().hex[:10]
    s = Session(sim_id, cfg, body.strategy)
    SESSIONS[sim_id] = s
    s.persist()
    return {**s.summary(), "frame": s.engine.last_frame()}


@app.get("/api/simulations")
def list_sims() -> dict[str, Any]:
    live = [s.summary() | {"live": True} for s in sorted(SESSIONS.values(), key=lambda s: -s.created)]
    ids = {x["id"] for x in live}
    stored = [x | {"live": False} for x in store.list_simulations() if x["id"] not in ids]
    return {"simulations": [{k: v for k, v in x.items() if k not in ("config", "injected")} for x in live] + stored}


@app.get("/api/simulations/{sim_id}")
def get_sim(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    return {**s.summary(), "frame": s.engine.last_frame()}


@app.post("/api/simulations/{sim_id}/start")
async def start(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    if s.engine.finished:
        raise err(409, "FINISHED", "simulation has finished; reset to run again")
    if s.status != "running":
        s.status = "running"
        s.task = asyncio.create_task(s.run_loop())
    await s.broadcast("status")
    return s.summary()


@app.post("/api/simulations/{sim_id}/pause")
async def pause(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    if s.status == "running":
        s.status = "paused"
        if s.task:
            s.task.cancel()
    s.persist()
    await s.broadcast("status")
    return s.summary()


@app.post("/api/simulations/{sim_id}/step")
async def step(sim_id: str, body: StepIn) -> dict[str, Any]:
    s = get_session(sim_id)
    if s.status == "running":
        raise err(409, "RUNNING", "pause the simulation before stepping")
    n = max(1, min(120, body.ticks))
    for _ in range(n):
        if s.engine.finished:
            break
        s.engine.step()
    s.status = "finished" if s.engine.finished else "paused"
    s.persist()
    await s.broadcast()
    return {**s.summary(), "frame": s.engine.last_frame()}


@app.post("/api/simulations/{sim_id}/reset")
async def reset(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    if s.task:
        s.task.cancel()
    s.feed.running = False
    if s.feed_task:
        s.feed_task.cancel()
    s.feed = FeedHub()
    s.engine = Engine(copy.deepcopy(s.cfg), s.strategy)
    s.status = "ready"
    s._audit_sent = 0
    s._round_sent = 0
    s.persist()
    await s.broadcast("reset")
    return {**s.summary(), "frame": s.engine.last_frame()}


@app.post("/api/simulations/{sim_id}/speed")
async def set_speed(sim_id: str, body: SpeedIn) -> dict[str, Any]:
    s = get_session(sim_id)
    if body.speed not in SPEEDS:
        raise err(400, "BAD_SPEED", f"speed must be one of {SPEEDS}")
    s.speed = body.speed
    await s.broadcast("status")
    return s.summary()


# ================================================================ live feed
@app.get("/api/live/cwc")
async def live_cwc(map: str = "kerala_aluva") -> dict[str, Any]:
    if map not in cwc_mod.STATIONS:
        return {"available": False, "reason": f"no CWC station configured for {map}"}
    return await asyncio.to_thread(cwc_mod.latest, map)


@app.post("/api/simulations/{sim_id}/feed")
async def feed_message(sim_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """The one door for real-time data: gauge readings, SOS, duplicate / contradictory reports,
    road reports, camp updates, dam releases. External senders use this too."""
    s = get_session(sim_id)
    try:
        rec = s.feed.apply(s.engine, {**body, "source": body.get("source") or "external"})
    except FeedError as exc:
        raise err(400, "BAD_FEED", str(exc))
    except EventError as exc:
        raise err(422, "FEED_REJECTED", str(exc))
    await s.broadcast("feed")
    return {"message": rec, "frame": s.engine.last_frame()}


@app.get("/api/simulations/{sim_id}/feed")
def feed_log(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    return {"status": s.feed.status(s.engine), "messages": s.feed.messages[-200:]}


@app.post("/api/simulations/{sim_id}/feed/start")
async def feed_start(sim_id: str, body: FeedStartIn) -> dict[str, Any]:
    s = get_session(sim_id)
    if body.mode not in ("drill", "synthetic", "live"):
        raise err(400, "BAD_MODE", "mode must be drill|synthetic|live")
    if s.engine.finished:
        raise err(409, "FINISHED", "simulation has finished; start a new one")
    if s.feed.running:
        s.feed.running = False
        if s.feed_task:
            s.feed_task.cancel()
    if body.mode == "drill":
        try:
            drill_plan(s.engine.world.map_id)
        except FeedError as exc:
            raise err(400, "NO_DRILL", str(exc))
    s.feed.mode, s.feed.params, s.feed.running = body.mode, dict(body.params), True
    if body.speed:
        s.speed = max(0.5, min(16.0, float(body.speed)))
    if s.status != "running":
        s.status = "running"
        s.task = asyncio.create_task(s.run_loop())
    s.feed_task = asyncio.create_task(run_feed(s, body.mode, dict(body.params)))
    await s.broadcast("feed")
    return {**s.summary(), "feed_status": s.feed.status(s.engine)}


@app.post("/api/simulations/{sim_id}/feed/stop")
async def feed_stop(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    s.feed.running = False
    if s.feed_task:
        s.feed_task.cancel()
    s.feed.status_note = "feed stopped"
    await s.broadcast("feed")
    return {"feed_status": s.feed.status(s.engine)}


@app.get("/api/simulations/{sim_id}/priority")
def priority(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    return {"tick": s.engine.world.tick, "board": priority_board(s.engine)}


@app.post("/api/simulations/{sim_id}/events")
async def inject_event(sim_id: str, body: EventIn) -> dict[str, Any]:
    s = get_session(sim_id)
    if body.type not in EVENT_TYPES:
        raise err(400, "BAD_EVENT", f"event type must be one of {list(EVENT_TYPES)}")
    try:
        ev = s.engine.inject(body.type, body.params, "user")
    except EventError as exc:
        raise err(422, "EVENT_REJECTED", str(exc))
    s.persist()
    await s.broadcast("event")
    return {"event": {"id": ev.id, "tick": ev.tick, "type": ev.type, "params": ev.params, "effects": ev.effects, "source": ev.source}, "state_version": s.engine.world.version, "frame": s.engine.last_frame()}


@app.get("/api/simulations/{sim_id}/events")
def list_events(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    return {"events": [{"id": e.id, "tick": e.tick, "type": e.type, "params": e.params, "effects": e.effects, "source": e.source} for e in s.engine.events], "injected": s.engine.injected}


@app.get("/api/simulations/{sim_id}/agents")
def agents(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    e = s.engine
    w = e.world
    out_res = []
    for r in sorted(w.resources.values(), key=lambda r: r.id):
        closed = believed_closed(w, r, e.cfg.comm_delay) if e.is_decentralized() else None
        truth_closed = {x.id for x in w.edges.values() if not (x.ground_ok if r.kind == "ground" else x.boat_ok)}
        stale = sorted((truth_closed ^ set(closed))) if closed is not None else []
        relevant_stale = [x for x in stale if w.edges[x].kind in (("road", "bridge") if r.kind == "ground" else ("road", "river", "jetty"))]
        out_res.append(
            {
                "id": r.id,
                "name": r.name,
                "kind": r.kind,
                "status": r.status,
                "capacity": r.capacity,
                "load": r.load,
                "information": "own state exact; incident + network broadcasts delayed by %d tick(s); closures discovered physically" % e.cfg.comm_delay if e.is_decentralized() else "centralised: coordinator holds complete, instant state",
                "belief_mismatch_edges": relevant_stale[:20],
                "discovered_closures": sorted(r.known_closed),
                "blackout_until": r.blackout_until if r.blackout_until >= w.tick else None,
                "missions": [a.id for a in w.assignments.values() if a.resource_id == r.id],
                "bids_submitted": sum(1 for rd in e.rounds for m in rd.messages if m.type == "BID" and m.frm == r.id),
                "commits": sum(1 for a in w.assignments.values() if a.resource_id == r.id),
            }
        )
    shelters = [
        {
            "id": sh.id,
            "name": sh.name,
            "capacity": sh.capacity,
            "occupancy": sh.occupancy,
            "reserved": sh.reserved(w),
            "free": sh.effective_free(w),
            "reservations": [{"id": x.id, "resource": x.resource_id, "incident": x.incident_id, "people": x.people, "status": x.status, "expires": x.expires_tick, "created": x.created_tick} for x in w.reservations.values() if x.shelter_id == sh.id][-40:],
            "accepts": sum(1 for rd in e.rounds for m in rd.messages if m.type == "ACCEPT_RESERVATION" and m.frm == sh.id),
            "rejects": sum(1 for rd in e.rounds for m in rd.messages if m.type == "REJECT_RESERVATION" and m.frm == sh.id),
        }
        for sh in sorted(w.shelters.values(), key=lambda x: x.id)
    ]
    incidents = [
        {"id": i.id, "status": i.status, "confidence": i.confidence, "publications": i.publications[-4:], "reports": len(i.report_ids)}
        for i in sorted(w.incidents.values(), key=lambda i: i.id)
    ]
    route_agent = {
        "closed_for_ground": sorted(x.id for x in w.edges.values() if not x.ground_ok and x.kind in ("road", "bridge")),
        "navigable_flooded_roads": sorted(x.id for x in w.edges.values() if x.boat_ok and x.kind == "road"),
        "manual_closures": [{"id": x.id, "name": x.name, "reason": x.closed_reason} for x in w.edges.values() if x.manual_closed],
        "bridges": [{"id": x.id, "name": x.name, "ground_ok": x.ground_ok, "clearance": x.clearance, "risk": x.risk} for x in w.edges.values() if x.kind == "bridge"],
    }
    return {"strategy": e.strategy, "resources": out_res, "shelters": shelters, "incidents": incidents, "route_agent": route_agent}


@app.get("/api/simulations/{sim_id}/assignments")
def assignments(sim_id: str) -> dict[str, Any]:
    return {"assignments": get_session(sim_id).engine.last_frame()["assignments"]}


@app.get("/api/simulations/{sim_id}/negotiations")
def negotiations(sim_id: str, limit: int = Query(60, ge=1, le=500), incident: Optional[str] = None) -> dict[str, Any]:
    e = get_session(sim_id).engine
    rounds = [r for r in e.rounds if not incident or incident in r.incidents]
    return {"total": len(rounds), "rounds": [r.to_dict(with_messages=False) for r in rounds[-limit:]][::-1]}


@app.get("/api/simulations/{sim_id}/negotiations/{round_id}")
def negotiation(sim_id: str, round_id: str) -> dict[str, Any]:
    e = get_session(sim_id).engine
    r = next((r for r in e.rounds if r.id == round_id), None)
    if not r:
        raise err(404, "NOT_FOUND", f"round {round_id} not found")
    decs = [d for d in e.decisions.values() if d["round_id"] == round_id]
    return {**r.to_dict(), "decisions": decs}


@app.get("/api/simulations/{sim_id}/decisions")
def decisions(sim_id: str, incident: Optional[str] = None, resource: Optional[str] = None, outcome: Optional[str] = None, limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    e = get_session(sim_id).engine
    ds = list(e.decisions.values())
    if incident:
        ds = [d for d in ds if d["incident_id"] == incident]
    if resource:
        ds = [d for d in ds if (d.get("selected") or {}).get("resource_id") == resource or any(c["resource_id"] == resource for c in d["candidates"])]
    if outcome:
        ds = [d for d in ds if d["outcome"] == outcome]
    return {"total": len(ds), "decisions": ds[-limit:][::-1]}


@app.get("/api/simulations/{sim_id}/decisions/{decision_id}")
def decision(sim_id: str, decision_id: str) -> dict[str, Any]:
    e = get_session(sim_id).engine
    d = e.decisions.get(decision_id)
    if not d:
        raise err(404, "NOT_FOUND", f"decision {decision_id} not found")
    a = e.world.assignments.get(d.get("assignment_id") or "")
    return {**d, "assignment": {"id": a.id, "status": a.status, "cancel_reason": a.cancel_reason, "reroutes": a.reroutes} if a else None}


@app.get("/api/simulations/{sim_id}/metrics")
def metrics(sim_id: str) -> dict[str, Any]:
    e = get_session(sim_id).engine
    return {"metrics": compute_metrics(e), "series": e.metric_series, "disruptions": e.disruptions}


@app.get("/api/simulations/{sim_id}/audit")
def audit(sim_id: str, category: Optional[str] = None, since: int = 0, limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
    e = get_session(sim_id).engine
    rows = [a for a in e.audit_log if a.tick >= since and (not category or a.category == category)]
    return {"total": len(rows), "audit": [_audit_dict(a) for a in rows[-limit:]]}


@app.get("/api/simulations/{sim_id}/safety")
def safety(sim_id: str) -> dict[str, Any]:
    e = get_session(sim_id).engine
    m = compute_metrics(e)
    return {
        "invariants": ["S1 no double booking", "S2 shelter capacity", "S3 prohibited edges", "S4 transport capacity", "S5 stale versions", "S6 reservation validity", "S7 operational & connected"],
        "route_violations": m["route_violations"],
        "invariant_violations": m["invariant_violations"],
        "commit_rejections": m["safety_rejections"],
        "movement_blocks": m["blocked_moves"],
        "log": e.safety_log[-300:][::-1],
    }


@app.get("/api/simulations/{sim_id}/frames")
def frames(sim_id: str, start: int = 0, end: Optional[int] = None, stride: int = Query(1, ge=1, le=20)) -> dict[str, Any]:
    e = get_session(sim_id).engine
    end = e.world.tick if end is None else end
    ticks = [t for t in sorted(e.frames) if start <= t <= end][::stride]
    return {"ticks": ticks, "frames": [e.frames[t] for t in ticks], "hashes": {t: e.hashes.get(t) for t in ticks}}


@app.get("/api/simulations/{sim_id}/replay/verify")
async def replay_verify(sim_id: str) -> dict[str, Any]:
    s = get_session(sim_id)
    snapshot = s.engine.fork()
    return await asyncio.get_running_loop().run_in_executor(None, verify_replay, snapshot)


@app.post("/api/simulations/{sim_id}/fork")
async def fork(sim_id: str, body: ForkIn) -> dict[str, Any]:
    s = get_session(sim_id)
    e = s.engine
    tick = e.world.tick if body.tick is None else body.tick
    if not (0 <= tick <= e.world.tick):
        raise err(422, "BAD_TICK", f"fork tick must be within 0..{e.world.tick}")
    cfg, injected = copy.deepcopy(e.base_cfg), copy.deepcopy(e.injected)
    try:
        return await asyncio.get_running_loop().run_in_executor(None, counterfactual, cfg, e.strategy, injected, tick, body.modifications, body.horizon)
    except EventError as exc:
        raise err(422, "BAD_MODIFICATION", str(exc))


# ================================================================ jobs (benchmarks + adversarial)
def _start_job(kind: str, request: dict[str, Any], fn: Any) -> dict[str, Any]:
    job_id = f"{kind[:3]}-{uuid.uuid4().hex[:8]}"
    job = {"id": job_id, "kind": kind, "status": "running", "progress": 0.0, "request": request, "result": None, "error": None, "created": time.time()}
    JOBS[job_id] = job

    def progress(p: float) -> None:
        job["progress"] = round(p, 3)

    async def runner() -> None:
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, fn, progress)
            job["result"], job["status"], job["progress"] = res, "done", 1.0
            store.save_job(job_id, kind, request, res)
        except Exception as exc:  # surface failures honestly
            job["status"], job["error"] = "error", str(exc)

    asyncio.get_running_loop().create_task(runner())
    return job


class ParetoIn(BaseModel):
    scenario: str = "kerala_2018"
    map: Optional[str] = None
    seeds: list[int] = Field(default_factory=lambda: [1, 2, 3])
    strategy: str = "decentralized"


@app.post("/api/pareto")
async def create_pareto(body: ParetoIn) -> dict[str, Any]:
    from ..analysis.runs import pareto as pareto_fn

    if body.strategy not in STRATEGIES or not (1 <= len(body.seeds) <= 6):
        raise err(400, "BAD_REQUEST", "strategy must be valid and seeds 1..6")
    cfg = make_config(body.scenario, None, {"map": body.map} if body.map else {})
    return _start_job("pareto", body.model_dump(), lambda prog: pareto_fn(cfg, list(body.seeds), body.strategy, prog))


@app.get("/api/pareto/{job_id}")
def get_pareto(job_id: str) -> dict[str, Any]:
    return get_benchmark(job_id)


@app.get("/api/evidence")
def evidence(map: str = "kerala_aluva") -> dict[str, Any]:
    """Observed Sentinel-1 water for 2018, the model's flood at that pass, and the dataset catalogue."""
    import json as _json
    from pathlib import Path as _Path

    from ..live.catalog import DATASETS, FEEDBACK
    from ..simulation.flood import update_flood
    from ..simulation.serialize import frame as frame_fn

    if not is_known(map):
        raise err(400, "BAD_MAP", f"unknown map '{map}'")
    out: dict[str, Any] = {"datasets": DATASETS, "feedback": FEEDBACK, "s1": None, "model_frame": None}
    path = _Path(__file__).resolve().parent.parent / "data" / "events" / f"s1_2018__{map}.json"
    if path.exists():
        s1 = _json.loads(path.read_text(encoding="utf-8"))
        out["s1"] = s1
        eng = Engine(get_preset("kerala_2018", map=map), record_frames=False)
        eng.world.river_override = float(s1["validation"].get("ramp_c") or 0.0)
        update_flood(eng.world, eng.cfg.flood_severity, eng.cfg.flood_peak_tick)
        f = frame_fn(eng)
        f["incidents"], f["resources"] = [], []
        out["model_frame"] = f
    return out


@app.post("/api/benchmarks")
async def create_benchmark(body: BenchmarkIn) -> dict[str, Any]:
    if not body.strategies or any(st not in STRATEGIES for st in body.strategies):
        raise err(400, "BAD_STRATEGY", f"strategies must be from {list(STRATEGIES)}")
    if not (1 <= len(body.seeds) <= 30):
        raise err(422, "BAD_SEEDS", "1..30 seeds")
    cfg = make_config(body.scenario, None, body.overrides)
    return _start_job("benchmark", body.model_dump(), lambda p: benchmark(cfg, body.strategies, body.seeds, p))


@app.get("/api/benchmarks/{job_id}")
def get_benchmark(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job:
        return job
    rec = store.load_job(job_id)
    if not rec:
        raise err(404, "NOT_FOUND", f"job {job_id} not found")
    return {**rec, "status": "done", "progress": 1.0}


@app.post("/api/adversarial")
async def create_adversarial(body: AdversarialIn) -> dict[str, Any]:
    if body.strategy not in STRATEGIES:
        raise err(400, "BAD_STRATEGY", "unknown strategy")
    if not (1 <= body.n_failures <= 5 and 1 <= body.trials <= 60):
        raise err(422, "BAD_REQUEST", "n_failures 1..5, trials 1..60")
    ov: dict[str, Any] = {"duration": body.duration} if body.duration else {}
    if body.map:
        ov["map"] = body.map
    cfg = make_config(body.scenario, None, ov)
    return _start_job("adversarial", body.model_dump(), lambda p: adversarial(cfg, body.strategy, body.n_failures, body.categories, body.trials, body.seed, p))


@app.get("/api/adversarial/{job_id}")
def get_adversarial(job_id: str) -> dict[str, Any]:
    return get_benchmark(job_id)


# ================================================================ reports
@app.get("/api/reports/{sim_id}")
async def report(sim_id: str, format: str = Query("json", pattern="^(json|pdf)$"), compare: bool = True) -> Any:
    s = get_session(sim_id)
    snapshot = s.engine.fork()
    rep = await asyncio.get_running_loop().run_in_executor(None, lambda: build_report(snapshot, sim_id, compare))
    if format == "pdf":
        pdf = await asyncio.get_running_loop().run_in_executor(None, report_pdf, rep)
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="resqnet-report-{sim_id}.pdf"'})
    return rep


# ================================================================ websocket
@app.websocket("/ws/simulations/{sim_id}")
async def ws_sim(ws: WebSocket, sim_id: str) -> None:
    await ws.accept()
    try:
        s = get_session(sim_id)
    except HTTPException:
        await ws.send_text(json.dumps({"type": "error", "error": {"code": "NOT_FOUND", "message": f"simulation {sim_id} not found"}}))
        await ws.close()
        return
    s.clients.add(ws)
    await ws.send_text(json.dumps(s.full_message("hello"), default=str))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "resync":
                await ws.send_text(json.dumps(s.full_message("resync"), default=str))
            elif msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong", "state_version": s.engine.world.version}))
    except WebSocketDisconnect:
        pass
    finally:
        s.clients.discard(ws)
