"""Replay, counterfactual branching, seeded benchmarks and adversarial search.

Every result here comes from actually running the simulation engine; nothing is
estimated or hard-coded.
"""
from __future__ import annotations

import copy
import random
import statistics
import time
from typing import Any, Callable, Optional

from ..metrics.metrics import SUMMARY_KEYS, compute_metrics
from ..simulation.engine import Engine, EventError
from ..simulation.scenarios import POLICIES, ScenarioConfig


# ---------------------------------------------------------------- replay

def rebuild(cfg: ScenarioConfig, strategy: str, injected: list[dict[str, Any]], until: int, record_frames: bool = False) -> Engine:
    """Re-run from scratch, re-applying the logged user events at the same (tick, seq)."""
    eng = Engine(copy.deepcopy(cfg), strategy, record_frames=record_frames)
    by_tick: dict[int, list[dict]] = {}
    for ev in sorted(injected, key=lambda e: (e["tick"], e["seq"])):
        by_tick.setdefault(ev["tick"], []).append(ev)

    def apply(t: int) -> None:
        for ev in by_tick.get(t, []):
            try:
                eng.inject(ev["type"], dict(ev["params"]), ev.get("source", "user"))
            except EventError:
                pass

    apply(0)
    while not eng.finished and eng.world.tick < until:
        eng.step()
        apply(eng.world.tick)
    return eng


def verify_replay(engine: Engine) -> dict[str, Any]:
    t0 = time.perf_counter()
    rep = rebuild(engine.base_cfg, engine.strategy, engine.injected, engine.world.tick)
    mismatches = [t for t, h in engine.hashes.items() if rep.hashes.get(t) != h]
    return {
        "ticks_compared": len(engine.hashes),
        "mismatches": mismatches[:20],
        "identical": not mismatches,
        "final_hash": engine.hashes.get(engine.world.tick),
        "replay_final_hash": rep.hashes.get(rep.world.tick),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# ---------------------------------------------------------------- counterfactual

def compact_frame(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "tick": f["tick"],
        "resources": [{k: r[k] for k in ("id", "kind", "x", "y", "status", "load", "incident_id", "route")} for r in f["resources"]],
        "incidents": [{k: i[k] for k in ("id", "x", "y", "status", "urgency", "people", "unassigned")} for i in f["incidents"]],
        "shelters": [{k: s[k] for k in ("id", "x", "y", "capacity", "occupancy", "reserved")} for s in f["shelters"]],
        "edges": {k: v[:3] for k, v in f["edges"].items()},
        "depths": f["depths"],
        "metrics": {k: f["metrics"][k] for k in ("people_rescued", "unmet_people", "deadline_misses", "mean_time_to_service")},
    }


def apply_modifications(eng: Engine, mods: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    for _ in range(int(mods.get("extra_boats") or 0)):
        notes += eng._apply_event("ADD_RESOURCE", {"kind": "boat", "node": mods.get("boat_node") or eng.world.meta.get("default_boat_node")}, "counterfactual").effects
    for _ in range(int(mods.get("extra_ground") or 0)):
        notes += eng._apply_event("ADD_RESOURCE", {"kind": "ground", "node": mods.get("ground_node") or eng.world.meta.get("default_ground_node")}, "counterfactual").effects
    for eid, state in (mods.get("edges") or {}).items():
        notes += eng._apply_event("OPEN_EDGE" if state == "open" else "CLOSE_EDGE", {"edge_id": eid, "reason": "counterfactual"}, "counterfactual").effects
    for sid, delta in (mods.get("shelter_delta") or {}).items():
        sh = eng.world.shelters.get(sid)
        if sh and int(delta):
            notes += eng._apply_event("SHELTER_CAPACITY", {"shelter_id": sid, "capacity": max(0, sh.capacity + int(delta)), "reason": "counterfactual"}, "counterfactual").effects
    if mods.get("policy") in POLICIES:
        eng.cfg.policy = mods["policy"]
        eng.cfg.weights = dict(POLICIES[mods["policy"]])
        notes.append(f"priority policy → {mods['policy']}")
    if mods.get("comm_delay") is not None and mods.get("comm_delay") != "":
        eng.cfg.comm_delay = int(mods["comm_delay"])
        notes.append(f"communication delay → {eng.cfg.comm_delay} ticks")
    if mods.get("strategy") in ("decentralized", "centralized", "greedy"):
        eng.strategy = mods["strategy"]
        eng.world.strategy = mods["strategy"]
        notes.append(f"strategy → {mods['strategy']}")
    for rid in mods.get("disable") or []:
        try:
            notes += eng._apply_event("RESOURCE_FAILURE", {"resource_id": rid, "reason": "counterfactual removal"}, "counterfactual").effects
        except EventError:
            pass
    return notes


def _assignment_seq(eng: Engine) -> dict[str, list[tuple[int, str]]]:
    seq: dict[str, list[tuple[int, str]]] = {}
    for a in sorted(eng.world.assignments.values(), key=lambda a: (a.created_tick, a.id)):
        seq.setdefault(a.incident_id, []).append((a.created_tick, a.resource_id))
    return seq


def counterfactual(cfg: ScenarioConfig, strategy: str, injected: list[dict[str, Any]], fork_tick: int, mods: dict[str, Any], horizon: Optional[int] = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    base = rebuild(cfg, strategy, [e for e in injected if e["tick"] <= fork_tick], fork_tick)
    branch = base.fork()
    notes = apply_modifications(branch, mods)
    # both branches get the same replanning round at the fork point, so the only
    # difference between them is the modification itself
    for eng in (base, branch):
        eng._microstep("counterfactual fork point")
        eng._snapshot()
    end = min(cfg.duration, horizon or cfg.duration)
    future = sorted([e for e in injected if e["tick"] > fork_tick], key=lambda e: (e["tick"], e["seq"]))
    frames: dict[str, list] = {"baseline": [], "alternative": []}
    for eng, key in ((base, "baseline"), (branch, "alternative")):
        eng.record_frames = False
        frames[key].append(compact_frame(eng.last_frame()))
        while not eng.finished and eng.world.tick < end:
            eng.step()
            for ev in [e for e in future if e["tick"] == eng.world.tick]:
                try:
                    eng.inject(ev["type"], dict(ev["params"]), ev.get("source", "user"))
                except EventError:
                    pass
            if eng.world.tick % 2 == 0 or eng.finished:
                frames[key].append(compact_frame(eng.last_frame()))
    mb, ma = compute_metrics(base), compute_metrics(branch)
    sb, sa = _assignment_seq(base), _assignment_seq(branch)
    diverged = []
    for iid in sorted(set(sb) | set(sa)):
        if sb.get(iid) != sa.get(iid):
            diverged.append({"incident": iid, "baseline": sb.get(iid, []), "alternative": sa.get(iid, [])})
    diverged.sort(key=lambda d: (min([t for t, _ in d["baseline"] + d["alternative"]] or [10**6]), d["incident"]))
    keys = SUMMARY_KEYS + ["fairness_by_neighbourhood", "served_incidents"]
    return {
        "fork_tick": fork_tick,
        "end_tick": base.world.tick,
        "modifications": mods,
        "applied": notes,
        "baseline": {k: mb[k] for k in keys},
        "alternative": {k: ma[k] for k in keys},
        "diverged_decisions": diverged[:25],
        "diverged_count": len(diverged),
        "frames": frames,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# ---------------------------------------------------------------- benchmarks

def run_one(cfg: ScenarioConfig, strategy: str) -> dict[str, Any]:
    eng = Engine(copy.deepcopy(cfg), strategy, record_frames=False)
    t0 = time.perf_counter()
    while not eng.finished:
        eng.step()
    m = compute_metrics(eng)
    out: dict[str, Any] = {k: m[k] for k in SUMMARY_KEYS}
    out["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["fairness_by_neighbourhood"] = m["fairness_by_neighbourhood"]
    return out


def aggregate(values: list[Optional[float]]) -> dict[str, Optional[float]]:
    xs = [v for v in values if v is not None]
    if not xs:
        return {"mean": None, "std": None, "min": None, "max": None, "n": 0}
    return {
        "mean": round(statistics.fmean(xs), 3),
        "std": round(statistics.stdev(xs), 3) if len(xs) > 1 else 0.0,
        "min": round(min(xs), 3),
        "max": round(max(xs), 3),
        "n": len(xs),
    }


def benchmark(cfg: ScenarioConfig, strategies: list[str], seeds: list[int], progress: Optional[Callable[[float], None]] = None) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    total = len(strategies) * len(seeds)
    done = 0
    for seed in seeds:
        for strat in strategies:
            c = copy.deepcopy(cfg)
            c.seed = seed
            r = run_one(c, strat)
            r.update({"seed": seed, "strategy": strat})
            runs.append(r)
            done += 1
            if progress:
                progress(done / total)
    summary = {s: {k: aggregate([r[k] for r in runs if r["strategy"] == s]) for k in SUMMARY_KEYS + ["wall_ms"]} for s in strategies}
    paired: dict[str, Any] = {}
    if "decentralized" in strategies and "centralized" in strategies:
        for k in ("mean_time_to_service", "deadline_misses", "unmet_people", "fairness_jain", "idle_with_demand_ticks", "negotiation_messages"):
            diffs = []
            for seed in seeds:
                a = next(r for r in runs if r["seed"] == seed and r["strategy"] == "decentralized")[k]
                b = next(r for r in runs if r["seed"] == seed and r["strategy"] == "centralized")[k]
                if a is not None and b is not None:
                    diffs.append(a - b)
            paired[k] = aggregate(diffs)
    return {"scenario": cfg.key, "seeds": seeds, "strategies": strategies, "runs": runs, "summary": summary, "paired_decentralized_minus_centralized": paired}


# ---------------------------------------------------------------- adversarial

FAILURE_POOL: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "bridge": [("CLOSE_EDGE", {"edge_id": b, "reason": "adversarial: bridge failure"}) for b in ("BR1", "BR2", "BR3")],
    "boat": [("RESOURCE_FAILURE", {"resource_id": f"B{i}", "reason": "adversarial: engine failure"}) for i in range(1, 6)],
    "ground": [("RESOURCE_FAILURE", {"resource_id": f"G{i}", "reason": "adversarial: vehicle failure"}) for i in range(1, 5)],
    "shelter": [("SHELTER_CAPACITY", {"shelter_id": s, "capacity": "occupancy", "reason": "adversarial: overflow"}) for s in ("S1", "S2", "S3")],
    "surge": [("FLOOD_SURGE", {"delta": 0.6})],
    "blackout": [("COMM_BLACKOUT", {"resource_id": f"B{i}", "duration": 25}) for i in range(1, 6)],
}


def failure_pool(cfg: ScenarioConfig) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """FAILURE_POOL with the bridges and shelters of the configured map (BR1..BRn, S1..Sn)."""
    from ..simulation.world import build_world

    w = build_world(cfg, "decentralized")
    pool = dict(FAILURE_POOL)
    bridges = sorted(e.id for e in w.edges.values() if e.kind == "bridge")
    pool["bridge"] = [("CLOSE_EDGE", {"edge_id": b, "reason": "adversarial: bridge failure"}) for b in bridges]
    pool["shelter"] = [("SHELTER_CAPACITY", {"shelter_id": s, "capacity": "occupancy", "reason": "adversarial: overflow"}) for s in sorted(w.shelters)]
    return pool


def adversarial(cfg: ScenarioConfig, strategy: str, n_failures: int, categories: list[str], trials: int, seed: int, progress: Optional[Callable[[float], None]] = None) -> dict[str, Any]:
    rng = random.Random(seed)
    fpool = failure_pool(cfg)
    pool = [(cat, t, p) for cat in categories for t, p in fpool.get(cat, [])]
    if not pool:
        raise ValueError("no failure categories selected")
    baseline = run_one(cfg, strategy)
    results = []
    seen: set = set()
    for k in range(trials):
        picks = rng.sample(pool, min(n_failures, len(pool)))
        sched = sorted(
            ({"tick": rng.randint(10, max(11, min(70, cfg.duration - 10))), "type": t, "params": dict(p), "category": cat} for cat, t, p in picks),
            key=lambda e: (e["tick"], e["type"]),
        )
        key = tuple((e["tick"], e["type"], tuple(sorted(e["params"].items()))) for e in sched)
        if key in seen:
            continue
        seen.add(key)
        c = copy.deepcopy(cfg)
        c.schedule = list(cfg.schedule) + [{"tick": e["tick"], "type": e["type"], "params": e["params"]} for e in sched]
        m = run_one(c, strategy)
        score = (m["unmet_people"] or 0) * 3 + (m["deadline_misses"] or 0) * 5 + (m["mean_time_to_service"] or 0) * 0.2
        results.append({"trial": k + 1, "failures": sched, "metrics": m, "score": round(score, 2)})
        if progress:
            progress((k + 1) / trials)
    results.sort(key=lambda r: (-r["score"], r["trial"]))
    return {
        "scenario": cfg.key,
        "strategy": strategy,
        "trials_run": len(results),
        "pool_size": len(pool),
        "search_space_note": f"Seeded random sample of {len(results)} combinations ({n_failures} failures each) drawn from a pool of {len(pool)} single failures, injected at ticks 10–70. Bounded search — not exhaustive verification.",
        "score_formula": "3 × unmet people + 5 × urgent deadline misses + 0.2 × mean time-to-service",
        "baseline": baseline,
        "worst": results[:6],
        "all_scores": [{"trial": r["trial"], "score": r["score"], "unmet": r["metrics"]["unmet_people"], "misses": r["metrics"]["deadline_misses"]} for r in results],
    }


# ---------------------------------------------------------------- Pareto front over policies

def policy_grid() -> list[dict[str, Any]]:
    """The named policies plus a grid over the cost weights (T time, R risk, U urgency, F fairness)."""
    out = [{"name": k, "weights": dict(v)} for k, v in POLICIES.items()]
    seen = {tuple(sorted(x["weights"].items())) for x in out}
    for T in (0.2, 0.35, 0.5, 0.65):
        for F in (0.0, 0.2, 0.4):
            R = 0.2
            U = round(1.0 - T - F - R, 2)
            if U < 0.05:
                continue
            w = {"T": T, "R": R, "U": U, "F": F}
            key = tuple(sorted(w.items()))
            if key not in seen:
                seen.add(key)
                out.append({"name": f"T{T:g} U{U:g} F{F:g}", "weights": w})
    return out


def pareto(cfg: ScenarioConfig, seeds: list[int], strategy: str = "decentralized", progress: Optional[Callable[[float], None]] = None) -> dict[str, Any]:
    """Run every policy over the same seeds and mark the non-dominated ones on
    (mean time to service, lower), (fairness, higher), (people not delivered, lower)."""
    grid = policy_grid()
    total = len(grid) * len(seeds)
    done = 0
    points = []
    for pol in grid:
        ms = []
        for seed in seeds:
            c = copy.deepcopy(cfg)
            c.seed = seed
            c.weights = dict(pol["weights"])
            c.policy = pol["name"] if pol["name"] in POLICIES else "custom"
            r = run_one(c, strategy)
            ms.append(r)
            done += 1
            if progress:
                progress(done / total)
        agg = lambda k: round(statistics.fmean([m[k] for m in ms if m[k] is not None]), 3) if any(m[k] is not None for m in ms) else None  # noqa: E731
        points.append({**pol, "mean_time_to_service": agg("mean_time_to_service"), "fairness_jain": agg("fairness_jain"), "unmet_people": agg("unmet_people"), "deadline_misses": agg("deadline_misses")})

    def dominates(a: dict, b: dict) -> bool:
        ka = (a["mean_time_to_service"], -a["fairness_jain"], a["unmet_people"])
        kb = (b["mean_time_to_service"], -b["fairness_jain"], b["unmet_people"])
        return all(x <= y for x, y in zip(ka, kb)) and any(x < y for x, y in zip(ka, kb))

    valid = [pt for pt in points if None not in (pt["mean_time_to_service"], pt["fairness_jain"], pt["unmet_people"])]
    for pt in points:
        pt["pareto"] = pt in valid and not any(dominates(o, pt) for o in valid if o is not pt)
    return {"scenario": cfg.key, "map": cfg.map, "seeds": seeds, "strategy": strategy, "points": points, "objectives": ["mean_time_to_service (min, lower is better)", "fairness_jain (higher is better)", "unmet_people (lower is better)"]}
