import { useEffect, useMemo, useState } from "react";
import { Crosshair, Layers as LayersIcon, MapPinPlus, MousePointer2, Scissors } from "lucide-react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useSim, type Layers } from "../store/sim";
import { CityMap, MapLegend } from "../components/CityMap";
import { RiverCard } from "../components/RiverCard";
import { ChaosButtons } from "../components/Chaos";
import { ActivityFeed } from "../components/Feed";
import { SelectionPanel } from "../components/SelectionPanel";
import { NewRunMenu, SimControls } from "../components/Shell";
import { api } from "../lib/api";
import { Badge, Button, cx, Empty, inputCls, Panel, Segmented, Spinner, Stat } from "../components/ui";
import { fmt, INCIDENT_TONE, isOpen, pct, statusLabel, URGENCY_TONE } from "../lib/format";

export default function Command() {
  const { world, frame, sim, layers, selection, setSelection, toggleLayer, inject, audit, focus } = useSim();
  const [observed, setObserved] = useState<{ runs: number[][]; row_km: number } | null>(null);
  useEffect(() => {
    if (!layers.satellite || !world?.map.real) return;
    api.get<{ s1: { runs: number[][]; row_km: number } | null }>(`/api/evidence?map=${encodeURIComponent(world.map.id)}`).then((d) => setObserved(d.s1)).catch(() => setObserved(null));
  }, [layers.satellite, world?.map.id]);
  const [mode, setMode] = useState<"select" | "close-edge" | "new-incident">("select");
  const [filter, setFilter] = useState<"open" | "urgent" | "uncovered" | "all">("open");
  const [tab, setTab] = useState<"detail" | "feed">("feed");
  const [sos, setSos] = useState({ people: 4, urgency: 4 });
  const [showLayers, setShowLayers] = useState(false);

  const incidents = useMemo(() => {
    if (!frame) return [];
    let xs = frame.incidents;
    if (filter === "open") xs = xs.filter(isOpen);
    if (filter === "urgent") xs = xs.filter((i) => isOpen(i) && i.urgency >= 4);
    if (filter === "uncovered") xs = xs.filter((i) => isOpen(i) && i.unassigned > 0);
    return [...xs].sort((a, b) => b.urgency - a.urgency || a.deadline - b.deadline);
  }, [frame, filter]);

  if (!world || !frame || !sim) return <Spinner label="Connecting to the simulation engine…" />;
  const m = frame.metrics;

  return (
    <div className="flex flex-col gap-3 p-3 lg:h-[calc(100vh-88px)] lg:min-h-[720px]">
      <SimControls
        extra={
          <>
            <ChaosButtons />
            <NewRunMenu />
          </>
        }
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[300px_1fr_360px]">
        {/* incident queue */}
        <Panel
          title="incident queue"
          sub={`${m.open_incidents} open · ${frame.incidents.filter((i) => isOpen(i) && i.unassigned > 0).length} uncovered`}
          right={<Badge tone="neutral">{incidents.length}</Badge>}
          bodyClass="flex flex-col"
          className="max-lg:h-80"
        >
          <div className="border-b border-line px-3 py-2">
            <Segmented size="sm" value={filter} onChange={setFilter} options={[{ value: "open", label: "Open" }, { value: "urgent", label: "Urgent" }, { value: "uncovered", label: "Uncovered" }, { value: "all", label: "All" }]} />
          </div>
          <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
            {incidents.length === 0 ? (
              <Empty title="Nothing in this filter" />
            ) : (
              incidents.map((i) => {
                const late = isOpen(i) && i.first_pickup_tick === null && frame.tick > i.deadline;
                return (
                  <button
                    key={i.id}
                    onClick={() => {
                      setSelection({ type: "incident", id: i.id });
                      setTab("detail");
                    }}
                    className={cx("flex w-full items-start gap-2.5 border-b border-line/70 px-3 py-2 text-left hover:bg-wash", selection?.type === "incident" && selection.id === i.id && "bg-info-soft/40")}
                  >
                    <span className="mono mt-0.5 grid size-6 shrink-0 place-items-center rounded-md text-[11px] font-semibold text-white" style={{ background: i.urgency >= 5 ? "#dc2626" : i.urgency >= 4 ? "#d97706" : i.urgency >= 3 ? "#0891b2" : "#a1a1aa" }}>
                      {i.urgency}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="mono text-[12.5px] font-semibold">{i.id}</span>
                        <Badge tone={INCIDENT_TONE[i.status]}>{statusLabel(i.status)}</Badge>
                        {late && <Badge tone="crit">late</Badge>}
                      </div>
                      <div className="mt-0.5 truncate text-xs text-mute">
                        {i.people} waiting{i.unassigned ? ` · ${i.unassigned} uncovered` : ""} · {i.node} · conf {fmt(i.confidence, 2)}
                      </div>
                      {i.assigned.length > 0 && <div className="mono mt-0.5 text-[10.5px] text-info">→ {i.assigned.join(", ")}</div>}
                    </div>
                    <span className="mono shrink-0 text-[10.5px] text-faint">t{i.deadline}</span>
                  </button>
                );
              })
            )}
          </div>
        </Panel>

        {/* map */}
        <section className="panel relative min-h-[420px] overflow-hidden">
          <div className="absolute top-3 left-3 z-10 flex flex-wrap items-center gap-1.5">
            <div className="inline-flex rounded-full border border-line-2 bg-white/95 p-0.5 shadow-sm backdrop-blur">
              {(
                [
                  ["select", <MousePointer2 key="a" className="size-3.5" />, "Inspect"],
                  ["close-edge", <Scissors key="b" className="size-3.5" />, "Close road"],
                  ["new-incident", <MapPinPlus key="c" className="size-3.5" />, "Add SOS"],
                ] as const
              ).map(([k, icon, l]) => (
                <button key={k} onClick={() => setMode(k)} className={cx("flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium", mode === k ? "bg-ink text-white" : "text-ink-2 hover:bg-black/5")}>
                  {icon}
                  {l}
                </button>
              ))}
            </div>
            <Button size="sm" icon={<LayersIcon className="size-3.5" />} onClick={() => setShowLayers((v) => !v)} className="bg-white/95">
              Layers
            </Button>
            {mode === "new-incident" && (
              <div className="flex items-center gap-1 rounded-full border border-line-2 bg-white/95 px-2 py-0.5 text-xs shadow-sm">
                <Crosshair className="size-3.5 text-info" />
                people
                <input className={cx(inputCls, "h-6 w-12 px-1 text-xs")} type="number" min={1} max={40} value={sos.people} onChange={(e) => setSos({ ...sos, people: Number(e.target.value) })} />
                urgency
                <input className={cx(inputCls, "h-6 w-10 px-1 text-xs")} type="number" min={1} max={5} value={sos.urgency} onChange={(e) => setSos({ ...sos, urgency: Number(e.target.value) })} />
              </div>
            )}
          </div>
          {showLayers && (
            <div className="panel fade-in absolute top-12 left-3 z-10 grid w-56 grid-cols-2 gap-1 p-2">
              {(Object.keys(layers) as (keyof Layers)[]).map((k) => (
                <label key={k} className="flex items-center gap-1.5 rounded px-1.5 py-1 text-xs capitalize hover:bg-wash">
                  <input type="checkbox" checked={layers[k]} onChange={() => toggleLayer(k)} />
                  {k}
                </label>
              ))}
            </div>
          )}
          <RiverCard />
          <CityMap
            world={world}
            frame={frame}
            focus={focus}
            observed={layers.satellite ? observed : null}
            layers={layers}
            selection={selection}
            mode={mode}
            transitionMs={Math.max(80, 1000 / (sim.speed || 1))}
            onSelect={(s) => {
              setSelection(s);
              setTab("detail");
            }}
            onEdgeClick={(id) => {
              const st = frame.edges[id];
              inject(st?.[2] ? "OPEN_EDGE" : "CLOSE_EDGE", { edge_id: id, reason: "operator closure" }, st?.[2] ? `${id} reopened` : `${id} closed`);
            }}
            onNodeClick={(node) => inject("NEW_INCIDENT", { node, people: sos.people, urgency: sos.urgency, source: "responder" }, `SOS at ${node}`)}
          />
          <div className="absolute bottom-3 left-3 z-10 rounded-xl border border-line bg-white/90 px-3 py-1.5 backdrop-blur">
            <MapLegend />
          </div>
        </section>

        {/* right column */}
        <Panel
          className="max-lg:h-[520px]"
          title={<Segmented size="sm" value={tab} onChange={setTab} options={[{ value: "feed", label: "Agent activity" }, { value: "detail", label: "Inspector" }]} />}
          right={<span className="mono text-[10.5px] text-faint">{audit.length} records</span>}
          bodyClass="flex flex-col"
        >
          <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">{tab === "feed" ? <ActivityFeed /> : <SelectionPanel />}</div>
        </Panel>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-[repeat(8,1fr)_1.6fr]">
        <Kpi label="people rescued" value={m.people_rescued} hint={`${m.people_in_transit} in transit`} tone="ok" />
        <Kpi label="still waiting" value={m.unmet_people} hint={`${m.open_incidents} open incidents`} tone={m.unmet_people > 20 ? "warn" : undefined} />
        <Kpi label="mean time to service" value={fmt(m.mean_time_to_service)} unit="min" hint={`median ${fmt(m.median_time_to_service)}`} />
        <Kpi label="deadline misses" value={m.deadline_misses} hint={`of ${m.urgent_incidents} urgent`} tone={m.deadline_misses ? "crit" : undefined} />
        <Kpi label="utilisation" value={pct(m.utilisation)} hint={`${m.idle_with_demand_ticks} idle-w/-demand`} />
        <Kpi label="fairness (Jain)" value={fmt(m.fairness_jain, 2)} hint="1.0 = even waits" tone="info" />
        <Kpi label="reroutes / reassign" value={`${m.reroutes}/${m.reassignments}`} hint={`${m.cancellations} cancellations`} />
        <Kpi label="safety" value={m.route_violations + m.invariant_violations === 0 ? "0 viol." : m.route_violations + m.invariant_violations} hint={`${m.safety_rejections} rejected · ${m.blocked_moves} blocked`} tone={m.route_violations + m.invariant_violations ? "crit" : "ok"} />
        <MiniChart />
      </div>
    </div>
  );
}

function Kpi(props: { label: string; value: React.ReactNode; unit?: string; hint?: string; tone?: "crit" | "warn" | "ok" | "info" }) {
  return (
    <div className="panel px-3.5 py-3">
      <Stat {...props} />
    </div>
  );
}

function MiniChart() {
  const frame = useSim((s) => s.frame);
  const [series, setSeries] = useState<{ tick: number; rescued: number; waiting: number }[]>([]);
  useEffect(() => {
    if (!frame) return;
    setSeries((prev) => {
      const base = prev.length && frame.tick < prev[prev.length - 1].tick ? [] : prev.filter((p) => p.tick !== frame.tick);
      return [...base, { tick: frame.tick, rescued: frame.metrics.people_rescued, waiting: frame.metrics.unmet_people }].slice(-400);
    });
  }, [frame]);
  return (
    <div className="panel col-span-2 px-3 pt-2 md:col-span-4 xl:col-span-1">
      <div className="label">rescued vs waiting</div>
      <div className="h-[52px]">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={series} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
            <XAxis dataKey="tick" hide />
            <YAxis hide />
            <Tooltip contentStyle={{ fontSize: 11, borderRadius: 8 }} labelFormatter={(t) => `t=${t}`} />
            <Area type="monotone" dataKey="waiting" stroke="#d97706" fill="#fef3c7" strokeWidth={1.5} isAnimationActive={false} />
            <Area type="monotone" dataKey="rescued" stroke="#15803d" fill="#dcfce7" strokeWidth={1.5} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
