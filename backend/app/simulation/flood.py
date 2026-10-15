"""Simplified, explicitly synthetic flood-propagation model.

NOT hydraulic forecasting. A per-neighbourhood water level rises along a ramp
towards a peak; node depth = level - synthetic elevation. Edge traversability is
derived from depth thresholds:

* ground units: road/bridge edge passable while mean depth < GROUND_MAX_DEPTH
* boats: river/jetty always (unless manually closed); flooded roads once depth >= BOAT_MIN_DEPTH
* bridges: deck unsafe for ground units once river level >= bridge clearance

Kept separate from the coordination logic: agents only see the resulting graph.
"""
from __future__ import annotations

from .state import EdgeKind, WorldState

GROUND_MAX_DEPTH = 0.35
BOAT_MIN_DEPTH = 0.5
BASE_LEVEL = 1.2


def ramp(tick: int, peak_tick: int) -> float:
    return min(1.0, 0.35 + 0.65 * tick / max(1, peak_tick))


def current_ramp(world: WorldState, peak_tick: int) -> float:
    """0.35..1.0: the replayed discharge curve when the run carries a real event, else the
    synthetic linear rise."""
    if world.river_override is not None:
        return 0.35 + 0.65 * world.river_override
    c = world.flood_curve
    if c:
        return 0.35 + 0.65 * c[min(max(world.tick, 0), len(c) - 1)]
    return ramp(world.tick, peak_tick)


def water_level(world: WorldState, nbhd: str, severity: float, peak_tick: int) -> float:
    f = world.neighbourhoods[nbhd].flood_factor if nbhd in world.neighbourhoods else 1.0
    return BASE_LEVEL + severity * 2.2 * current_ramp(world, peak_tick) * f + world.flood_offset


def river_level(world: WorldState, severity: float, peak_tick: int) -> float:
    return 1.0 + severity * 2.0 * current_ramp(world, peak_tick) + world.flood_offset


def update_flood(world: WorldState, severity: float, peak_tick: int) -> list[tuple[str, str, bool, bool]]:
    """Recompute depths and traversability. Returns edges whose availability changed:
    (edge_id, cause, ground_ok_before, boat_ok_before)."""
    levels = {nb: water_level(world, nb, severity, peak_tick) for nb in world.neighbourhoods}
    rl = river_level(world, severity, peak_tick)
    for n in world.nodes.values():
        n.depth = 0.0 if n.kind == "river" else round(max(0.0, levels[n.neighbourhood] - n.elevation), 3)
    changed = []
    for e in world.edges.values():
        before = (e.ground_ok, e.boat_ok)
        nu, nv = world.nodes[e.u], world.nodes[e.v]
        if e.kind in (EdgeKind.ROAD, EdgeKind.BRIDGE):
            e.depth = round((nu.depth + nv.depth) / 2, 3)
            ground = e.depth < GROUND_MAX_DEPTH
            cause = "flood depth"
            if e.kind == EdgeKind.BRIDGE and rl >= e.clearance:
                ground = False
                cause = "river above bridge clearance"
            boat = e.kind == EdgeKind.ROAD and e.depth >= BOAT_MIN_DEPTH
            e.risk = round(min(1.0, (e.depth / GROUND_MAX_DEPTH) * 0.7 + (0.25 + 0.5 * rl / e.clearance if e.kind == EdgeKind.BRIDGE else 0.0)), 3) if ground else round(min(1.0, 0.35 + 0.4 * max(0.0, 1.0 - e.depth)), 3)
        else:
            e.depth = 0.0
            ground, boat = False, True
            cause = "channel"
            if e.gated:  # flood access: open only while the land end is under boat-navigable water
                e.depth = round(max(nu.depth, nv.depth), 3)
                boat = e.depth >= BOAT_MIN_DEPTH
                cause = "flood access"
            e.risk = round(min(1.0, 0.1 + 0.25 * current_ramp(world, peak_tick) * severity + world.flood_offset * 0.2), 3)
        if e.manual_closed:
            ground, boat = False, False
            cause = e.closed_reason or "closed"
        e.ground_ok, e.boat_ok = ground, boat
        if before != (ground, boat):
            e.version += 1
            changed.append((e.id, cause, before[0], before[1]))
    return changed


def snapshot_traversability(world: WorldState) -> None:
    world.trav_history[world.tick] = {
        "ground": frozenset(e.id for e in world.edges.values() if not e.ground_ok),
        "boat": frozenset(e.id for e in world.edges.values() if not e.boat_ok),
    }
    for t in [t for t in world.trav_history if t < world.tick - 60]:
        del world.trav_history[t]
