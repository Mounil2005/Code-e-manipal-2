import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Plus, Rocket, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { useSim } from "../store/sim";
import { ChaosButtons } from "../components/Chaos";
import { SimControls } from "../components/Shell";
import { Badge, Button, cx, Empty, Field, inputCls, PageHeader, Panel, Segmented } from "../components/ui";
import type { ScenarioConfig, Strategy } from "../types";
import { STRATEGY_LABEL } from "../lib/format";

const SCHED_TYPES = ["TRIPLE_FAILURE", "CLOSE_EDGE", "RESOURCE_FAILURE", "SHELTER_CAPACITY", "FLOOD_SURGE", "COMM_BLACKOUT"];

export default function Scenarios() {
  const { sim, frame, world, createSim, inject } = useSim();
  const NB = (world?.neighbourhoods ?? []).map((n) => [n.id, n.name] as const);
  const bridges = (world?.edges ?? []).filter((e) => e.kind === "bridge").map((e) => e.id);
  const shelterIds = (world?.shelters ?? []).map((s) => s.id);
  const nav = useNavigate();
  const [presets, setPresets] = useState<ScenarioConfig[]>([]);
  const [base, setBase] = useState("triple_failure");
  const [cfg, setCfg] = useState<ScenarioConfig | null>(null);
  const [strategy, setStrategy] = useState<Strategy>("decentralized");
  const [launching, setLaunching] = useState(false);
  const [disruptions, setDisruptions] = useState<any[]>([]);
  const [manual, setManual] = useState({ type: "CLOSE_EDGE", target: "" });

  useEffect(() => {
    api.get<{ scenarios: ScenarioConfig[] }>("/api/scenarios").then((r) => {
      setPresets(r.scenarios);
      setCfg(structuredClone(r.scenarios.find((s) => s.key === base) ?? r.scenarios[0]));
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!sim) return;
    api.get<any>(`/api/simulations/${sim.id}/metrics`).then((r) => setDisruptions(r.disruptions)).catch(() => {});
  }, [sim?.id, frame?.last_event_id, frame?.tick]); // eslint-disable-line react-hooks/exhaustive-deps

  const pick = (k: string) => {
    setBase(k);
    const p = presets.find((s) => s.key === k);
    if (p) setCfg(structuredClone(p));
  };
  const set = <K extends keyof ScenarioConfig>(k: K, v: ScenarioConfig[K]) => setCfg((c) => (c ? { ...c, [k]: v } : c));

  const launch = async () => {
    if (!cfg) return;
    setLaunching(true);
    // weights are derived server-side from the chosen policy
    const { key, name, description, weights, ...rest } = cfg;
    void key;
    void description;
    void weights;
    try {
      await createSim(base, strategy, { ...rest, name });
      nav("/command");
    } catch {
      /* toast already shown */
    } finally {
      setLaunching(false);
    }
  };

  const targets: Record<string, string[]> = {
    CLOSE_EDGE: bridges,
    OPEN_EDGE: bridges,
    RESOURCE_FAILURE: frame?.resources.map((r) => r.id) ?? [],
    RESOURCE_RESTORE: frame?.resources.filter((r) => r.status === "DISABLED").map((r) => r.id) ?? [],
    COMM_BLACKOUT: frame?.resources.map((r) => r.id) ?? [],
    SHELTER_CAPACITY: shelterIds,
    CONTRADICTORY_REPORT: frame?.incidents.filter((i) => i.status !== "RESCUED").map((i) => i.id) ?? [],
  };
  const target = manual.target || targets[manual.type]?.[0] || "";
  const fireManual = () => {
    const t = manual.type;
    const p: Record<string, unknown> =
      t === "CLOSE_EDGE" || t === "OPEN_EDGE"
        ? { edge_id: target, reason: "chaos lab" }
        : t === "SHELTER_CAPACITY"
          ? { shelter_id: target, capacity: "occupancy", reason: "chaos lab overflow" }
          : t === "COMM_BLACKOUT"
            ? { resource_id: target, duration: 20 }
            : t === "CONTRADICTORY_REPORT"
              ? { incident_id: target, claim: "NO_VICTIMS", source: "resident" }
              : { resource_id: target, reason: "chaos lab" };
    inject(t, p, `${t.toLowerCase().replace(/_/g, " ")} ${target}`);
  };

  const paramKey = (t: string) => (t === "CLOSE_EDGE" ? "edge_id" : t === "SHELTER_CAPACITY" ? "shelter_id" : t === "FLOOD_SURGE" ? "delta" : "resource_id");

  return (
    <div className="space-y-5 p-5">
      <SimControls />
      <PageHeader eyebrow="chaos lab" title="Chaos Lab" sub="Design the storm on the left. Break the live run and watch it recover on the right." />

      <div className="grid items-start gap-5 xl:grid-cols-[1fr_420px]">
        {/* ---------------- scenario editor: every parameter visible ---------------- */}
        <Panel title="Design the storm" sub="Launching creates a new seeded run with exactly these settings" right={<Segmented size="sm" value={strategy} onChange={setStrategy} options={(["decentralized", "centralized", "greedy"] as Strategy[]).map((s) => ({ value: s, label: STRATEGY_LABEL[s] }))} />}>
          {!cfg ? (
            <Empty title="Loading presets…" />
          ) : (
            <div className="space-y-7 p-5">
              <section>
                <h3 className="mb-3 text-sm font-semibold">Start from a preset</h3>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  {presets.map((p) => (
                    <button key={p.key} onClick={() => pick(p.key)} className={cx("rounded-xl border p-3.5 text-left transition", base === p.key ? "border-ink bg-ink text-white" : "border-line bg-white hover:border-ink/30")}>
                      <div className="text-sm font-semibold">{p.name}</div>
                      <div className={cx("mt-1 line-clamp-3 text-xs leading-snug", base === p.key ? "text-white/70" : "text-mute")}>{p.description}</div>
                    </button>
                  ))}
                </div>
              </section>

              <section>
                <h3 className="mb-3 text-sm font-semibold">Flood</h3>
                <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-3">
                  <Slider label="Flood severity" v={cfg.flood_severity} min={0.4} max={2} step={0.05} on={(v) => set("flood_severity", v)} fmt={(v) => `${v.toFixed(2)}×`} />
                  <Slider label="Flood peak (minute)" v={cfg.flood_peak_tick} min={20} max={150} step={5} on={(v) => set("flood_peak_tick", v)} />
                  <Slider label="Duration (min)" v={cfg.duration} min={60} max={360} step={10} on={(v) => set("duration", v)} />
                </div>
              </section>

              <section>
                <h3 className="mb-3 text-sm font-semibold">Demand and fleet</h3>
                <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-3">
                  <Slider label="Incoming requests" v={cfg.arrival_incidents} min={0} max={40} step={1} on={(v) => set("arrival_incidents", v)} hint={`+${cfg.initial_incidents} at start`} />
                  <Slider label="Rescue boats" v={cfg.boats} min={0} max={7} step={1} on={(v) => set("boats", v)} />
                  <Slider label="Ground teams" v={cfg.ground_units} min={0} max={6} step={1} on={(v) => set("ground_units", v)} />
                  {cfg.shelter_capacity.map((c, i) => (
                    <Slider key={i} label={`Camp S${i + 1} capacity`} v={c} min={0} max={120} step={1} on={(v) => set("shelter_capacity", cfg.shelter_capacity.map((x, j) => (j === i ? v : x)))} />
                  ))}
                </div>
              </section>

              <section>
                <h3 className="mb-3 text-sm font-semibold">Information quality</h3>
                <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-3">
                  <Slider label="Report noise" v={cfg.report_noise} min={0} max={1} step={0.05} on={(v) => set("report_noise", v)} fmt={(v) => `${Math.round(v * 100)}%`} />
                  <Slider label="Duplicate reports" v={cfg.duplicate_rate} min={0} max={1} step={0.05} on={(v) => set("duplicate_rate", v)} fmt={(v) => `${Math.round(v * 100)}%`} />
                  <Slider label="Delayed reports" v={cfg.delayed_report_rate} min={0} max={1} step={0.05} on={(v) => set("delayed_report_rate", v)} fmt={(v) => `${Math.round(v * 100)}%`} />
                  <Slider label="False alarms" v={cfg.false_alarm_rate} min={0} max={0.5} step={0.02} on={(v) => set("false_alarm_rate", v)} fmt={(v) => `${Math.round(v * 100)}%`} />
                  <Slider label="Contradictory reports" v={cfg.contradiction_rate} min={0} max={0.6} step={0.02} on={(v) => set("contradiction_rate", v)} fmt={(v) => `${Math.round(v * 100)}%`} />
                  <Slider label="Communication delay" v={cfg.comm_delay} min={0} max={10} step={1} on={(v) => set("comm_delay", v)} fmt={(v) => `${v} min`} />
                </div>
              </section>

              <section>
                <h3 className="mb-3 text-sm font-semibold">Priorities</h3>
                <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-3">
                  <Field label="Priority policy">
                    <select className={inputCls} value={cfg.policy} onChange={(e) => set("policy", e.target.value)}>
                      {["balanced", "fastest", "urgency_first", "equity"].map((p) => (
                        <option key={p} value={p}>
                          {p.replace(/_/g, " ")}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Random seed" hint="same seed = same run">
                    <input className={inputCls} type="number" value={cfg.seed} onChange={(e) => set("seed", Number(e.target.value))} />
                  </Field>
                </div>
                <div className="label mt-4 mb-2">Neighbourhood priority (1.0 = neutral)</div>
                <div className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
                  {NB.map(([id, name]) => (
                    <Slider key={id} label={name} v={cfg.neighbourhood_priority[id] ?? 1} min={0.5} max={3} step={0.1} on={(v) => set("neighbourhood_priority", { ...cfg.neighbourhood_priority, [id]: v })} fmt={(v) => `${v.toFixed(1)}×`} />
                  ))}
                </div>
              </section>

              <section>
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold">Scheduled failures</h3>
                  <Button size="sm" icon={<Plus className="size-3.5" />} onClick={() => set("schedule", [...cfg.schedule, { tick: 30, type: "CLOSE_EDGE", params: { edge_id: "@critical", reason: "scheduled" } }])}>
                    Add failure
                  </Button>
                </div>
                {cfg.schedule.length === 0 ? (
                  <div className="rounded-lg border border-dashed border-line-2 p-3 text-sm text-mute">None scheduled. You can also inject failures live from the panel on the right.</div>
                ) : (
                  <div className="space-y-2">
                    {cfg.schedule.map((e, i) => (
                      <div key={i} className="flex flex-wrap items-center gap-2">
                        <span className="text-sm text-mute">At minute</span>
                        <input className={"h-9 rounded-lg border border-line-2 bg-white px-2.5 text-sm outline-none focus:border-info w-20"} type="number" value={e.tick} onChange={(ev) => set("schedule", cfg.schedule.map((x, j) => (j === i ? { ...x, tick: Number(ev.target.value) } : x)))} />
                        <select className={"h-9 rounded-lg border border-line-2 bg-white px-2.5 text-sm outline-none focus:border-info w-52"} value={e.type} onChange={(ev) => set("schedule", cfg.schedule.map((x, j) => (j === i ? { ...x, type: ev.target.value, params: defaultParams(ev.target.value) } : x)))}>
                          {SCHED_TYPES.map((t) => (
                            <option key={t} value={t}>
                              {t.toLowerCase().replace(/_/g, " ")}
                            </option>
                          ))}
                        </select>
                        {e.type !== "TRIPLE_FAILURE" && (
                          <input
                            className={"h-9 rounded-lg border border-line-2 bg-white px-2.5 text-sm outline-none focus:border-info w-40"}
                            aria-label="Target"
                            value={String((e.params as Record<string, unknown>)[paramKey(e.type)] ?? "")}
                            onChange={(ev) => set("schedule", cfg.schedule.map((x, j) => (j === i ? { ...x, params: { ...x.params, [paramKey(e.type)]: e.type === "FLOOD_SURGE" ? Number(ev.target.value) : ev.target.value } } : x)))}
                          />
                        )}
                        <button aria-label="Remove" className="text-faint hover:text-crit" onClick={() => set("schedule", cfg.schedule.filter((_, j) => j !== i))}>
                          <Trash2 className="size-4" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              <div className="flex flex-wrap items-center justify-end gap-3 border-t border-line pt-4">
                <span className="text-xs text-mute">{world?.map.real ? `Real roads of ${world.map.name}; floods and incidents simulated and reproducible.` : "Synthetic and reproducible."}</span>
                <Button variant="solid" icon={<Rocket className="size-4" />} loading={launching} onClick={launch}>
                  Launch scenario
                </Button>
              </div>
            </div>
          )}
        </Panel>

        {/* ---------------- live fault injection + recovery ---------------- */}
        <div className="space-y-5 xl:sticky xl:top-20">
          <Panel title="Break the live run" sub={sim ? `Into ${sim.scenario_name} at minute ${frame?.tick}` : "Start a run first"}>
            <div className="space-y-4 p-4">
              <ChaosButtons layout="grid" />
              <div className="rounded-xl border border-line bg-wash p-3">
                <div className="label mb-2">Target something specific</div>
                <div className="flex flex-wrap gap-1.5">
                  <select className={"h-9 rounded-lg border border-line-2 bg-white px-2.5 text-sm outline-none focus:border-info min-w-0 flex-1"} value={manual.type} onChange={(e) => setManual({ type: e.target.value, target: targets[e.target.value]?.[0] ?? "" })}>
                    {Object.keys(targets).map((t) => (
                      <option key={t} value={t}>
                        {t.toLowerCase().replace(/_/g, " ")}
                      </option>
                    ))}
                  </select>
                  <select className={"h-9 rounded-lg border border-line-2 bg-white px-2.5 text-sm outline-none focus:border-info w-24"} value={target} onChange={(e) => setManual({ ...manual, target: e.target.value })}>
                    {(targets[manual.type] ?? []).map((t) => (
                      <option key={t}>{t}</option>
                    ))}
                  </select>
                  <Button variant="solid" disabled={!target} onClick={fireManual}>
                    Inject
                  </Button>
                </div>
              </div>
            </div>
          </Panel>

          <Panel title="Recovery" sub="Minutes until every affected request is covered again">
            <div className="scroll-thin max-h-[360px] overflow-y-auto">
              {disruptions.length === 0 ? (
                <Empty title="Nothing broken yet" body="Inject a failure; affected requests and recovery time appear here." />
              ) : (
                <ul className="divide-y divide-line">
                  {[...disruptions].reverse().map((d) => (
                    <li key={d.event} className="px-4 py-3 text-sm">
                      <div className="flex items-center gap-2">
                        <span className="font-medium capitalize">{d.type.replace(/_/g, " ").toLowerCase()}</span>
                        <span className="text-xs text-mute">minute {d.tick}</span>
                        <span className="ml-auto">{d.recovery === null ? <Badge tone="crit" dot>recovering</Badge> : <Badge tone="ok">recovered in {d.recovery} min</Badge>}</span>
                      </div>
                      <div className="mt-1 text-xs text-mute">
                        {d.incidents.length ? `${d.incidents.length} requests affected` : "No requests affected"}
                        {d.holds.length ? ` · loaded units re-sheltered: ${d.holds.join(", ")}` : ""}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

function defaultParams(t: string): Record<string, unknown> {
  switch (t) {
    case "CLOSE_EDGE":
      return { edge_id: "@critical", reason: "scheduled" };
    case "RESOURCE_FAILURE":
      return { resource_id: "B1", reason: "scheduled" };
    case "SHELTER_CAPACITY":
      return { shelter_id: "S2", capacity: "occupancy" };
    case "FLOOD_SURGE":
      return { delta: 0.5 };
    case "COMM_BLACKOUT":
      return { resource_id: "B2", duration: 20 };
    default:
      return {};
  }
}

function Slider({ label, v, min, max, step, on, fmt, hint }: { label: string; v: number; min: number; max: number; step: number; on: (v: number) => void; fmt?: (v: number) => string; hint?: string }) {
  return (
    <Field label={label} hint={(fmt ? fmt(v) : String(v)) + (hint ? ` · ${hint}` : "")}>
      <input type="range" className="w-full accent-[#0a0a0a]" min={min} max={max} step={step} value={v} onChange={(e) => on(Number(e.target.value))} />
    </Field>
  );
}
