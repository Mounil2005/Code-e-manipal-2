export type Strategy = "decentralized" | "centralized" | "greedy";

export interface WorldNode {
  id: string;
  x: number;
  y: number;
  elevation: number;
  neighbourhood: string;
  kind: "land" | "river" | "shelter" | "station";
  label: string;
  /** real-geography maps only */
  lat?: number;
  lng?: number;
  elev_msl?: number | null;
  pop?: number;
}
export interface WorldEdge {
  id: string;
  u: string;
  v: string;
  kind: "road" | "bridge" | "river" | "jetty";
  length_km: number;
  name: string;
  clearance: number | null;
  /** km polyline u → v (real-geography maps only) */
  geometry?: [number, number][];
  /** flood-access link: only drawn (and usable by boats) while the land end is flooded */
  gated?: boolean;
}
export interface MapInfo {
  id: string;
  name: string;
  region: string;
  real: boolean;
  note: string;
  source?: Record<string, string>;
  origin?: [number, number] | null;
  bbox?: [number, number, number, number] | null;
  projection?: { lat0: number; lon0: number; kx: number; ky: number } | null;
  critical_bridge?: string | null;
}
export interface FloodEvent {
  id: string;
  name: string;
  note: string;
  source: Record<string, string>;
  start: string;
  hours: number;
  real_hours_per_tick: number;
  baseline_m3s: number;
  peak_m3s: number;
  peak_time: string;
  rain_total_mm: number | null;
  /** IMD gauge-based daily totals, keyed by ISO date */
  rain_daily_mm?: Record<string, number> | null;
  discharge: number[];
  time_at: string[];
}
export interface MapListing extends MapInfo {
  nodes?: number;
  bridges?: number;
  shelters?: number;
  population?: number | null;
}
export interface StaticWorld {
  map: MapInfo;
  name: string;
  disclaimer: string;
  nodes: WorldNode[];
  edges: WorldEdge[];
  shelters: { id: string; name: string; node: string; amenity: string }[];
  neighbourhoods: { id: string; name: string; flood_factor: number }[];
}

export interface Resource {
  id: string;
  name: string;
  kind: "boat" | "ground";
  capacity: number;
  load: number;
  status: string;
  x: number;
  y: number;
  node: string;
  next_node: string | null;
  route: string[];
  assignment_id: string | null;
  incident_id: string | null;
  shelter_id: string | null;
  eta: number | null;
  blackout_until: number | null;
  version: number;
  distance_km: number;
  busy_ticks: number;
  idle_ticks: number;
  speed_kmh: number;
  home_node: string;
}
export interface Incident {
  id: string;
  node: string;
  x: number;
  y: number;
  neighbourhood: string;
  status: string;
  urgency: number;
  people: number;
  unassigned: number;
  rescued: number;
  confidence: number;
  provisional: boolean;
  deadline: number;
  reported_tick: number;
  first_pickup_tick: number | null;
  completed_tick: number | null;
  reports: string[];
  version: number;
  visited: boolean;
  assigned: string[];
}
export interface Shelter {
  id: string;
  name: string;
  node: string;
  x: number;
  y: number;
  capacity: number;
  occupancy: number;
  reserved: number;
  free: number;
  version: number;
}
export interface Assignment {
  id: string;
  resource_id: string;
  incident_id: string;
  shelter_id: string;
  reservation_id: string;
  people: number;
  loaded: number;
  status: string;
  created_tick: number;
  eta_pickup: number;
  eta_dropoff: number;
  reroutes: number;
  decision_id: string;
  round_id: string;
  cancel_reason: string;
  version: number;
}
export interface Metrics {
  tick: number;
  incidents_reported: number;
  real_incidents: number;
  open_incidents: number;
  served_incidents: number;
  rescued_incidents: number;
  people_total_known: number;
  people_rescued: number;
  people_in_transit: number;
  unmet_people: number;
  unmet_incidents: number;
  mean_time_to_service: number | null;
  median_time_to_service: number | null;
  p90_time_to_service: number | null;
  urgent_incidents: number;
  deadline_misses: number;
  idle_ticks: number;
  idle_with_demand_ticks: number;
  utilisation: number;
  route_violations: number;
  blocked_moves: number;
  safety_rejections: number;
  invariant_violations: number;
  fairness_jain: number;
  fairness_by_neighbourhood: { id: string; name: string; incidents: number; served: number; rescued_people: number; mean_wait: number | null; max_wait: number | null; deadline_misses: number }[];
  reassignments: number;
  reroutes: number;
  cancellations: number;
  false_alarm_visits: number;
  provisional_dispatches: number;
  negotiation_rounds: number;
  negotiation_messages: number;
  compute_ms: number;
  disruptions: number;
  mean_recovery_time: number | null;
  unresolved_disruptions: number;
  active_assignments: number;
  units_available: number;
  units_disabled: number;
}
export interface Frame {
  coordinator?: {available: boolean; restores_at: number};
  tick: number;
  version: number;
  strategy: Strategy;
  scenario: string;
  finished: boolean;
  duration: number;
  river_level: number;
  flood_real_time?: string | null;
  flood_discharge?: number | null;
  approval_mode?: boolean;
  recommendations?: Recommendation[];
  operator?: { approvals: number; rejections: number; overrides: number; mean_latency: number | null };
  flood_offset: number;
  resources: Resource[];
  incidents: Incident[];
  shelters: Shelter[];
  edges: Record<string, [number, number, number, number, number]>;
  depths: Record<string, number>;
  assignments: Assignment[];
  reservations: { id: string; shelter_id: string; resource_id: string; incident_id: string; people: number; expires: number; status: string }[];
  metrics: Metrics;
  last_event_id: string | null;
  audit_count: number;
  round_count: number;
}
export interface AuditRecord {
  id: string;
  tick: number;
  category: string;
  message: string;
  refs: Record<string, unknown>;
  state_version: number;
  severity: "info" | "warn" | "critical" | "ok";
}
export interface RoundSummary {
  id: string;
  tick: number;
  strategy: string;
  trigger: string;
  iterations: number;
  commits: string[];
  rejections: number;
  safety_rejections: number;
  duration_ms: number;
  incidents: string[];
  resources: string[];
  message_count: number;
  matching: { pass: number; resource: string; incident: string; cost: number }[];
}
export interface Message {
  seq: number;
  tick: number;
  type: string;
  from: string;
  to: string;
  incident: string;
  payload: Record<string, any>;
  reason: string;
}
export interface Candidate {
  resource_id: string;
  feasible: boolean;
  reason: string;
  people?: number;
  eta_pickup?: number | null;
  eta_total?: number | null;
  cost: number | null;
  components?: Record<string, number>;
  shelter?: string | null;
  pickup_path?: string[];
  versions?: { resource: number; incident: number };
  knowledge?: string;
}
export interface Decision {
  id: string;
  tick: number;
  round_id: string;
  strategy: string;
  incident_id: string;
  state_version: number;
  incident: { status: string; confidence: number; people: number; unassigned: number; urgency: number; deadline: number; node: string; neighbourhood: string; version: number; known_version: number; eligibility: string };
  evidence: { id: string; source: string; reliability: number; claim: string; people: number | null; urgency: number | null; tick: number; node: string; note: string }[];
  candidates: Candidate[];
  rejections: { resource: string; reason: string }[];
  selected: Candidate | null;
  shelter_log: { shelter: string; accepted: boolean; reason: string; reservation?: string }[];
  safety_checks: { name: string; passed: boolean; detail: string }[];
  outcome: string;
  explanation: string;
  what_would_change: string;
  weights: Record<string, number>;
  assignment_id?: string;
  shelter?: string;
  rejection_code?: string;
  assignment?: { id: string; status: string; cancel_reason: string; reroutes: number } | null;
}
export interface ScenarioConfig {
  key: string;
  name: string;
  description: string;
  map?: string;
  seed: number;
  duration: number;
  flood_severity: number;
  flood_peak_tick: number;
  initial_incidents: number;
  arrival_incidents: number;
  arrival_window: number;
  boats: number;
  ground_units: number;
  shelter_capacity: number[];
  shelter_occupancy: number[];
  report_noise: number;
  duplicate_rate: number;
  delayed_report_rate: number;
  false_alarm_rate: number;
  contradiction_rate: number;
  comm_delay: number;
  policy: string;
  weights: Record<string, number>;
  neighbourhood_priority: Record<string, number>;
  schedule: { tick: number; type: string; params: Record<string, unknown> }[];
}
export interface SimSummary {
  id: string;
  scenario: string;
  scenario_name: string;
  strategy: Strategy;
  status: "ready" | "running" | "paused" | "finished";
  speed: number;
  tick: number;
  duration: number;
  version: number;
  seed: number;
  map?: string;
  map_name?: string;
  flood_event?: FloodEvent | null;
  config: ScenarioConfig;
  injected: { tick: number; seq: number; type: string; params: Record<string, unknown>; source: string }[];
}

// ---------------------------------------------------------------- live feed
export interface FeedDelta {
  since_tick: number;
  minutes: number;
  roads_closed: string[];
  roads_closed_count: number;
  roads_reopened_count: number;
  boat_streets_opened: number;
  rerouted: { assignment: string; resource: string; incident: string; old_km: number | null; new_km: number | null }[];
  new_assignments: { assignment: string; resource: string; incident: string; shelter: string; people: number; decision_id: string; why: string }[];
  cancelled: { assignment: string; resource: string; incident: string; reason: string }[];
  reassigned: { incident: string; from: string; to: string }[];
  camps: { shelter: string; name: string; occupancy: number; occ_delta: number; reserved: number; reserved_delta: number; capacity: number; full: boolean }[];
  supplies: { people_in_camps: number; new_arrivals: number; water_l_per_day: number; ration_kits_per_day: number; added_water_l_per_day: number; added_kits_per_day: number; standard: string };
  rescued: number;
  new_incidents: number;
}
export interface FeedMessage {
  seq: number;
  received_at: number;
  tick: number;
  type: "gauge" | "sos" | "duplicate" | "contradiction" | "road" | "shelter" | "dam";
  source: "live" | "drill" | "synthetic" | "external";
  label: string;
  time: string | null;
  payload: Record<string, any>;
  event_id: string;
  effects: string[];
  delta: FeedDelta | null;
}
export interface FeedStatus {
  mode: "live" | "drill" | "synthetic" | null;
  running: boolean;
  params: Record<string, any>;
  note: string;
  count: number;
  current_delta: FeedDelta | null;
  gauge: { tick: number; level_m: number; ramp: number; time: string | null; station: string | null; source: string } | null;
}
export interface PriorityRow {
  rank: number;
  incident: string;
  node: string;
  neighbourhood: string;
  people: number;
  unassigned: number;
  urgency: number;
  deadline_in: number;
  confidence: number;
  status: string;
  eligibility: string;
  reports: number;
  sources: string[];
  contradicted: boolean;
  state: "assigned" | "waiting" | "partly assigned";
  why_waiting: string;
  assigned: { resource: string; shelter: string; people: number; phase: string; eta_min: number; decision_id: string }[];
  cost: { T: number; R: number; U: number; F: number; penalty: number } | null;
  decision_id: string | null;
}
export interface LiveCwc {
  available: boolean;
  reason?: string;
  stale?: boolean;
  source?: string;
  gauge?: { code: string; name: string; river: string; level_m: number | null; time: string | null; history: [string, number][]; rain_mm_24h: number; ramp: number | null } | null;
  dams?: { code: string; name: string; outflow_m3s: number | null; inflow_m3s: number | null; level_m: number | null; time: string | null; outflow_history: [string, number][] }[];
}

export interface Recommendation {
  id: string;
  tick: number;
  age: number;
  resource: string;
  incident: string;
  people: number;
  shelter: string | null;
  eta_pickup: number;
  eta_total: number;
  cost: number;
  components: { T: number; R: number; U: number; F: number; penalty?: number };
  decision_id: string;
  alternatives: { resource: string; cost: number | null }[];
  why: string;
}
