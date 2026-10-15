"""Authoritative world-state data model.

Internal state uses plain dataclasses (cheap to deep-copy for counterfactual forks);
the API layer serialises them. Every mutable entity carries a `version` that is bumped
on each state transition, which the safety shield uses to reject stale commits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------- enumerations
# Plain string constants keep JSON serialisation trivial.


class EdgeKind:
    ROAD = "road"
    BRIDGE = "bridge"
    RIVER = "river"  # navigable channel, boats only
    JETTY = "jetty"  # slipway between river and bank, boats only


class ResourceKind:
    BOAT = "boat"
    GROUND = "ground"


class ResourceStatus:
    IDLE = "IDLE"
    EN_ROUTE_PICKUP = "EN_ROUTE_PICKUP"
    LOADING = "LOADING"
    EN_ROUTE_SHELTER = "EN_ROUTE_SHELTER"
    UNLOADING = "UNLOADING"
    HOLDING = "HOLDING"  # loaded, waiting for any shelter capacity
    DISABLED = "DISABLED"


class IncidentStatus:
    UNVERIFIED = "UNVERIFIED"  # evidence below provisional threshold
    SUSPECTED = "SUSPECTED"  # provisional dispatch allowed
    VERIFIED = "VERIFIED"
    ASSIGNED = "ASSIGNED"  # all reported people covered by commitments
    RESCUED = "RESCUED"
    FALSE_ALARM = "FALSE_ALARM"


class AssignmentStatus:
    COMMITTED = "COMMITTED"
    LOADED = "LOADED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ReservationStatus:
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


ACTIVE_ASSIGNMENT = (AssignmentStatus.COMMITTED, AssignmentStatus.LOADED)
OPEN_INCIDENT = (
    IncidentStatus.UNVERIFIED,
    IncidentStatus.SUSPECTED,
    IncidentStatus.VERIFIED,
    IncidentStatus.ASSIGNED,
)

# ---------------------------------------------------------------- graph


@dataclass
class Neighbourhood:
    id: str
    name: str
    flood_factor: float  # how strongly the synthetic water level rises here


@dataclass
class GraphNode:
    id: str
    x: float  # km, local projected coordinates (synthetic)
    y: float
    elevation: float  # metres above datum (synthetic)
    neighbourhood: str
    kind: str  # land | river | shelter | station
    label: str = ""
    depth: float = 0.0  # current synthetic flood depth (m)
    lat: float = 0.0  # real-geography maps only
    lng: float = 0.0
    elev_msl: Optional[float] = None  # DEM elevation above mean sea level (real maps)
    pop: int = 0  # buildings nearest this node (real maps); weights where people live


@dataclass
class GraphEdge:
    id: str
    u: str
    v: str
    kind: str
    length_km: float
    name: str = ""
    clearance: float = 99.0  # bridges: river level at which deck is unsafe
    manual_closed: bool = False  # closed by an injected event
    closed_reason: str = ""
    depth: float = 0.0  # mean flood depth over the edge
    ground_ok: bool = True
    boat_ok: bool = False
    risk: float = 0.0  # 0..1 route-risk attribute
    version: int = 0
    geometry: list[tuple[float, float]] = field(default_factory=list)  # km polyline u -> v (real maps)
    gated: bool = False  # jetty that is only usable by boats while its land end is flooded
    speed: float = 1.0  # ground speed factor from the road class (real maps)


# ---------------------------------------------------------------- incidents


@dataclass
class IncidentReport:
    id: str
    tick: int  # tick at which the report reaches the system
    source: str  # resident | responder | sensor
    reliability: float  # source reliability metadata (heuristic, uncalibrated)
    node: str
    people: Optional[int]  # None = missing information
    urgency: Optional[int]
    claim: str = "PRESENT"  # PRESENT | NO_VICTIMS
    location_uncertainty_km: float = 0.0
    truth_id: str = ""  # hidden ground-truth link (never read by agents)
    incident_id: str = ""  # incident it was reconciled into
    note: str = ""


@dataclass
class Incident:
    id: str
    node: str
    neighbourhood: str
    reported_tick: int
    urgency: int
    people_estimate: int  # people still believed to need rescue
    deadline_tick: int
    status: str = IncidentStatus.UNVERIFIED
    confidence: float = 0.0
    report_ids: list[str] = field(default_factory=list)
    people_rescued: int = 0
    first_pickup_tick: Optional[int] = None
    completed_tick: Optional[int] = None
    eligible_since: Optional[int] = None
    provisional: bool = False
    version: int = 1
    # hidden ground truth links (only revealed physically when a unit arrives)
    truth_ids: list[str] = field(default_factory=list)
    visited: bool = False
    # publication log for decentralised agents with communication delay
    publications: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------- resources


@dataclass
class RescueResource:
    id: str
    name: str
    kind: str
    capacity: int
    speed_kmh: float
    home_node: str
    node: str  # last node reached
    next_node: Optional[str] = None  # node being travelled to (mid-edge)
    edge_id: Optional[str] = None
    edge_progress: float = 0.0  # 0..1 along (node -> next_node)
    route: list[str] = field(default_factory=list)  # remaining nodes after next_node
    status: str = ResourceStatus.IDLE
    assignment_id: Optional[str] = None
    load: int = 0
    version: int = 1
    blackout_until: int = -1
    blackout_start: int = -1
    # closures discovered physically / via shield rejection: edge_id -> tick discovered
    known_closed: dict[str, int] = field(default_factory=dict)
    busy_ticks: int = 0
    idle_ticks: int = 0
    idle_with_demand_ticks: int = 0
    distance_km: float = 0.0
    timer: int = 0  # loading / unloading countdown
    retreat_edge: Optional[str] = None  # edge it is permitted to back out of after closure


# ---------------------------------------------------------------- shelters


@dataclass
class CapacityReservation:
    id: str
    shelter_id: str
    assignment_id: str
    resource_id: str
    incident_id: str
    people: int
    created_tick: int
    expires_tick: int
    status: str = ReservationStatus.ACTIVE
    version: int = 1


@dataclass
class Shelter:
    id: str
    name: str
    node: str
    capacity: int
    occupancy: int
    version: int = 1

    def reserved(self, world: "WorldState") -> int:
        return sum(
            r.people
            for r in world.reservations.values()
            if r.shelter_id == self.id and r.status == ReservationStatus.ACTIVE
        )

    def effective_free(self, world: "WorldState") -> int:
        return self.capacity - self.occupancy - self.reserved(world)


# ---------------------------------------------------------------- coordination


@dataclass
class Assignment:
    id: str
    resource_id: str
    incident_id: str
    shelter_id: str
    reservation_id: str
    people: int
    created_tick: int
    strategy: str
    round_id: str
    decision_id: str
    status: str = AssignmentStatus.COMMITTED
    eta_pickup_tick: int = 0
    eta_dropoff_tick: int = 0
    pickup_route: list[str] = field(default_factory=list)
    dropoff_route: list[str] = field(default_factory=list)
    cancel_reason: str = ""
    reroutes: int = 0
    version: int = 1
    loaded_people: int = 0


@dataclass
class SimulationEvent:
    id: str
    tick: int
    type: str
    params: dict[str, Any]
    source: str  # scenario | user | system
    seq: int = 0
    effects: list[str] = field(default_factory=list)


@dataclass
class AuditRecord:
    id: str
    tick: int
    category: str  # event | assignment | cancel | reroute | safety | negotiation | incident | shelter | movement
    message: str
    refs: dict[str, Any] = field(default_factory=dict)
    state_version: int = 0
    severity: str = "info"  # info | warn | critical | ok


@dataclass
class WorldState:
    tick: int
    seed: int
    scenario: str
    strategy: str
    nodes: dict[str, GraphNode]
    edges: dict[str, GraphEdge]
    neighbourhoods: dict[str, Neighbourhood]
    resources: dict[str, RescueResource]
    shelters: dict[str, Shelter]
    incidents: dict[str, Incident] = field(default_factory=dict)
    reports: dict[str, IncidentReport] = field(default_factory=dict)
    pending_reports: list[IncidentReport] = field(default_factory=list)
    reservations: dict[str, CapacityReservation] = field(default_factory=dict)
    assignments: dict[str, Assignment] = field(default_factory=dict)
    version: int = 0
    counters: dict[str, int] = field(default_factory=dict)
    flood_offset: float = 0.0  # added by flood-surge events
    coordinator_down_until: int = 0  # exclusive; local field execution remains available
    # edge closures as known per tick, used for communication delay modelling
    closure_log: list[dict[str, Any]] = field(default_factory=list)
    # hidden ground truth: truth_id -> {node, people, urgency, exists, remaining}
    truth: dict[str, dict[str, Any]] = field(default_factory=dict)
    # traversability history (tick -> closed edge ids per mode) for delayed agent beliefs
    trav_history: dict[int, dict[str, frozenset]] = field(default_factory=dict)
    # map source: id of the geography this world was built from, plus its static metadata
    map_id: str = "riverbend"
    meta: dict[str, Any] = field(default_factory=dict)
    # real flood event: normalised river discharge (0..1) per tick; None -> synthetic ramp
    flood_curve: Optional[list[float]] = None
    # live feed: river ramp (0..1) set by the latest gauge reading; overrides curve and synthetic ramp
    river_override: Optional[float] = None
    gauge_log: list[dict[str, Any]] = field(default_factory=list)

    def next_id(self, prefix: str) -> str:
        n = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = n
        return f"{prefix}-{n:03d}" if prefix not in ("B", "G", "S") else f"{prefix}{n}"

    def bump(self) -> int:
        self.version += 1
        return self.version
