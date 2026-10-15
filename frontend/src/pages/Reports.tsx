import { useEffect, useMemo, useState } from "react";
import { Download, FileJson, Printer, RefreshCw } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, apiUrl } from "../lib/api";
import { useSim } from "../store/sim";
import { Logo } from "../components/Shell";
import { Badge, Button, cx, Empty, PageHeader, Panel, Segmented, Spinner, Stat } from "../components/ui";
import { fmt, pct, STRATEGY_LABEL } from "../lib/format";

export default function Reports() {
  const { sim, frame, openDecision, attach } = useSim();
  const [rep, setRep] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [runs, setRuns] = useState<any[]>([]);
  const [outFilter, setOutFilter] = useState<"all" | "unmet" | "missed">("all");

  const load = async () => {
    if (!sim) return;
    setLoading(true);
    setErr(null);
    try {
      setRep(await api.get<any>(`/api/reports/${sim.id}`));
      setRuns((await api.get<any>("/api/simulations")).simulations);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    load();
  }, [sim?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const outcomes = useMemo(() => {
    if (!rep) return [];
    return rep.outcomes.filter((o: any) => (outFilter === "unmet" ? o.status !== "RESCUED" && o.status !== "FALSE_ALARM" : outFilter === "missed" ? o.missed_deadline : true));
  }, [rep, outFilter]);

  const downloadJson = () => {
    const blob = new Blob([JSON.stringify(rep, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `resqnet-report-${sim?.id}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  if (!sim) return <Spinner />;
  return (
    <div className="space-y-3 p-3">
      <div className="no-print">
        <PageHeader
          eyebrow="after-action report"
          title="What happened, what it cost, what to change"
          sub="Generated only from this run's events and metrics. The strategy comparison replays the same seed and the same injected events under each strategy."
          right={
            <>
              <Button size="sm" icon={<RefreshCw className="size-3.5" />} loading={loading} onClick={load}>
                Regenerate at t={frame?.tick}
              </Button>
              <Button size="sm" icon={<FileJson className="size-3.5" />} disabled={!rep} onClick={downloadJson}>
                JSON
              </Button>
              <a href={apiUrl(`/api/reports/${sim.id}?format=pdf`)} className="inline-flex h-7 items-center gap-1.5 rounded-full bg-ink px-2.5 text-xs font-medium text-white hover:bg-ink-2">
                <Download className="size-3.5" />
                PDF
              </a>
              <Button size="sm" variant="ghost" icon={<Printer className="size-3.5" />} onClick={() => window.print()}>
                Print
              </Button>
            </>
          }
        />
      </div>
      {err && <div className="panel p-4 text-sm text-crit">{err}</div>}
      {!rep ? (
        loading ? <Spinner label="Compiling report (re-running comparison strategies)…" /> : <Empty title="No report" />
      ) : (
        <article className="space-y-3">
          <div className="panel p-6">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <Logo />
                <h2 className="mt-4 font-display text-[26px] font-semibold tracking-tight">{rep.title}</h2>
                <p className="mt-1 max-w-2xl text-sm text-mute">{rep.scenario.description}</p>
              </div>
              <div className="mono text-right text-xs leading-6 text-mute">
                <div>run {rep.simulation_id}</div>
                <div>
                  seed {rep.scenario.seed} · {STRATEGY_LABEL[rep.scenario.strategy]} · {rep.scenario.policy}
                </div>
                <div>
                  t={rep.scenario.ticks_run}/{rep.scenario.duration} · comms delay {rep.scenario.comm_delay}
                </div>
              </div>
            </div>
            <div className="mt-2 rounded-lg bg-warn-soft/60 px-3 py-2 text-xs text-warn">{rep.disclaimer}</div>
            <div className="mt-5 grid grid-cols-2 gap-5 md:grid-cols-4 xl:grid-cols-8">
              <Stat label="rescued" value={rep.metrics.people_rescued} tone="ok" hint={`of ${rep.metrics.people_total_known} known`} />
              <Stat label="not delivered" value={rep.metrics.unmet_people} tone={rep.metrics.unmet_people ? "warn" : undefined} />
              <Stat label="mean tts" value={fmt(rep.metrics.mean_time_to_service)} unit="min" hint={`p90 ${fmt(rep.metrics.p90_time_to_service)}`} />
              <Stat label="deadline misses" value={rep.metrics.deadline_misses} tone={rep.metrics.deadline_misses ? "crit" : undefined} />
              <Stat label="utilisation" value={pct(rep.metrics.utilisation)} />
              <Stat label="fairness" value={fmt(rep.metrics.fairness_jain, 3)} tone="info" />
              <Stat label="reassignments" value={rep.metrics.reassignments} hint={`${rep.metrics.reroutes} reroutes`} />
              <Stat label="safety" value={rep.safety.route_violations + rep.safety.invariant_violations} tone={rep.safety.route_violations + rep.safety.invariant_violations ? "crit" : "ok"} hint="violations" />
            </div>
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <Panel title="lessons from this run">
              <ul className="space-y-2 p-4 text-sm">
                {rep.lessons.map((l: string, i: number) => (
                  <li key={i} className="flex gap-2">
                    <span className="mono text-faint">{String(i + 1).padStart(2, "0")}</span>
                    {l}
                  </li>
                ))}
              </ul>
            </Panel>
            <Panel title="fairness by neighbourhood" sub="mean wait (served: time-to-service; unserved: current wait)">
              <div className="h-56 p-2">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={rep.metrics.fairness_by_neighbourhood} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                    <CartesianGrid vertical={false} stroke="#efefec" />
                    <XAxis dataKey="name" tick={{ fontSize: 11 }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                    <Tooltip contentStyle={{ fontSize: 11, borderRadius: 8 }} />
                    <Bar dataKey="mean_wait" fill="#0a0a0a" radius={[4, 4, 0, 0]} isAnimationActive={false} />
                    <Bar dataKey="deadline_misses" fill="#dc2626" radius={[4, 4, 0, 0]} isAnimationActive={false} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Panel>
          </div>

          {rep.strategy_comparison.length > 0 && (
            <Panel title="strategy comparison · same seed, same injected events">
              <table className="w-full text-sm">
                <thead className="label">
                  <tr className="border-b border-line">
                    {["strategy", "mean tts", "misses", "unmet", "idle w/ demand", "fairness", "reassign", "messages", "violations"].map((h) => (
                      <th key={h} className="px-4 py-2 text-left font-normal">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rep.strategy_comparison.map((c: any) => (
                    <tr key={c.strategy} className={cx("mono border-b border-line/60", c.strategy === rep.scenario.strategy && "bg-info-soft/30")}>
                      <td className="px-4 py-1.5 font-sans">{STRATEGY_LABEL[c.strategy]}{c.strategy === rep.scenario.strategy && <Badge tone="info" className="ml-2">this run</Badge>}</td>
                      <td className="px-4">{fmt(c.mean_time_to_service)}</td>
                      <td className="px-4">{c.deadline_misses}</td>
                      <td className="px-4">{c.unmet_people}</td>
                      <td className="px-4">{c.idle_with_demand_ticks}</td>
                      <td className="px-4">{fmt(c.fairness_jain, 3)}</td>
                      <td className="px-4">{c.reassignments}</td>
                      <td className="px-4">{c.negotiation_messages}</td>
                      <td className="px-4">{c.route_violations}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="px-4 py-2 text-xs text-mute">A single seed is anecdotal — use the Strategy Arena for multi-seed means and variability.</p>
            </Panel>
          )}

          <div className="grid gap-3 lg:grid-cols-2">
            <Panel title="critical timeline">
              <div className="scroll-thin max-h-[420px] overflow-y-auto">
                {rep.critical_timeline.map((e: any, i: number) => (
                  <div key={i} className="flex gap-3 border-b border-line/60 px-4 py-1.5 text-[12.5px]">
                    <span className="mono w-12 shrink-0 text-faint">t={e.tick}</span>
                    <span className={e.severity === "ok" ? "text-ok" : "text-ink-2"}>{e.message}</span>
                  </div>
                ))}
              </div>
            </Panel>
            <Panel title="disruptions & recovery">
              <div className="divide-y divide-line">
                {rep.disruptions.length === 0 ? (
                  <Empty title="No disruptions in this run" />
                ) : (
                  rep.disruptions.map((d: any) => (
                    <div key={d.event} className="flex items-center gap-2 px-4 py-2 text-xs">
                      <span className="mono">{d.event}</span>
                      <Badge tone="warn">{d.type.toLowerCase().replace(/_/g, " ")}</Badge>
                      <span className="mono text-mute">t={d.tick} · {d.incidents.length} requests affected</span>
                      <span className="ml-auto">{d.recovery === null ? <Badge tone="crit">unresolved</Badge> : <Badge tone="ok">{d.recovery} min</Badge>}</span>
                    </div>
                  ))
                )}
              </div>
              <div className="border-t border-line p-4">
                <div className="label mb-2">key decisions</div>
                {rep.key_decisions.length === 0 ? (
                  <div className="text-xs text-mute">No safety rejections or post-disruption commitments.</div>
                ) : (
                  rep.key_decisions.slice(0, 10).map((d: any) => (
                    <button key={d.id} onClick={() => openDecision(d.id)} className="block w-full border-b border-line/50 py-1.5 text-left text-xs hover:text-info">
                      <span className="mono text-mute">{d.id} t={d.tick} {d.incident}</span> — {d.explanation.slice(0, 160)}…
                    </button>
                  ))
                )}
              </div>
            </Panel>
          </div>

          <Panel title="rescue outcomes" right={<Segmented size="sm" value={outFilter} onChange={setOutFilter} options={[{ value: "all", label: "All" }, { value: "unmet", label: "Not rescued" }, { value: "missed", label: "Missed deadline" }]} />}>
            <div className="scroll-thin max-h-[420px] overflow-auto">
              <table className="w-full text-[12.5px]">
                <thead className="label sticky top-0 bg-white">
                  <tr className="border-b border-line">
                    {["incident", "neighbourhood", "urgency", "status", "reported", "first pickup", "deadline", "rescued", "waiting"].map((h) => (
                      <th key={h} className="px-3 py-2 text-left font-normal">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {outcomes.map((o: any) => (
                    <tr key={o.id} className="mono border-b border-line/60">
                      <td className="px-3 py-1">{o.id}</td>
                      <td className="px-3 font-sans">{o.neighbourhood}</td>
                      <td className="px-3">{o.urgency}</td>
                      <td className="px-3">
                        <Badge tone={o.status === "RESCUED" ? "ok" : o.status === "FALSE_ALARM" ? "neutral" : "warn"}>{o.status.toLowerCase().replace("_", " ")}</Badge>
                      </td>
                      <td className="px-3">{o.reported}</td>
                      <td className="px-3">{o.first_pickup ?? "—"}</td>
                      <td className={cx("px-3", o.missed_deadline && "text-crit")}>{o.deadline}</td>
                      <td className="px-3">{o.rescued}</td>
                      <td className="px-3">{o.waiting}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          <div className="grid gap-3 lg:grid-cols-2">
            <Panel title="resource utilisation">
              <table className="w-full text-[12.5px]">
                <tbody>
                  {rep.resources.map((r: any) => (
                    <tr key={r.id} className="mono border-b border-line/60">
                      <td className="px-4 py-1.5">{r.id}</td>
                      <td className="px-2 font-sans text-mute">{r.name}</td>
                      <td className="px-2">{r.missions} done</td>
                      <td className="px-2 text-crit">{r.cancelled ? `${r.cancelled} cancelled` : ""}</td>
                      <td className="px-2">{r.distance_km} km</td>
                      <td className="px-4 text-right">{r.busy_ticks + r.idle_ticks ? Math.round((100 * r.busy_ticks) / (r.busy_ticks + r.idle_ticks)) : 0}% busy</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
            <Panel title="safety constraint results & limitations">
              <div className="space-y-1 p-4 text-sm">
                <div>Route violations: <b className="mono">{rep.safety.route_violations}</b> · Invariant violations: <b className="mono">{rep.safety.invariant_violations}</b></div>
                <div>Commits rejected: <b className="mono">{rep.safety.commit_rejections}</b> · Movements blocked: <b className="mono">{rep.safety.movement_blocks}</b></div>
                <ul className="mt-3 space-y-1 text-xs text-mute">
                  {rep.limitations.map((l: string) => (
                    <li key={l}>• {l}</li>
                  ))}
                </ul>
              </div>
            </Panel>
          </div>

          <Panel title="compare runs" sub="Every run is stored event-sourced and can be reopened" className="no-print">
            <div className="scroll-thin max-h-[300px] overflow-auto">
              <table className="w-full text-[12.5px]">
                <thead className="label">
                  <tr className="border-b border-line">
                    {["run", "scenario", "strategy", "tick", "status", "rescued", "unmet", "mean tts", "fairness", ""].map((h) => (
                      <th key={h} className="px-3 py-2 text-left font-normal">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {runs.map((r) => (
                    <tr key={r.id} className={cx("mono border-b border-line/60", r.id === sim.id && "bg-info-soft/30")}>
                      <td className="px-3 py-1">{r.id}</td>
                      <td className="px-3">{r.scenario}</td>
                      <td className="px-3">{STRATEGY_LABEL[r.strategy]}</td>
                      <td className="px-3">{r.tick}</td>
                      <td className="px-3">{r.status}</td>
                      <td className="px-3">{r.metrics?.people_rescued ?? "—"}</td>
                      <td className="px-3">{r.metrics?.unmet_people ?? "—"}</td>
                      <td className="px-3">{fmt(r.metrics?.mean_time_to_service)}</td>
                      <td className="px-3">{fmt(r.metrics?.fairness_jain, 3)}</td>
                      <td className="px-3">
                        {r.id !== sim.id && (
                          <button className="text-info hover:underline" onClick={() => attach(r.id)}>
                            open
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="px-3 py-2 text-[11px] text-mute">Final metrics are stored when a run finishes; in-progress runs show “—”.</p>
            </div>
          </Panel>
        </article>
      )}
    </div>
  );
}
