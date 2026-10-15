"""Scenario configuration and the reproducible demo presets."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

POLICIES: dict[str, dict[str, float]] = {
    # C_ij = wT*T + wR*R + wU*U + wF*F  (proposed starting weights, not trained values)
    "balanced": {"T": 0.35, "R": 0.25, "U": 0.20, "F": 0.20},
    "fastest": {"T": 0.60, "R": 0.20, "U": 0.20, "F": 0.00},
    "urgency_first": {"T": 0.25, "R": 0.15, "U": 0.50, "F": 0.10},
    "equity": {"T": 0.25, "R": 0.15, "U": 0.20, "F": 0.40},
}

STRATEGIES = ("decentralized", "centralized", "greedy")


@dataclass
class ScenarioConfig:
    key: str = "normal"
    name: str = "Normal Flood"
    description: str = ""
    map: str = "riverbend"  # map source id (see simulation/maps.py)
    flood_event: str = ""  # real flood event id replayed as the river level (see simulation/events.py)
    extra_shelters: bool = True  # maps with more than three shelters open S4+ at their indicative capacity
    river_source: str = "model"  # "model" (scenario ramp / replayed event) or "feed" (live gauge readings)
    approval_mode: bool = False  # True: the agents only recommend dispatches; an operator approves each one
    seed: int = 7
    duration: int = 180  # ticks; 1 tick = 1 simulated minute
    flood_severity: float = 1.0
    flood_peak_tick: int = 90
    initial_incidents: int = 12
    arrival_incidents: int = 14
    arrival_window: int = 100
    boats: int = 5
    ground_units: int = 4
    shelter_capacity: list[int] = field(default_factory=lambda: [45, 60, 40])
    shelter_occupancy: list[int] = field(default_factory=lambda: [6, 10, 4])
    report_noise: float = 0.15
    duplicate_rate: float = 0.15
    delayed_report_rate: float = 0.10
    false_alarm_rate: float = 0.0
    contradiction_rate: float = 0.0
    comm_delay: int = 1  # ticks before a decentralised agent hears a broadcast
    policy: str = "balanced"
    weights: dict[str, float] = field(default_factory=lambda: dict(POLICIES["balanced"]))
    neighbourhood_priority: dict[str, float] = field(default_factory=dict)
    schedule: list[dict[str, Any]] = field(default_factory=list)  # [{tick,type,params}]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScenarioConfig":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        if "weights" not in d or not d.get("weights"):
            cfg.weights = dict(POLICIES.get(cfg.policy, POLICIES["balanced"]))
        return cfg


def _preset(**kw: Any) -> ScenarioConfig:
    return ScenarioConfig(**kw)


PRESETS: dict[str, ScenarioConfig] = {
    "bridge_dependency": _preset(
        key="bridge_dependency", name="Across the River",
        description="South-bank rescue demand, north-bank shelter capacity. Ground teams must cross bridges; close a live bridge in Judge Mode to inspect rerouting.",
        flood_severity=0.3, boats=2, ground_units=4,
        shelter_capacity=[80, 0, 80], shelter_occupancy=[0, 0, 0], extra_shelters=False,
        report_noise=0, duplicate_rate=0, delayed_report_rate=0,
    ),
    "coordinator_outage": _preset(
        key="coordinator_outage",
        name="Coordinator Outage",
        description="Central dispatch fails at t=15 for 60 minutes. Existing missions continue; peer communication and local safety remain available. No standby coordinator is modelled.",
        schedule=[{"tick": 15, "type": "COORDINATOR_OUTAGE", "params": {"duration": 60}}],
    ),
    "live_ops": _preset(
        key="live_ops",
        name="Live operations",
        description="River level comes from the live feed (CWC gauge, the 2018 drill or a synthetic flood) and SOS calls arrive through the feed. Starts calm.",
        river_source="feed",
        approval_mode=True,
        duration=1440,
        boats=7,
        initial_incidents=4,
        arrival_incidents=0,
        report_noise=0.0,
        duplicate_rate=0.0,
        delayed_report_rate=0.0,
    ),
    "kerala_2018": _preset(
        key="kerala_2018",
        name="Kerala 2018 · flood replay",
        description="River discharge replays the real August 2018 flood (GloFAS) and SOS arrivals follow the real rainfall (ERA5), time-compressed to 180 minutes. Needs a Kerala map; other maps fall back to a synthetic rise.",
        flood_event="kerala_2018",
        flood_severity=1.15,
        boats=7,  # the real 2018 rescue relied on hundreds of fishing boats
        initial_incidents=10,
        arrival_incidents=22,
        arrival_window=150,
    ),
    "normal": _preset(
        key="normal",
        name="A · Normal Flood",
        description="Steady river rise, honest reports. Demonstrates routine negotiation and shelter reservations.",
    ),
    "flash_flood": _preset(
        key="flash_flood",
        name="Flash Flood",
        description="Water rises three times faster; low-lying roads close early and boats carry most rescues.",
        flood_severity=1.35,
        flood_peak_tick=35,
        arrival_incidents=18,
        arrival_window=70,
    ),
    "bridge_closure": _preset(
        key="bridge_closure",
        name="Critical Bridge Closure",
        description="The most heavily used bridge is closed by structural damage at t=15 while units depend on it.",
        schedule=[{"tick": 15, "type": "CLOSE_EDGE", "params": {"edge_id": "@critical", "reason": "structural damage"}}],
    ),
    "resource_shortage": _preset(
        key="resource_shortage",
        name="Resource Shortage",
        description="Only 3 boats and 2 ground teams are available for the same demand.",
        boats=3,
        ground_units=2,
    ),
    "shelter_overflow": _preset(
        key="shelter_overflow",
        name="Shelter Overflow",
        description="Shelter S2 loses most of its capacity at t=25 (power failure).",
        schedule=[{"tick": 25, "type": "SHELTER_CAPACITY", "params": {"shelter_id": "S2", "capacity": 16, "reason": "power failure"}}],
        extra_shelters=False,  # a capacity stress test: only the three configured shelters
    ),
    "contradictory": _preset(
        key="contradictory",
        name="B · Conflicting Information",
        description="Noisy, duplicated, delayed and contradictory SOS reports, including false alarms and a 3-tick comms delay.",
        report_noise=0.45,
        duplicate_rate=0.45,
        delayed_report_rate=0.3,
        false_alarm_rate=0.18,
        contradiction_rate=0.2,
        comm_delay=3,
    ),
    "triple_failure": _preset(
        key="triple_failure",
        name="C · Triple Failure",
        description="At t=22 a bridge in use closes, a committed boat fails and a shelter with inbound arrivals overflows — simultaneously.",
        schedule=[{"tick": 22, "type": "TRIPLE_FAILURE", "params": {}}],
    ),
}


def get_preset(key: str, **overrides: Any) -> ScenarioConfig:
    base = PRESETS.get(key, PRESETS["normal"])
    d = base.to_dict()
    d.update({k: v for k, v in overrides.items() if v is not None})
    if "policy" in overrides and overrides["policy"] and "weights" not in overrides:
        d["weights"] = dict(POLICIES.get(overrides["policy"], POLICIES["balanced"]))
    return ScenarioConfig.from_dict(d)
