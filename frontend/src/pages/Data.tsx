import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Check, Satellite } from "lucide-react";
import { api } from "../lib/api";
import { useSim } from "../store/sim";
import { CityMap } from "../components/CityMap";
import { Badge, Button, cx, Panel } from "../components/ui";

/** Data & evidence: which datasets, how and how often we get them, where they are used, why, and
 * what they changed; the Sentinel-1 check of the flood model; and the policy Pareto front. */

interface Dataset { key: string; name: string; provider: string; what: string; access: string; interval: string; used_for: string; why: string; benefit: string; limits: string }
interface Feedback { asked: string; answer: string; where: string }
interface Evidence {
  datasets: Dataset[];
  feedback: Feedback[];
  s1: null | {
    source: string; method: string; post_time: string; flooded_km2: number; row_km: number; runs: number[][];
    validation: { discharge_m3s: number; nodes: number; observed_wet: number; model_wet: number; confusion: { precision: number; recall: number; f1: number }; auc_inverse_elevation: number; auc_model_depth: number; base_rate: number; radius_m: number };
  };
  model_frame: any;
}
interface ParetoPoint { name: string; weights: Record<string, number>; mean_time_to_service: number; fairness_jain: number; unmet_people: number; deadline_misses: number; pareto: boolean }

const f2 = (v?: number | null) => (v === null || v === undefined || Number.isNaN(v) ? "—" : v.toFixed(2));

export default function Data() {
  const { world, mapId } = useSim();
  const [ev, setEv] = useState<Evidence | null>(null);
  const [open, setOpen] = useState<string | null>("cwc");
  const [job, setJob] = useState<any>(null);
  const poll = useRef<ReturnType<typeof setInterval>>();

  useEffect(() => {
    api.get<Evidence>(`/api/evidence?map=${encodeURIComponent(mapId)}`).then(setEv).catch(() => setEv(null));
  }, [mapId]);
  useEffect(() => () => clearInterval(poll.current), []);

  const runPareto = async () => {
    const j = await api.post<any>("/api/pareto", { scenario: "kerala_2018", map: mapId, seeds: [1, 2, 3] });
    setJob(j);
    clearInterval(poll.current);
    poll.current = setInterval(async () => {
      const r = await api.get<any>(`/api/pareto/${j.id}`);
      setJob(r);
      if (r.status !== "running") clearInterval(poll.current);
    }, 1000);
  };
  const points: ParetoPoint[] = job?.result?.points ?? [];
  const front = points.filter((p) => p.pareto).sort((a, b) => a.mean_time_to_service - b.mean_time_to_service);
  const balanced = points.find((p) => p.name === "balanced");

  return (
    <div className="space-y-4 p-4">
      <section className="rounded-2xl bg-slate-950 p-6 text-white">
        <p className="mono text-xs uppercase tracking-[0.25em] text-cyan-300">data &amp; evidence</p>
        <h1 className="my-2 text-3xl font-semibold tracking-tight">What data we use, why, and what it changed</h1>
        <p className="max-w-4xl text-slate-300">
          Each source below says how we get it, how often, where in the product it is used, why we chose it and what difference it made, with its limits. The flood model is checked against what a satellite actually saw.
        </p>
      </section>

      {/* how data flows */}
      <Panel title="How data reaches a decision">
        <div className="flex flex-wrap items-center gap-2 p-4 text-[13px]">
          {[
            ["Sources", "CWC gauges (hourly) · 2018 record · synthetic flood · SOS / field reports"],
            ["One feed door", "POST /feed: timestamped, source-labelled messages"],
            ["Engine events", "river level, new SOS, duplicates, contradictions, closures, camp updates"],
            ["Agents re-negotiate", "bids priced on time, risk, urgency, fairness; safety shield checks each commit"],
            ["What you see", "priority board, re-planned routes, camps, supplies, decision records"],
          ].map(([t, d], i, all) => (
            <div key={t} className="flex items-center gap-2">
              <div className="rounded-xl border border-line bg-wash px-3 py-2">
                <div className="font-semibold">{t}</div>
                <div className="text-[11.5px] text-mute">{d}</div>
              </div>
              {i < all.length - 1 && <ArrowRight className="size-4 text-faint" />}
            </div>
          ))}
        </div>
      </Panel>

      {/* judge feedback */}
      {ev && (
        <Panel title="Questions from the last review, and where they are answered">
          <ul className="divide-y divide-line">
            {ev.feedback.map((f) => (
              <li key={f.asked} className="grid gap-1 px-4 py-2.5 text-sm md:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)_auto] md:gap-4">
                <span className="flex gap-2 font-medium">
                  <Check className="mt-0.5 size-4 shrink-0 text-ok" />
                  {f.asked}
                </span>
                <span className="text-ink-2">{f.answer}</span>
                <Link to={f.where} className="mono text-[12px] text-info underline">
                  {f.where}
                </Link>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {/* datasets */}
      {ev && (
        <Panel title="Datasets" sub="Click a source for how, how often, where, why and what it changed">
          <div className="divide-y divide-line">
            {ev.datasets.map((d) => (
              <div key={d.key} id={d.key}>
                <button className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-wash" onClick={() => setOpen(open === d.key ? null : d.key)}>
                  <span className="min-w-0 flex-1">
                    <span className="font-semibold">{d.name}</span>
                    <span className="ml-2 text-[12.5px] text-mute">{d.provider}</span>
                  </span>
                  <Badge tone="info">{d.interval}</Badge>
                </button>
                {open === d.key && (
                  <dl className="grid gap-x-6 gap-y-2 px-4 pb-4 text-[13.5px] md:grid-cols-2">
                    {(
                      [
                        ["What", d.what],
                        ["How we get it", d.access],
                        ["Where it is used", d.used_for],
                        ["Why this source", d.why],
                        ["What it changed", d.benefit],
                        ["Limits", d.limits],
                      ] as [string, string][]
                    ).map(([k, v]) => (
                      <div key={k}>
                        <dt className="label">{k}</dt>
                        <dd className="mt-0.5 leading-relaxed text-ink-2">{v}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </div>
            ))}
          </div>
        </Panel>
      )}

      {/* satellite check */}
      <div id="s1" className="grid gap-4 lg:grid-cols-[1.5fr_1fr]">
        <Panel title="Satellite check · Sentinel-1, 21 Aug 2018" right={<Satellite className="size-4 text-mute" />} bodyClass="p-0">
          {ev?.s1 && world?.map.real && ev.model_frame ? (
            <div className="relative h-[520px] overflow-hidden rounded-b-[14px]">
              <CityMap world={world} frame={ev.model_frame} observed={{ runs: ev.s1.runs, row_km: ev.s1.row_km }} layers={{ routes: false, units: false, incidents: false }} />
              <div className="absolute top-3 left-3 z-10 space-y-1 rounded-xl border border-line bg-white/90 px-3 py-2 text-[12px] backdrop-blur">
                <div className="flex items-center gap-2">
                  <span className="inline-block size-3 rounded-sm bg-[#c026d3] opacity-70" /> water the satellite saw (21 Aug)
                </div>
                <div className="flex items-center gap-2">
                  <span className="inline-block size-3 rounded-full bg-[#3b9ddb] opacity-60" /> flood the model predicts at that river level
                </div>
              </div>
            </div>
          ) : (
            <p className="p-4 text-sm text-mute">{world?.map.real ? "Loading…" : "Open a Kerala map (New run → Map) to see the satellite check."}</p>
          )}
        </Panel>
        <Panel title="How it was checked">
          {ev?.s1 ? (
            <div className="space-y-3 p-4 text-[13.5px] leading-relaxed text-ink-2">
              <p>
                <b>Data.</b> {ev.s1.source}. {ev.s1.method}. Observed new water: <b>{ev.s1.flooded_km2} km²</b>.
              </p>
              <p>
                <b>Model.</b> The simulator's water depths at the GloFAS discharge for the pass time ({Math.round(ev.s1.validation.discharge_m3s)} m³/s).
              </p>
              <p>
                <b>Result</b> at {ev.s1.validation.radius_m} m around each junction ({ev.s1.validation.observed_wet} of {ev.s1.validation.nodes} observed wet):
              </p>
              <div className="grid grid-cols-4 gap-2 text-center">
                {(
                  [
                    ["ranking AUC", f2(ev.s1.validation.auc_inverse_elevation)],
                    ["precision", f2(ev.s1.validation.confusion.precision)],
                    ["recall", f2(ev.s1.validation.confusion.recall)],
                    ["F1", f2(ev.s1.validation.confusion.f1)],
                  ] as [string, string][]
                ).map(([k, v]) => (
                  <div key={k} className="rounded-lg bg-wash p-2">
                    <div className="mono text-lg font-semibold">{v}</div>
                    <div className="text-[10.5px] text-mute">{k}</div>
                  </div>
                ))}
              </div>
              <p className="text-[12.5px] text-mute">
                AUC 0.5 is chance, 1.0 perfect. The first terrain model scored about 0.5; this check is why the map now uses the 30 m DEM. Limits: the pass was 5 days after the peak, radar misses water under trees and between buildings, and the sample is small. Full method: docs/flood_validation.md.
              </p>
            </div>
          ) : (
            <p className="p-4 text-sm text-mute">No satellite snapshot for this map.</p>
          )}
        </Panel>
      </div>

      {/* pareto */}
      <Panel title="Why this dispatch policy · Pareto front" sub="Every policy run on the same Kerala 2018 flood and seeds; a policy is on the front if no other is faster, fairer and leaves fewer people waiting at once" right={<Button size="sm" variant="solid" loading={job?.status === "running"} onClick={runPareto}>{job ? "Run again" : "Run the policy sweep (~20 s)"}</Button>}>
        <div id="pareto" className="grid gap-4 p-4 lg:grid-cols-[1.4fr_1fr]">
          {points.length ? (
            <>
              <ParetoChart points={points} />
              <div className="text-[13px]">
                <div className="label mb-1.5">on the front</div>
                <table className="w-full">
                  <thead className="text-left text-[11px] text-mute">
                    <tr>
                      <th className="py-1">policy</th>
                      <th>time</th>
                      <th>fair</th>
                      <th>unmet</th>
                    </tr>
                  </thead>
                  <tbody className="mono text-[12px]">
                    {front.map((p) => (
                      <tr key={p.name} className={cx(p.name === "balanced" && "font-semibold")}>
                        <td className="py-0.5">{p.name}</td>
                        <td>{p.mean_time_to_service.toFixed(1)}</td>
                        <td>{p.fairness_jain.toFixed(3)}</td>
                        <td>{p.unmet_people.toFixed(1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="mt-3 leading-relaxed text-ink-2">
                  {balanced?.pareto ? "The default balanced policy is on the front: no tested policy beats it on all three goals at once." : "The default balanced policy is dominated here, which is worth reporting honestly."} Points that tie mean the weights rarely change a choice in this flood, because most incidents have only one unit that can reach them; the weights matter when units compete.
                </p>
              </div>
            </>
          ) : (
            <p className="text-sm text-mute">{job?.status === "running" ? `Running… ${Math.round((job.progress ?? 0) * 100)}%` : "Press the button to sweep 13 policies over 3 seeds on the 2018 flood."}</p>
          )}
        </div>
      </Panel>
    </div>
  );
}

/** Time to service (x, lower better) vs fairness (y, higher better); size = people not delivered. */
function ParetoChart({ points }: { points: ParetoPoint[] }) {
  const W = 620;
  const H = 330;
  const m = { l: 56, r: 16, t: 14, b: 44 };
  const xs = points.map((p) => p.mean_time_to_service);
  const ys = points.map((p) => p.fairness_jain);
  const us = points.map((p) => p.unmet_people);
  const pad = (lo: number, hi: number, f: number) => [lo - (hi - lo || 1) * f, hi + (hi - lo || 1) * f];
  const [x0, x1] = pad(Math.min(...xs), Math.max(...xs), 0.12);
  const [y0, y1] = pad(Math.min(...ys), Math.max(...ys), 0.12);
  const X = (v: number) => m.l + ((v - x0) / (x1 - x0)) * (W - m.l - m.r);
  const Y = (v: number) => H - m.b - ((v - y0) / (y1 - y0)) * (H - m.t - m.b);
  const R = (u: number) => 5 + 9 * ((u - Math.min(...us)) / Math.max(1e-9, Math.max(...us) - Math.min(...us)));
  const ticks = (a: number, b: number, n = 5) => Array.from({ length: n }, (_, i) => a + ((b - a) * i) / (n - 1));
  const front = points.filter((p) => p.pareto).sort((a, b) => a.mean_time_to_service - b.mean_time_to_service);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Pareto front of dispatch policies">
      {ticks(x0, x1).map((t) => (
        <g key={"x" + t}>
          <line x1={X(t)} x2={X(t)} y1={m.t} y2={H - m.b} stroke="#efefec" />
          <text x={X(t)} y={H - m.b + 16} textAnchor="middle" fontSize={10} fill="#71717a">{t.toFixed(1)}</text>
        </g>
      ))}
      {ticks(y0, y1).map((t) => (
        <g key={"y" + t}>
          <line x1={m.l} x2={W - m.r} y1={Y(t)} y2={Y(t)} stroke="#efefec" />
          <text x={m.l - 8} y={Y(t) + 3} textAnchor="end" fontSize={10} fill="#71717a">{t.toFixed(3)}</text>
        </g>
      ))}
      <text x={(W + m.l) / 2} y={H - 6} textAnchor="middle" fontSize={11} fill="#3f3f46">mean time to service, min (lower is better)</text>
      <text x={14} y={(H - m.b) / 2} textAnchor="middle" fontSize={11} fill="#3f3f46" transform={`rotate(-90 14 ${(H - m.b) / 2})`}>fairness (higher is better)</text>
      {front.length > 1 && <polyline points={front.map((p) => `${X(p.mean_time_to_service)},${Y(p.fairness_jain)}`).join(" ")} fill="none" stroke="#0891b2" strokeDasharray="4 4" strokeOpacity={0.6} />}
      {points.filter((p) => !p.pareto).map((p) => (
        <circle key={p.name} cx={X(p.mean_time_to_service)} cy={Y(p.fairness_jain)} r={R(p.unmet_people)} fill="#a1a1aa" fillOpacity={0.55}>
          <title>{`${p.name}: ${p.mean_time_to_service.toFixed(2)} min, fairness ${p.fairness_jain.toFixed(3)}, ${p.unmet_people.toFixed(1)} not delivered`}</title>
        </circle>
      ))}
      {front.map((p) => (
        <circle key={p.name} cx={X(p.mean_time_to_service)} cy={Y(p.fairness_jain)} r={R(p.unmet_people)} fill="#0891b2" fillOpacity={0.85} stroke={p.name === "balanced" ? "#0a0a0a" : "#fff"} strokeWidth={p.name === "balanced" ? 2.5 : 1}>
          <title>{`${p.name}: ${p.mean_time_to_service.toFixed(2)} min, fairness ${p.fairness_jain.toFixed(3)}, ${p.unmet_people.toFixed(1)} not delivered`}</title>
        </circle>
      ))}
      {points.filter((p) => p.name === "balanced").map((p) => (
        <text key="lbl" x={X(p.mean_time_to_service) + R(p.unmet_people) + 5} y={Y(p.fairness_jain) + 4} fontSize={11} fontWeight={600} fill="#0a0a0a">balanced (default)</text>
      ))}
      <g transform={`translate(${W - m.r - 190} ${H - m.b - 52})`} fontSize={10.5} fill="#3f3f46">
        <circle cx={6} cy={6} r={5} fill="#0891b2" /><text x={16} y={10}>on the Pareto front</text>
        <circle cx={6} cy={22} r={5} fill="#a1a1aa" /><text x={16} y={26}>dominated</text>
        <text x={0} y={42} fill="#71717a">bigger = more people not delivered</text>
      </g>
    </svg>
  );
}
