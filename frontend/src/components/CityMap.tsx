import { createPortal } from "react-dom";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { Layers as LayersIcon, Minus, Plus, Scan } from "lucide-react";
import type { StaticWorld } from "../types";
import type { Focus, Layers, Selection } from "../store/sim";
import { NB_COLOR, URGENCY_COLOR } from "../lib/format";
import { BASE_LAYERS, DESIGN_PX_PER_KM, edgeLookup, neighbourhoodColors, pathPoints, pxPerKm, toPoints, xyToLatLng } from "../lib/geo";
import { cx } from "./ui";

/** Minimal frame shape the map needs (full frames and counterfactual compact frames). */
export interface MapFrame {
  route_ghosts?: {resource: string; tick: number; edge: string; path: string[]}[];
  tick: number;
  resources: { id: string; kind: string; x: number; y: number; node?: string | null; status: string; load: number; incident_id: string | null; route: string[]; blackout_until?: number | null }[];
  incidents: { id: string; x: number; y: number; status: string; urgency: number; people: number; unassigned: number; confidence?: number; provisional?: boolean }[];
  shelters: { id: string; x: number; y: number; capacity: number; occupancy: number; reserved: number; name?: string }[];
  edges: Record<string, number[]>;
  depths?: Record<string, number>;
}

interface Props {
  world: StaticWorld;
  frame: MapFrame;
  layers?: Partial<Layers>;
  selection?: Selection;
  onSelect?: (s: Selection) => void;
  onEdgeClick?: (edgeId: string) => void;
  onNodeClick?: (nodeId: string) => void;
  mode?: "select" | "close-edge" | "new-incident";
  transitionMs?: number;
  compact?: boolean;
  className?: string;
  highlight?: string[];
  /** fly the camera here (real-geography maps); a new `key` re-triggers */
  focus?: Focus;
  /** observed water (Sentinel-1) as horizontal pixel runs [x0, y, x1] in km */
  observed?: { runs: number[][]; row_km: number } | null;
}

const ALL: Layers = { roads: true, water: true, flood: true, closures: true, routes: true, incidents: true, units: true, shelters: true, uncertain: true, labels: false, satellite: true };
const PAD = 0.7;
const MARKER_SCALE = 0.82;

// ------------------------------------------------------------------ shared scene
/**
 * Everything drawn on the map, in km coordinates. `geo` switches between the synthetic
 * city (stroke widths in km, scaled by the SVG's own zoom) and a real-geography overlay
 * hosted inside Leaflet (stroke widths in screen px, markers counter-scaled by --k so
 * they keep a constant on-screen size at every zoom).
 */
function Scene({ world, frame, layers: layerProp, selection, onSelect, onEdgeClick, onNodeClick, mode = "select", transitionMs = 900, compact, highlight = [], geo, clicked, observed }: Props & { geo: boolean; clicked: (e?: React.MouseEvent) => boolean }) {
  const layers = { ...ALL, ...layerProp };
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const nodes = useMemo(() => Object.fromEntries(world.nodes.map((n) => [n.id, n])), [world]);
  const edgeIdx = useMemo(() => edgeLookup(world), [world]);
  const nbColor = useMemo(() => ({ ...NB_COLOR, ...neighbourhoodColors(world) }), [world]);
  const river = useMemo(() => world.nodes.filter((n) => n.kind === "river").sort((a, b) => a.x - b.x), [world]);
  const nbCentroids = useMemo(() => {
    const acc: Record<string, { x: number; y: number; n: number }> = {};
    for (const n of world.nodes) {
      if (n.kind === "river") continue;
      const a = (acc[n.neighbourhood] ??= { x: 0, y: 0, n: 0 });
      a.x += n.x;
      a.y += n.y;
      a.n += 1;
    }
    return Object.entries(acc).map(([id, a]) => ({ id, x: a.x / a.n, y: a.y / a.n, name: world.neighbourhoods.find((z) => z.id === id)?.name ?? id }));
  }, [world]);

  // `lw`: widths of lines drawn straight on the map (screen px on the geo overlay via
  // vector-effect, km on the synthetic city). `sw`: widths inside marker groups, which are
  // drawn in design units and counter-scaled on the geo overlay.
  const lw = (px: number) => (geo ? px : px / 100);
  const sw = (px: number) => px / 100;
  const dash = (a: number, b: number) => (geo ? `${(a * DESIGN_PX_PER_KM).toFixed(1)} ${(b * DESIGN_PX_PER_KM).toFixed(1)}` : `${a} ${b}`);
  const ve = geo ? ({ vectorEffect: "non-scaling-stroke" } as const) : {};
  const halo = geo ? ({ stroke: "#fff", strokeWidth: 0.05, paintOrder: "stroke", strokeLinejoin: "round" } as const) : {};
  const hit = geo ? "hit" : "";
  const mk = geo ? "mk" : undefined;

  const routeColor = (status: string) => (status === "EN_ROUTE_SHELTER" || status === "UNLOADING" ? "#15803d" : status === "HOLDING" ? "#dc2626" : "#0891b2");
  const sel = (t: string, id: string) => selection?.type === t && selection.id === id;
  const depths = frame.depths ?? {};
  const level = ((frame as { river_level?: number }).river_level ?? 2) as number;
  const geometryOf = (e: StaticWorld["edges"][number]): [number, number][] => {
    const a = nodes[e.u];
    const b = nodes[e.v];
    return geo && e.geometry && e.geometry.length > 1 ? e.geometry : [[a.x, a.y], [b.x, b.y]];
  };

  return (
    <>
      <defs>
        <radialGradient id={`flood${uid}`}>
          <stop offset="0%" stopColor="#1f86c9" stopOpacity="0.6" />
          <stop offset="55%" stopColor="#3b9ddb" stopOpacity="0.32" />
          <stop offset="100%" stopColor="#5ab4e6" stopOpacity="0" />
        </radialGradient>
      </defs>
      {/* synthetic city only: district tint and the hand-drawn river */}
      {!geo &&
        world.nodes
          .filter((n) => n.kind !== "river")
          .map((n) => <circle key={"d" + n.id} cx={n.x} cy={n.y} r={0.78} fill={nbColor[n.neighbourhood]} opacity={0.035} />)}
      {!geo && layers.water && river.length > 1 && (
        <>
          <polyline
            points={[`${river[0].x - 1.2},${river[0].y}`, ...river.map((r) => `${r.x},${r.y}`), `${river[river.length - 1].x + 1.2},${river[river.length - 1].y}`].join(" ")}
            fill="none"
            stroke="#d4e9f4"
            strokeWidth={0.62 + Math.max(0, (level - 2) * 0.1)}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <polyline points={river.map((r) => `${r.x},${r.y}`).join(" ")} fill="none" stroke="#b9dbec" strokeWidth={0.16} strokeLinecap="round" strokeDasharray="0.02 0.18" />
        </>
      )}
      {/* geo: the navigable channel, widening with the river level */}
      {geo &&
        layers.water &&
        world.edges
          .filter((e) => e.kind === "river")
          .map((e) => {
            const pts = toPoints(geometryOf(e));
            return (
              <g key={"rv" + e.id}>
                <polyline points={pts} fill="none" stroke="#38bdf8" strokeOpacity={0.28} strokeWidth={7 + Math.max(0, level - 1.5) * 4} strokeLinecap="round" strokeLinejoin="round" {...ve} />
                <polyline points={pts} fill="none" stroke="#0ea5e9" strokeOpacity={0.75} strokeWidth={1.4} strokeDasharray="1 5" strokeLinecap="round" {...ve} />
              </g>
            );
          })}
      {/* flood */}
      {layers.flood &&
        (geo ? (
          <g style={{ mixBlendMode: "multiply" }}>
            {Object.entries(depths).map(([id, d]) => {
              const n = nodes[id];
              if (!n || n.kind === "river") return null;
              const r = 0.32 + 0.3 * Math.min(d, 2.2);
              return <circle key={"f" + id} cx={n.x} cy={n.y} r={r} fill={`url(#flood${uid})`} opacity={Math.min(1, 0.55 + 0.4 * Math.min(d / 1.6, 1))} />;
            })}
          </g>
        ) : (
          <g filter={`url(#blur${uid})`}>
            <defs>
              <filter id={`blur${uid}`} x="-50%" y="-50%" width="200%" height="200%">
                <feGaussianBlur stdDeviation="0.18" />
              </filter>
            </defs>
            {Object.entries(depths).map(([id, d]) => {
              const n = nodes[id];
              if (!n || n.kind === "river") return null;
              return <circle key={"f" + id} cx={n.x} cy={n.y} r={0.35 + 0.28 * Math.min(d, 2)} fill={d >= 0.5 ? "#7fb9d6" : "#a9d2e6"} opacity={0.22 + 0.28 * Math.min(d / 1.6, 1)} />;
            })}
          </g>
        ))}
      {/* observed water, Sentinel-1 */}
      {geo && observed && layers.satellite && (
        <g opacity={0.55} pointerEvents="none">
          {observed.runs.map(([x0, y, x1], i) => (
            <rect key={"s1" + i} x={x0} y={y - observed.row_km / 2} width={Math.max(0.002, x1 - x0)} height={observed.row_km} fill="#c026d3" />
          ))}
        </g>
      )}
      {/* district labels */}
      {!compact &&
        nbCentroids.map((c) => (
          <g key={"nl" + c.id} transform={`translate(${c.x} ${geo ? c.y : c.y + (c.y < 3 ? -0.95 : 1.05)})`}>
            <g className={mk}>
              <text textAnchor="middle" fontSize={geo ? 0.17 : 0.2} fontWeight={geo ? 500 : 400} fontFamily="JetBrains Mono" letterSpacing={geo ? 0.04 : 0.02} fill={nbColor[c.id]} opacity={geo ? 0.85 : 0.75} {...halo}>
                {c.name.toUpperCase()}
              </text>
            </g>
          </g>
        ))}
      {/* roads, bridges, jetties */}
      {layers.roads &&
        world.edges.map((e) => {
          const st = frame.edges[e.id] ?? [1, 0, 0];
          const [g, boat, manual] = st;
          const isWater = e.kind === "river" || e.kind === "jetty";
          if (isWater && !layers.water) return null;
          if (geo && e.kind === "river") return null; // drawn as the channel above
          if (e.gated && !boat) return null; // flood-access links only exist while the land is under water
          let stroke = geo ? "#6b7685" : "#cfcfca";
          let width = lw(geo ? 3.4 : 4.5);
          let dashArr: string | undefined;
          let op = 1;
          if (e.kind === "bridge") {
            stroke = geo ? "#111827" : "#27272a";
            width = lw(geo ? 6 : 9);
          }
          if (e.kind === "jetty") {
            stroke = "#6bb7da";
            width = lw(geo ? 2.2 : 2.5);
            dashArr = dash(0.05, 0.06);
          }
          if (e.kind === "river") stroke = "transparent";
          if (!isWater && !g && layers.closures) {
            stroke = boat ? "#4aa3cc" : "#7cb9d6";
            dashArr = dash(0.1, 0.07);
            op = 0.95;
          }
          if (manual && layers.closures) {
            stroke = "#dc2626";
            dashArr = dash(0.12, 0.08);
            width = lw(e.kind === "bridge" ? (geo ? 6 : 9) : geo ? 4.4 : 6);
          }
          if (!isWater && !g && e.kind === "bridge" && !manual && layers.closures) {
            stroke = "#d97706";
            dashArr = dash(0.12, 0.08);
          }
          const pts = toPoints(geometryOf(e));
          const geom = geometryOf(e);
          const mid = geom[Math.floor(geom.length / 2)];
          const mx = geom.length % 2 === 0 ? (geom[geom.length / 2 - 1][0] + geom[geom.length / 2][0]) / 2 : mid[0];
          const my = geom.length % 2 === 0 ? (geom[geom.length / 2 - 1][1] + geom[geom.length / 2][1]) / 2 : mid[1];
          const hot = sel("edge", e.id);
          return (
            <g key={e.id}>
              {geo && !isWater && <polyline points={pts} fill="none" stroke="#fff" strokeOpacity={0.92} strokeWidth={width + 2.6} strokeLinecap="round" strokeLinejoin="round" {...ve} />}
              {boat && e.kind === "road" && layers.flood ? <polyline points={pts} fill="none" stroke={geo ? "#9fd6f0" : "#cfe6f2"} strokeOpacity={geo ? 0.7 : 1} strokeWidth={geo ? 12 : lw(14)} strokeLinecap="round" strokeLinejoin="round" {...ve} /> : null}
              <polyline points={pts} fill="none" stroke={hot ? "#0891b2" : stroke} strokeWidth={hot ? width * 1.6 : width} strokeDasharray={dashArr} strokeLinecap="round" strokeLinejoin="round" opacity={op} {...ve} />
              {(onEdgeClick || onSelect) && !isWater && (
                <polyline
                  points={pts}
                  fill="none"
                  stroke="transparent"
                  strokeWidth={geo ? 18 : lw(22)}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className={cx(geo && "hit-stroke", mode === "close-edge" && "cursor-pointer hover:stroke-[#dc262633]")}
                  {...ve}
                  onClick={(ev) => {
                    if (!clicked(ev)) return;
                    if (mode === "close-edge" && onEdgeClick) onEdgeClick(e.id);
                    else onSelect?.({ type: "edge", id: e.id });
                  }}
                >
                  <title>{`${e.name || e.id} · ${e.kind}${manual ? " · CLOSED" : !g ? " · impassable for ground" : ""}${boat && e.kind === "road" ? " · navigable by boat" : ""}`}</title>
                </polyline>
              )}
              {manual && layers.closures ? (
                <g transform={`translate(${mx} ${my})`}>
                  <g className={mk}>
                    <circle r={0.11} fill="#fff" stroke="#dc2626" strokeWidth={sw(2.5)} />
                    <path d="M-0.05 -0.05L0.05 0.05M0.05 -0.05L-0.05 0.05" stroke="#dc2626" strokeWidth={sw(2.5)} strokeLinecap="round" />
                  </g>
                </g>
              ) : null}
              {e.kind === "bridge" && !compact && (
                <g transform={`translate(${mx} ${my})`}>
                  <g className={mk}>
                    <text x={0.16} y={0.05} fontSize={0.15} fontFamily="JetBrains Mono" fontWeight={geo ? 600 : 400} fill={manual ? "#dc2626" : "#3f3f46"} {...halo}>
                      {geo ? e.id : e.name}
                      <title>{e.name}</title>
                    </text>
                  </g>
                </g>
              )}
            </g>
          );
        })}
      {/* nodes (for incident placement) */}
      {mode === "new-incident" &&
        world.nodes
          .filter((n) => n.kind === "land")
          .map((n) => (
            <g key={"n" + n.id} transform={`translate(${n.x} ${n.y})`}>
              <g className={mk}>
                <circle r={0.12} fill="#fff" stroke="#0891b2" strokeWidth={sw(2)} className={cx(hit, "cursor-pointer hover:fill-[#e0f7fb]")} onClick={(ev) => clicked(ev) && onNodeClick?.(n.id)}>
                  <title>{`${n.id} · ${n.neighbourhood} · elev ${n.elevation} m · depth ${depths[n.id] ?? 0} m`}</title>
                </circle>
              </g>
            </g>
          ))}
      {layers.labels &&
        world.nodes.map((n) => (
          <g key={"lb" + n.id} transform={`translate(${n.x} ${n.y - 0.13})`}>
            <g className={mk}>
              <text textAnchor="middle" fontSize={0.11} fontFamily="JetBrains Mono" fill="#71717a" {...halo}>
                {n.id}
              </text>
            </g>
          </g>
        ))}
      {/* routes */}
      {layers.routes &&
        frame.route_ghosts?.map((g, i) => (
          <polyline
            key={`${g.resource}-${g.tick}-${i}`}
            points={toPoints(pathPoints(g.path, nodes, edgeIdx))}
            fill="none"
            stroke="#e11d48"
            strokeWidth={lw(geo ? 3 : 4)}
            strokeDasharray={dash(0.12, 0.1)}
            opacity={0.7}
            pointerEvents="none"
            {...ve}
          >
            <title>{g.resource}: route before {g.edge} closed at minute {g.tick}</title>
          </polyline>
        ))}
      {layers.routes &&
        frame.resources.map((r) => {
          if (!r.route?.length || r.status === "DISABLED") return null;
          const ids = r.route.filter((id) => nodes[id]);
          const from = { x: r.x, y: r.y };
          // the unit is part-way along node -> route[0]; follow the real road shape from there
          const line = r.node && nodes[r.node] && ids.length ? pathPoints([r.node, ...ids], nodes, edgeIdx, from) : [[r.x, r.y] as [number, number], ...pathPoints(ids, nodes, edgeIdx)];
          const pts = toPoints(line);
          const c = routeColor(r.status);
          const hot = sel("resource", r.id);
          return (
            <g key={"rt" + r.id} opacity={selection && !hot && selection.type === "resource" ? 0.25 : 1} pointerEvents="none">
              <polyline points={pts} fill="none" stroke={c} strokeOpacity={0.2} strokeWidth={lw(geo ? (hot ? 12 : 8) : hot ? 16 : 11)} strokeLinecap="round" strokeLinejoin="round" {...ve} />
              <polyline points={pts} fill="none" stroke={c} strokeWidth={lw(geo ? (hot ? 3.6 : 2.4) : hot ? 4 : 2.6)} strokeDasharray={dash(0.06, 0.06)} className="route-dash" strokeLinecap="round" strokeLinejoin="round" {...ve} />
            </g>
          );
        })}
      {/* shelters */}
      {layers.shelters &&
        frame.shelters.map((s) => {
          const full = s.occupancy + s.reserved >= s.capacity;
          const occ = s.capacity ? Math.min(1, s.occupancy / s.capacity) : 1;
          const res = s.capacity ? Math.min(1 - occ, s.reserved / s.capacity) : 0;
          const hot = sel("shelter", s.id);
          return (
            <g key={s.id} transform={`translate(${s.x} ${s.y})`} className={cx(hit, "cursor-pointer")} onClick={(ev) => clicked(ev) && onSelect?.({ type: "shelter", id: s.id })}>
              <g className={mk}>
                <title>{`${s.id} ${s.name ?? ""} · ${s.occupancy}/${s.capacity} occupied · ${s.reserved} reserved`}</title>
                {hot && <rect x={-0.27} y={-0.27} width={0.54} height={0.54} rx={0.1} fill="none" stroke="#0891b2" strokeWidth={sw(3)} />}
                <rect x={-0.2} y={-0.2} width={0.4} height={0.4} rx={0.07} fill={full ? "#fee2e2" : "#fff"} stroke={full ? "#dc2626" : "#0a0a0a"} strokeWidth={sw(2.6)} />
                <path d="M-0.1 0.06V-0.02L0 -0.1L0.1 -0.02V0.06Z" fill={full ? "#dc2626" : "#0a0a0a"} />
                <rect x={-0.24} y={0.26} width={0.48} height={0.06} rx={0.03} fill="#e4e4e0" />
                <rect x={-0.24} y={0.26} width={0.48 * occ} height={0.06} rx={0.03} fill={full ? "#dc2626" : "#0a0a0a"} />
                <rect x={-0.24 + 0.48 * occ} y={0.26} width={0.48 * res} height={0.06} fill="#22d3ee" />
                {!compact && (
                  <text x={0} y={0.48} textAnchor="middle" fontSize={0.13} fontFamily="JetBrains Mono" fill="#3f3f46" {...halo}>
                    {s.id} {s.occupancy}/{s.capacity}
                  </text>
                )}
              </g>
            </g>
          );
        })}
      {/* incidents */}
      {layers.incidents &&
        frame.incidents.map((i) => {
          const done = i.status === "RESCUED";
          const fa = i.status === "FALSE_ALARM";
          const uncertain = i.status === "UNVERIFIED" || i.status === "SUSPECTED";
          if (uncertain && !layers.uncertain) return null;
          const c = URGENCY_COLOR(i.urgency);
          const r = 0.1 + Math.min(0.12, i.people * 0.012);
          const hot = sel("incident", i.id) || highlight.includes(i.id);
          const open = !done && !fa;
          return (
            <g key={i.id} transform={`translate(${i.x} ${i.y})`} className={cx(hit, "cursor-pointer")} onClick={(ev) => clicked(ev) && onSelect?.({ type: "incident", id: i.id })} opacity={done || fa ? 0.55 : 1}>
              <g className={mk}>
                <title>{`${i.id} · ${i.status} · urgency ${i.urgency} · ${i.people} waiting${i.confidence !== undefined ? ` · confidence ${i.confidence}` : ""}`}</title>
                {open && i.urgency >= 5 && i.unassigned > 0 && <circle r={0.3} fill={c} className="pulse" />}
                {hot && <circle r={r + 0.12} fill="none" stroke="#0891b2" strokeWidth={sw(3)} />}
                {done ? (
                  <>
                    <circle r={0.09} fill="#15803d" />
                    <path d="M-0.04 0L-0.01 0.03L0.045 -0.03" stroke="#fff" strokeWidth={sw(2.4)} fill="none" strokeLinecap="round" />
                  </>
                ) : fa ? (
                  <>
                    <circle r={0.09} fill="#fff" stroke="#a1a1aa" strokeWidth={sw(2)} />
                    <path d="M-0.035 -0.035L0.035 0.035M0.035 -0.035L-0.035 0.035" stroke="#a1a1aa" strokeWidth={sw(2)} />
                  </>
                ) : (
                  <>
                    <rect x={-r} y={-r} width={2 * r} height={2 * r} transform="rotate(45)" rx={0.025} fill={i.unassigned > 0 ? c : "#fff"} stroke={c} strokeWidth={sw(uncertain ? 2.2 : 3)} strokeDasharray={uncertain ? "0.035 0.03" : undefined} fillOpacity={uncertain && i.unassigned > 0 ? 0.55 : 1} />
                    {!compact && (
                      <text y={0.035} textAnchor="middle" fontSize={0.11} fontWeight={600} fontFamily="JetBrains Mono" fill={i.unassigned > 0 ? "#fff" : c}>
                        {i.people}
                      </text>
                    )}
                  </>
                )}
                {!compact && open && (
                  <text x={r + 0.06} y={-r} fontSize={0.12} fontFamily="JetBrains Mono" fill="#52525b" {...halo}>
                    {i.id.replace("I-0", "I")}
                  </text>
                )}
              </g>
            </g>
          );
        })}
      {/* units */}
      {layers.units &&
        frame.resources.map((r) => {
          const hot = sel("resource", r.id);
          const dis = r.status === "DISABLED";
          const hold = r.status === "HOLDING";
          const black = r.blackout_until !== null && r.blackout_until !== undefined;
          return (
            <g
              key={r.id}
              className={cx("unit cursor-pointer", hit)}
              style={{ transform: `translate(${r.x}px, ${r.y}px)`, transitionDuration: `${transitionMs}ms` }}
              onClick={(ev) => clicked(ev) && onSelect?.({ type: "resource", id: r.id })}
            >
              <g className={mk}>
                <title>{`${r.id} · ${r.kind} · ${r.status}${r.load ? ` · ${r.load} aboard` : ""}${black ? " · COMMS DOWN" : ""}`}</title>
                {hot && <circle r={0.3} fill="#22d3ee" opacity={0.18} />}
                {hold && <circle r={0.26} fill="none" stroke="#dc2626" strokeWidth={sw(2)} className="pulse" />}
                {r.kind === "boat" ? (
                  <path d="M-0.17 -0.07H0.1L0.19 0L0.1 0.07H-0.17Z" fill={dis ? "#fff" : "#0a0a0a"} stroke={dis ? "#dc2626" : hot ? "#22d3ee" : "#fff"} strokeWidth={sw(2)} />
                ) : (
                  <rect x={-0.13} y={-0.09} width={0.26} height={0.18} rx={0.05} fill={dis ? "#fff" : "#27272a"} stroke={dis ? "#dc2626" : hot ? "#22d3ee" : "#fff"} strokeWidth={sw(2)} />
                )}
                <text y={0.04} x={r.kind === "boat" ? -0.02 : 0} textAnchor="middle" fontSize={0.1} fontWeight={600} fontFamily="JetBrains Mono" fill={dis ? "#dc2626" : "#fff"}>
                  {r.id}
                </text>
                {r.load > 0 && (
                  <g transform="translate(0.17 -0.14)">
                    <circle r={0.075} fill="#15803d" />
                    <text y={0.03} textAnchor="middle" fontSize={0.085} fontWeight={700} fill="#fff" fontFamily="JetBrains Mono">
                      {r.load}
                    </text>
                  </g>
                )}
                {black && (
                  <text x={0} y={-0.16} textAnchor="middle" fontSize={0.09} fontFamily="JetBrains Mono" fill="#b45309" {...halo}>
                    no comms
                  </text>
                )}
              </g>
            </g>
          );
        })}
    </>
  );
}

// ------------------------------------------------------------------ synthetic city: plain SVG with its own pan/zoom
function PlainMap(props: Props) {
  const { world, mode = "select", compact, className } = props;
  const bounds = useMemo(() => {
    const xs = world.nodes.map((n) => n.x);
    const ys = world.nodes.map((n) => n.y);
    return { x0: Math.min(...xs) - PAD, y0: Math.min(...ys) - PAD, w: Math.max(...xs) - Math.min(...xs) + 2 * PAD, h: Math.max(...ys) - Math.min(...ys) + 2 * PAD };
  }, [world]);
  const [view, setView] = useState({ k: 1, tx: 0, ty: 0 });
  const drag = useRef<{ x: number; y: number; tx: number; ty: number; moved: boolean } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const scaleOf = () => {
    const r = svgRef.current!.getBoundingClientRect();
    return Math.min(r.width / bounds.w, r.height / bounds.h);
  };
  const onWheel = (e: React.WheelEvent) => {
    const k2 = Math.max(1, Math.min(4, view.k * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    setView((v) => ({ ...v, k: k2 }));
  };
  const onDown = (e: React.PointerEvent) => {
    drag.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty, moved: false };
  };
  const onMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d || e.buttons !== 1) return;
    const sc = scaleOf() * view.k;
    const mx = (e.clientX - d.x) / sc;
    const my = (e.clientY - d.y) / sc;
    if (Math.abs(mx) + Math.abs(my) > 0.05) d.moved = true;
    setView((v) => ({ ...v, tx: d.tx + mx, ty: d.ty + my }));
  };
  const onUp = () => {
    setTimeout(() => (drag.current = null), 0);
  };
  const clicked = () => !drag.current?.moved;

  return (
    <div className={cx("relative h-full w-full overflow-hidden rounded-[inherit] bg-[#fbfbfa]", mode !== "select" && "cursor-crosshair", className)}>
      <svg
        ref={svgRef}
        viewBox={`${bounds.x0} ${bounds.y0} ${bounds.w} ${bounds.h}`}
        className="h-full w-full touch-none select-none"
        preserveAspectRatio="xMidYMid meet"
        onWheel={onWheel}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerLeave={onUp}
        role="img"
        aria-label="Synthetic map of Riverbend"
      >
        <defs>
          <pattern id="grid" width="0.5" height="0.5" patternUnits="userSpaceOnUse">
            <path d="M0.5 0H0V0.5" fill="none" stroke="#000" strokeOpacity="0.035" strokeWidth={0.01} />
          </pattern>
        </defs>
        <rect x={bounds.x0} y={bounds.y0} width={bounds.w} height={bounds.h} fill="url(#grid)" />
        <g transform={`translate(${bounds.x0 + bounds.w / 2} ${bounds.y0 + bounds.h / 2}) scale(${view.k}) translate(${-(bounds.x0 + bounds.w / 2) + view.tx} ${-(bounds.y0 + bounds.h / 2) + view.ty})`}>
          <Scene {...props} geo={false} clicked={clicked} />
        </g>
      </svg>
      {!compact && <ZoomControls onIn={() => setView((v) => ({ ...v, k: Math.min(4, v.k * 1.25) }))} onOut={() => setView((v) => ({ ...v, k: Math.max(1, v.k / 1.25) }))} onReset={() => setView({ k: 1, tx: 0, ty: 0 })} />}
    </div>
  );
}

function ZoomControls({ onIn, onOut, onReset, extra }: { onIn: () => void; onOut: () => void; onReset: () => void; extra?: React.ReactNode }) {
  return (
    <div className="no-print absolute right-3 bottom-7 z-[500] flex flex-col overflow-hidden rounded-xl border border-line-2 bg-white/90 shadow-sm backdrop-blur">
      {extra}
      <button aria-label="Zoom in" className={cx("p-1.5 hover:bg-wash", extra && "border-t border-line")} onClick={onIn}>
        <Plus className="size-4" />
      </button>
      <button aria-label="Zoom out" className="border-t border-line p-1.5 hover:bg-wash" onClick={onOut}>
        <Minus className="size-4" />
      </button>
      <button aria-label="Reset view" className="border-t border-line p-1.5 hover:bg-wash" onClick={onReset}>
        <Scan className="size-4" />
      </button>
    </div>
  );
}

// ------------------------------------------------------------------ real geography: Leaflet basemap + synced SVG overlay
const BASE_KEY = "resqnet.basemap";

function GeoMap(props: Props) {
  const { world, mode = "select", compact, className } = props;
  const proj = world.map.projection!;
  const host = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const tileRef = useRef<L.TileLayer | null>(null);
  const downRef = useRef<{ x: number; y: number } | null>(null);
  const [svgEl, setSvgEl] = useState<SVGSVGElement | null>(null);
  const [menu, setMenu] = useState(false);
  const [saved, setSaved] = useState<number | null>(null);
  useEffect(() => {
    if (!menu || typeof caches === "undefined") return;
    caches
      .open("resqnet-offline-v1")
      .then((c) => c.keys())
      .then((k) => setSaved(k.length))
      .catch(() => setSaved(null));
  }, [menu]);
  const [base, setBase] = useState<string>(() => {
    try {
      return localStorage.getItem(BASE_KEY) || "light";
    } catch {
      return "light";
    }
  });

  // km bounds covering every node and road shape (+ margin so labels and flood blobs are not clipped)
  const area = useMemo(() => {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const take = (x: number, y: number) => {
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    };
    world.nodes.forEach((n) => take(n.x, n.y));
    world.edges.forEach((e) => e.geometry?.forEach((p) => take(p[0], p[1])));
    return { x0, y0, x1, y1 };
  }, [world]);
  const key = world.map.id;

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const pad = 1.4;
    const sw = xyToLatLng(proj, area.x0 - pad, area.y1 + pad);
    const ne = xyToLatLng(proj, area.x1 + pad, area.y0 - pad);
    const overlayBounds = L.latLngBounds(sw, ne);
    const dataBounds = L.latLngBounds(xyToLatLng(proj, area.x0, area.y1), xyToLatLng(proj, area.x1, area.y0));

    const map = L.map(el, {
      zoomControl: false,
      attributionControl: false,
      zoomSnap: 0.25,
      zoomDelta: 0.5,
      wheelPxPerZoomLevel: 110,
      wheelDebounceTime: 30,
      maxZoom: 16,
      minZoom: 9,
      scrollWheelZoom: !compact,
      maxBoundsViscosity: 0.85,
    });
    L.control.attribution({ position: "bottomright", prefix: false }).addTo(map);
    map.fitBounds(dataBounds, { padding: [24, 24], animate: false });
    map.setMinZoom(Math.max(9, map.getZoom() - 1.5));
    map.setMaxBounds(overlayBounds.pad(0.2));
    mapRef.current = map;

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `${area.x0 - pad} ${area.y0 - pad} ${area.x1 - area.x0 + 2 * pad} ${area.y1 - area.y0 + 2 * pad}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("class", "geo-overlay");
    L.svgOverlay(svg, overlayBounds, { interactive: false, zIndex: 450 }).addTo(map);
    const ms = compact ? MARKER_SCALE * 0.62 : MARKER_SCALE;
    const setK = (z: number) => svg.style.setProperty("--k", String((ms * DESIGN_PX_PER_KM) / pxPerKm(z, proj.lat0)));
    setK(map.getZoom());
    map.on("zoomanim", (e) => setK((e as L.ZoomAnimEvent).zoom));
    map.on("zoom", () => setK(map.getZoom()));
    map.on("zoomend", () => setK(map.getZoom()));
    setSvgEl(svg);

    const ro = new ResizeObserver(() => map.invalidateSize({ animate: false }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      map.off();
      map.remove();
      mapRef.current = null;
      tileRef.current = null;
      setSvgEl(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, compact]);

  // basemap
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const b = BASE_LAYERS.find((x) => x.id === base) ?? BASE_LAYERS[0];
    tileRef.current?.remove();
    const t = L.tileLayer(b.url, { attribution: b.attribution, subdomains: b.subdomains ?? "abc", maxNativeZoom: b.maxZoom, maxZoom: 19, className: b.className, crossOrigin: "anonymous", updateWhenZooming: false, keepBuffer: 3 });
    t.addTo(map);
    t.setZIndex(1);
    tileRef.current = t;
  }, [base, svgEl]);

  const clicked = (e?: React.MouseEvent) => {
    const d = downRef.current;
    if (!d || !e) return true;
    return Math.hypot(e.clientX - d.x, e.clientY - d.y) < 6;
  };
  const fit = () => {
    const map = mapRef.current;
    if (!map) return;
    map.fitBounds(L.latLngBounds(xyToLatLng(proj, area.x0, area.y1), xyToLatLng(proj, area.x1, area.y0)), { padding: [24, 24] });
  };
  const focus = props.focus;
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !focus) return;
    if (focus.fit && !focus.points.length) {
      map.flyToBounds(L.latLngBounds(xyToLatLng(proj, area.x0, area.y1), xyToLatLng(proj, area.x1, area.y0)), { padding: [24, 24], duration: 1.0 });
      return;
    }
    const lls = focus.points.map(([x, y]) => L.latLng(...xyToLatLng(proj, x, y)));
    if (!lls.length) return;
    if (lls.length === 1) map.flyTo(lls[0], focus.zoom ?? Math.max(map.getZoom(), 14.5), { duration: 1.2 });
    else map.flyToBounds(L.latLngBounds(lls), { padding: [90, 90], maxZoom: focus.zoom ?? 15, duration: 1.2 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.key, svgEl]);

  return (
    <div
      className={cx("geo-map relative h-full w-full overflow-hidden rounded-[inherit]", mode !== "select" && "cursor-crosshair", className)}
      onPointerDownCapture={(e) => (downRef.current = { x: e.clientX, y: e.clientY })}
    >
      <div ref={host} className="absolute inset-0 z-0" role="application" aria-label={`Map of ${world.map.name}`} />
      {svgEl && createPortal(<Scene {...props} geo clicked={clicked} />, svgEl)}
      {!compact && (
        <>
          <ZoomControls
            onIn={() => mapRef.current?.zoomIn(0.5)}
            onOut={() => mapRef.current?.zoomOut(0.5)}
            onReset={fit}
            extra={
              <button aria-label="Basemap" className="p-1.5 hover:bg-wash" onClick={() => setMenu((m) => !m)}>
                <LayersIcon className="size-4" />
              </button>
            }
          />
          {menu && (
            <div className="panel fade-in no-print absolute right-14 bottom-7 z-[500] w-40 p-1.5">
              <div className="label px-1.5 pb-1">basemap</div>
              {BASE_LAYERS.map((b) => (
                <button
                  key={b.id}
                  className={cx("flex w-full items-center rounded-md px-2 py-1 text-left text-[12.5px]", base === b.id ? "bg-ink text-white" : "hover:bg-black/[0.05]")}
                  onClick={() => {
                    setBase(b.id);
                    try {
                      localStorage.setItem(BASE_KEY, b.id);
                    } catch {
                      /* ignore */
                    }
                    setMenu(false);
                  }}
                >
                  {b.label}
                </button>
              ))}
              {saved !== null && (
                <div className="mono border-t border-line px-1.5 pt-1.5 mt-1 text-[10px] leading-tight text-mute" title="Tiles you have viewed are kept so the map works without a connection">
                  {saved.toLocaleString()} tiles saved for offline
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function CityMap(props: Props) {
  const geo = !!props.world.map?.real && !!props.world.map.projection;
  return geo ? <GeoMap {...props} /> : <PlainMap {...props} />;
}

export function MapLegend() {
  const item = (el: React.ReactNode, label: string) => (
    <span className="inline-flex items-center gap-1.5">
      {el}
      {label}
    </span>
  );
  return (
    <div className="mono flex flex-wrap items-center gap-x-3.5 gap-y-1 text-[10.5px] text-mute">
      {item(<span className="inline-block size-2.5 rotate-45 rounded-[2px] bg-crit" />, "urgent")}
      {item(<span className="inline-block size-2.5 rotate-45 rounded-[2px] border border-dashed border-warn" />, "unverified")}
      {item(<span className="inline-block h-2 w-3.5 rounded-sm bg-ink" />, "boat / team")}
      {item(<span className="inline-block size-2.5 rounded-[3px] border-[1.5px] border-ink" />, "shelter")}
      {item(<span className="inline-block h-0.5 w-4 border-t-2 border-dashed border-info" />, "to pickup")}
      {item(<span className="inline-block h-0.5 w-4 border-t-2 border-dashed border-ok" />, "to shelter")}
      {item(<span className="inline-block h-0.5 w-4 border-t-2 border-dashed border-crit" />, "closed")}
      {item(<span className="inline-block h-0.5 w-4 border-t-2 border-dashed border-[#93c5dd]" />, "flooded")}
    </div>
  );
}
