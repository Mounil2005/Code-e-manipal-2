import { create } from "zustand";
import { api, ApiError } from "../lib/api";
import type { AuditRecord, FeedMessage, FeedStatus, Frame, MapListing, RoundSummary, SimSummary, StaticWorld, Strategy } from "../types";

export type Selection = { type: "incident" | "resource" | "shelter" | "edge"; id: string } | null;
/** Where the map camera should go: one point (fly to) or several (fit them). `key` re-triggers. */
export type Focus = { points: [number, number][]; key: number; zoom?: number; fit?: boolean } | null;
export interface LastEvent {
  type: string;
  tick: number;
  effects: string[];
}
export type Conn = "connecting" | "live" | "reconnecting" | "offline";
export interface Toast {
  id: number;
  kind: "ok" | "warn" | "crit" | "info";
  title: string;
  body?: string;
}
export interface Layers {
  roads: boolean;
  water: boolean;
  flood: boolean;
  closures: boolean;
  routes: boolean;
  incidents: boolean;
  units: boolean;
  shelters: boolean;
  uncertain: boolean;
  labels: boolean;
  satellite: boolean;
}

interface SimState {
  world: StaticWorld | null;
  maps: MapListing[];
  /** the map new simulations are created on */
  mapId: string;
  setMap: (id: string) => Promise<void>;
  sim: SimSummary | null;
  frame: Frame | null;
  conn: Conn;
  audit: AuditRecord[];
  rounds: RoundSummary[];
  selection: Selection;
  decisionId: string | null;
  layers: Layers;
  toasts: Toast[];
  focus: Focus;
  setFocus: (f: Omit<NonNullable<Focus>, "key"> | null) => void;
  lastEvent: LastEvent | null;
  feed: FeedMessage[];
  feedStatus: FeedStatus | null;
  lastSeq: number;
  busy: Record<string, boolean>;
  setSelection: (s: Selection) => void;
  openDecision: (id: string | null) => void;
  toggleLayer: (k: keyof Layers) => void;
  toast: (t: Omit<Toast, "id">) => void;
  dismiss: (id: number) => void;
  loadWorld: (mapId?: string) => Promise<void>;
  bootstrap: () => Promise<void>;
  createSim: (scenario: string, strategy: Strategy, overrides?: Record<string, unknown>, map?: string) => Promise<void>;
  attach: (id: string) => Promise<void>;
  control: (action: "start" | "pause" | "reset" | "step", ticks?: number) => Promise<void>;
  setSpeed: (speed: number) => Promise<void>;
  inject: (type: string, params: Record<string, unknown>, label?: string) => Promise<boolean>;
  applySocket: (msg: any) => void;
  setConn: (c: Conn) => void;
}

const SIM_KEY = "resqnet.sim";
const MAP_KEY = "resqnet.map";
const DEFAULT_MAP = "kerala_aluva";
let toastSeq = 1;
const worldCache: Record<string, StaticWorld> = {};

/** Map points of the things that just broke: newly closed edges, failed units, shrunk shelters. */
function disruptedPoints(world: StaticWorld | null, prev: Frame | null, next: Frame | null): [number, number][] {
  if (!world || !prev || !next) return [];
  const pts: [number, number][] = [];
  const node = (id: string) => world.nodes.find((n) => n.id === id);
  for (const e of world.edges) {
    const a = prev.edges[e.id];
    const b = next.edges[e.id];
    if (a && b && !a[2] && b[2]) {
      const g = e.geometry;
      if (g && g.length) pts.push(g[Math.floor(g.length / 2)]);
      else {
        const u = node(e.u);
        const v = node(e.v);
        if (u && v) pts.push([(u.x + v.x) / 2, (u.y + v.y) / 2]);
      }
    }
  }
  for (const r of next.resources) {
    const p = prev.resources.find((x) => x.id === r.id);
    if (p && p.status !== "DISABLED" && r.status === "DISABLED") pts.push([r.x, r.y]);
  }
  for (const s of next.shelters) {
    const p = prev.shelters.find((x) => x.id === s.id);
    if (p && s.capacity < p.capacity) pts.push([s.x, s.y]);
  }
  return pts;
}
let focusSeq = 1;
const focusFor = (s: { world: StaticWorld | null; frame: Frame | null }, next: Frame | null) => {
  const pts = disruptedPoints(s.world, s.frame, next);
  return pts.length ? { focus: { points: pts, key: focusSeq++ } } : {};
};

async function fetchWorld(mapId: string): Promise<StaticWorld> {
  if (!worldCache[mapId]) worldCache[mapId] = await api.get<StaticWorld>(`/api/world?map=${encodeURIComponent(mapId)}`);
  return worldCache[mapId];
}

export const useSim = create<SimState>((set, get) => ({
  world: null,
  maps: [],
  mapId: DEFAULT_MAP,
  setMap: async (id) => {
    if (id === get().mapId && get().sim?.map === id) return;
    try {
      localStorage.setItem(MAP_KEY, id);
    } catch {
      /* ignore */
    }
    set({ mapId: id });
    const sim = get().sim;
    await get().createSim(sim?.scenario ?? "normal", (sim?.strategy ?? "decentralized") as Strategy, {}, id);
  },
  sim: null,
  frame: null,
  conn: "connecting",
  audit: [],
  rounds: [],
  selection: null,
  decisionId: null,
  layers: { roads: true, water: true, flood: true, closures: true, routes: true, incidents: true, units: true, shelters: true, uncertain: true, labels: false, satellite: false },
  toasts: [],
  focus: null,
  setFocus: (f) => set({ focus: f ? { ...f, key: focusSeq++ } : null }),
  lastEvent: null,
  feed: [],
  feedStatus: null,
  lastSeq: 0,
  busy: {},
  setSelection: (selection) => set({ selection }),
  openDecision: (decisionId) => set({ decisionId }),
  toggleLayer: (k) => set((s) => ({ layers: { ...s.layers, [k]: !s.layers[k] } })),
  toast: (t) => {
    const id = toastSeq++;
    set((s) => ({ toasts: [...s.toasts.slice(-4), { ...t, id }] }));
    setTimeout(() => get().dismiss(id), t.kind === "crit" ? 7000 : 4500);
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })),
  loadWorld: async (mapId) => {
    const id = mapId ?? get().mapId;
    if (get().world?.map.id === id) return;
    set({ world: await fetchWorld(id) });
  },
  bootstrap: async () => {
    try {
      let preferred = DEFAULT_MAP;
      try {
        preferred = localStorage.getItem(MAP_KEY) || DEFAULT_MAP;
      } catch {
        /* storage unavailable */
      }
      const maps = (await api.get<{ maps: MapListing[] }>("/api/maps")).maps;
      if (!maps.some((m) => m.id === preferred)) preferred = maps.find((m) => m.real)?.id ?? maps[0].id;
      set({ maps, mapId: preferred });
      await get().loadWorld(preferred);
    } catch (e) {
      set({ conn: "offline" });
      get().toast({ kind: "crit", title: "Backend unreachable", body: (e as Error).message });
      return;
    }
    let id: string | null = null;
    try {
      id = localStorage.getItem(SIM_KEY);
    } catch {
      /* storage unavailable */
    }
    if (id) {
      try {
        await get().attach(id);
        if (get().sim?.map === get().mapId) return;
      } catch {
        /* stale id → create fresh */
      }
    }
    await get().createSim("normal", "decentralized");
  },
  createSim: async (scenario, strategy, overrides = {}, map) => {
    set((s) => ({ busy: { ...s.busy, create: true } }));
    try {
      const r = await api.post<SimSummary & { frame: Frame }>("/api/simulations", { scenario, strategy, overrides, map: map ?? get().mapId });
      const { frame, ...sim } = r;
      const world = await fetchWorld(sim.map ?? get().mapId);
      try {
        localStorage.setItem(SIM_KEY, sim.id);
      } catch {
        /* ignore */
      }
      set({ world, mapId: sim.map ?? get().mapId, sim, frame, audit: [], rounds: [], selection: null, decisionId: null, lastSeq: 0, lastEvent: null, feed: [], feedStatus: null, focus: { points: [], fit: true, key: focusSeq++ } });
    } catch (e) {
      get().toast({ kind: "crit", title: "Could not create simulation", body: (e as Error).message });
      throw e;
    } finally {
      set((s) => ({ busy: { ...s.busy, create: false } }));
    }
  },
  attach: async (id) => {
    const r = await api.get<SimSummary & { frame: Frame }>(`/api/simulations/${id}`);
    const { frame, ...sim } = r;
    const world = await fetchWorld(sim.map ?? "riverbend");
    try {
      localStorage.setItem(SIM_KEY, sim.id);
    } catch {
      /* ignore */
    }
    set({ world, sim, frame, audit: [], rounds: [], selection: null, lastSeq: 0, feed: [], feedStatus: null });
  },
  control: async (action, ticks = 1) => {
    const sim = get().sim;
    if (!sim) return;
    set((s) => ({ busy: { ...s.busy, [action]: true } }));
    try {
      const r = await api.post<any>(`/api/simulations/${sim.id}/${action}`, action === "step" ? { ticks } : {});
      const { frame, ...summary } = r;
      set((s) => ({ sim: { ...s.sim!, ...summary }, ...(frame ? { frame, ...(action === "reset" ? {} : focusFor(s, frame)) } : {}), ...(action === "reset" ? { audit: [], rounds: [], selection: null } : {}) }));
    } catch (e) {
      get().toast({ kind: "crit", title: `${action} failed`, body: (e as ApiError).message });
    } finally {
      set((s) => ({ busy: { ...s.busy, [action]: false } }));
    }
  },
  setSpeed: async (speed) => {
    const sim = get().sim;
    if (!sim) return;
    try {
      const r = await api.post<SimSummary>(`/api/simulations/${sim.id}/speed`, { speed });
      set((s) => ({ sim: { ...s.sim!, ...r } }));
    } catch (e) {
      get().toast({ kind: "crit", title: "Speed change failed", body: (e as Error).message });
    }
  },
  inject: async (type, params, label) => {
    const sim = get().sim;
    if (!sim) return false;
    set((s) => ({ busy: { ...s.busy, [type]: true } }));
    try {
      const r = await api.post<any>(`/api/simulations/${sim.id}/events`, { type, params });
      set((s) => ({ frame: r.frame, ...focusFor(s, r.frame), lastEvent: { type, tick: r.event.tick, effects: r.event.effects as string[] } }));
      get().toast({ kind: type.includes("FAIL") || type === "CLOSE_EDGE" || type === "SHELTER_CAPACITY" ? "warn" : "info", title: label ?? type.replace(/_/g, " ").toLowerCase(), body: (r.event.effects as string[]).join(" · ") });
      return true;
    } catch (e) {
      get().toast({ kind: "crit", title: "Rejected by engine", body: (e as Error).message });
      return false;
    } finally {
      set((s) => ({ busy: { ...s.busy, [type]: false } }));
    }
  },
  applySocket: (msg) => {
    const st = get();
    if (!st.sim || msg.sim_id !== st.sim.id) return;
    if (msg.type === "error") return;
    const full = !!msg.full;
    set((s) => {
      const merge = <T extends { id: string }>(prev: T[], next: T[] = [], cap: number) => {
        const seen = new Set(prev.map((x) => x.id));
        return [...prev, ...next.filter((x) => !seen.has(x.id))].slice(-cap);
      };
      const audit = full ? msg.audit : msg.type === "reset" ? [] : merge(s.audit, msg.audit, 800);
      const rounds = full ? msg.rounds : msg.type === "reset" ? [] : merge(s.rounds, msg.rounds, 300);
      let feed = s.feed;
      if (full || msg.type === "reset") feed = (msg.feed as FeedMessage[]) ?? [];
      else if (msg.feed?.length) {
        const bySeq = new Map(s.feed.map((m) => [m.seq, m]));
        for (const m of msg.feed as FeedMessage[]) bySeq.set(m.seq, m);
        feed = [...bySeq.values()].sort((a, b) => a.seq - b.seq).slice(-400);
      }
      return {
        feed,
        feedStatus: msg.feed_status ?? s.feedStatus,
        frame: msg.frame ?? s.frame,
        ...(msg.frame && msg.type !== "reset" && !full ? focusFor(s, msg.frame) : {}),
        sim: { ...s.sim!, status: msg.status, speed: msg.speed, tick: msg.frame?.tick ?? s.sim!.tick, version: msg.state_version, ...(msg.summary ?? {}) },
        audit,
        rounds,
        lastSeq: msg.seq ?? s.lastSeq,
      };
    });
  },
  setConn: (conn) => set({ conn }),
}));
