import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Bomb, Check, UserCheck, CloudRain, Droplets, Gauge, MapPin, Radio, Route, Siren, Square, Tent, Waves, Zap } from "lucide-react";
import { api } from "../lib/api";
import { useSim } from "../store/sim";
import { CityMap, MapLegend } from "../components/CityMap";
import { Badge, Button, cx, Field, inputCls, Panel, Segmented } from "../components/ui";
import { URGENCY_COLOR } from "../lib/format";
import type { FeedDelta, FeedMessage, Frame, LiveCwc, PriorityRow, Recommendation } from "../types";

/** Live operations: the product running on a real-time feed. River readings come from CWC (live),
 * the real 2018 CWC gauge record (drill) or a synthetic flood; SOS calls and field reports arrive
 * through the same feed. Everything shown is the engine's own state and reasoning. */

const SOURCE_TONE: Record<string, string> = { live: "ok", drill: "warn", synthetic: "info", external: "ink" };
const SOURCE_LABEL: Record<string, string> = { live: "live · CWC", drill: "drill · 2018 CWC readings", synthetic: "synthetic", external: "external sender" };
const TYPE_ICON: Record<string, React.ReactNode> = {
  gauge: <Gauge className="size-3.5" />,
  sos: <Siren className="size-3.5" />,
  duplicate: <Radio className="size-3.5" />,
  contradiction: <Zap className="size-3.5" />,
  road: <Route className="size-3.5" />,
  shelter: <Tent className="size-3.5" />,
  dam: <Waves className="size-3.5" />,
};
const fmtT = (t?: string | null) => {
  if (!t) return "";
  if (t.startsWith("+")) return t;
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? t : d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
};

function Spark({ values, w = 140, h = 30, color = "#0891b2" }: { values: number[]; w?: number; h?: number; color?: string }) {
  if (values.length < 2) return null;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const x = (i: number) => (i / (values.length - 1)) * w;
  const y = (v: number) => h - 3 - ((v - lo) / Math.max(1e-9, hi - lo)) * (h - 6);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join("");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="block" style={{ width: w, height: h }} aria-hidden>
      <path d={`${d}L${w} ${h}L0 ${h}Z`} fill={color} opacity={0.12} />
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} />
      <circle cx={x(values.length - 1)} cy={y(values[values.length - 1])} r={2.6} fill="#0a0a0a" />
    </svg>
  );
}

export default function Live() {
  const { world, frame, sim, createSim, inject, focus, feed, feedStatus, openDecision, setSelection, selection, setFocus, toast, mapId } = useSim();
  const [cwc, setCwc] = useState<LiveCwc | null>(null);
  const [board, setBoard] = useState<PriorityRow[]>([]);
  const [mode, setMode] = useState<"drill" | "synthetic">("drill");
  const [p, setP] = useState({ interval_s: 3, sos_per_hour: 1.5, duplicate_rate: 0.2, contradiction_rate: 0.05, road_report_rate: 0.04, shelter_update_rate: 0.02, rise_hours: 24, hold_hours: 8, fall_hours: 24, peak: 0.9 });
  const [speed, setSpeed] = useState(4);
  const [busy, setBusy] = useState<string | null>(null);
  const [placing, setPlacing] = useState(false);
  const [deltaSeq, setDeltaSeq] = useState<number | null>(null);
  const onLive = sim?.scenario === "live_ops";
  const real = !!world?.map.real;

  // real CWC readings for the map's gauge and dams (the backend caches; CWC updates hourly)
  useEffect(() => {
    if (!real) return;
    let alive = true;
    const load = () => api.get<LiveCwc>(`/api/live/cwc?map=${encodeURIComponent(mapId)}`).then((d) => alive && setCwc(d)).catch(() => alive && setCwc({ available: false, reason: "backend unreachable" }));
    load();
    const t = setInterval(load, 60000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [mapId, real]);

  // the engine's own ranking, refreshed every 2 s
  useEffect(() => {
    if (!sim || !onLive) return;
    let alive = true;
    const load = () => api.get<{ board: PriorityRow[] }>(`/api/simulations/${sim.id}/priority`).then((r) => alive && setBoard(r.board)).catch(() => {});
    load();
    const t = setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [sim?.id, onLive]);

  const ensureLiveSim = async (fresh: boolean) => {
    const st = useSim.getState();
    if (fresh || st.sim?.scenario !== "live_ops" || st.frame?.finished) await createSim("live_ops", "decentralized", { seed: 7 });
    return useSim.getState().sim!;
  };
  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    try {
      await fn();
    } catch (e) {
      toast({ kind: "crit", title: `${label} failed`, body: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };
  const goLive = () =>
    run("Go live", async () => {
      const s = await ensureLiveSim(false);
      await api.post(`/api/simulations/${s.id}/feed/start`, { mode: "live", params: { poll_s: 60 }, speed: 1 });
      toast({ kind: "ok", title: "Live: CWC telemetry", body: "River readings now come from the real CWC gauge; SOS calls only arrive if someone sends them." });
    });
  const startFeed = () =>
    run("Start flood feed", async () => {
      const s = await ensureLiveSim(feed.length > 0);
      await api.post(`/api/simulations/${s.id}/feed/start`, { mode, params: p, speed });
      setFocus({ points: [], fit: true });
    });
  const stopFeed = () => run("Stop", async () => void (sim && (await api.post(`/api/simulations/${sim.id}/feed/stop`, {}))));
  const sendSos = (node: string) =>
    sim &&
    api
      .post(`/api/simulations/${sim.id}/feed`, { type: "sos", source: "external", node, people: 4, urgency: 4, reporter: "resident" })
      .then(() => {
        toast({ kind: "info", title: `SOS received at ${node}`, body: "Sent through the live feed like any external system would." });
        setPlacing(false);
      })
      .catch((e) => toast({ kind: "crit", title: "SOS rejected", body: (e as Error).message }));

  const gaugeMsgs = useMemo(() => feed.filter((m) => m.type === "gauge"), [feed]);
  const shownDelta: { msg: FeedMessage | null; delta: FeedDelta | null } = useMemo(() => {
    if (deltaSeq !== null) {
      const m = gaugeMsgs.find((x) => x.seq === deltaSeq) ?? null;
      return { msg: m, delta: m?.delta ?? null };
    }
    const last = gaugeMsgs[gaugeMsgs.length - 1] ?? null;
    return { msg: last, delta: feedStatus?.current_delta ?? null };
  }, [deltaSeq, gaugeMsgs, feedStatus]);

  const status = feedStatus;
  const srcNow = status?.mode === "live" ? "live" : status?.mode ?? null;
  const m = frame?.metrics;

  return (
    <div className="space-y-3 p-4">
      {/* header */}
      <section className="flex flex-wrap items-center gap-3 rounded-2xl bg-slate-950 px-5 py-4 text-white">
        <div className="min-w-0 flex-1">
          <p className="mono text-[11px] uppercase tracking-[0.22em] text-cyan-300">live operations · {world?.map.name ?? ""}</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">Real-time feed in, prioritised rescue out</h1>
          <p className="mt-1 max-w-4xl text-sm text-slate-300">
            River gauge readings, SOS calls and field reports arrive through one feed. The agents re-negotiate after every message; the board shows who is helped first and why.
          </p>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          {onLive && frame && (
            <div className="rounded-full bg-white/10 p-0.5">
              <Segmented
                size="sm"
                value={frame.approval_mode ? "operator" : "auto"}
                onChange={(v) => inject("SET_APPROVAL_MODE", { enabled: v === "operator" }, v === "operator" ? "Operator approves every dispatch" : "Autonomous dispatch")}
                options={[
                  { value: "operator", label: "AI recommends · you decide" },
                  { value: "auto", label: "Autonomous" },
                ]}
              />
            </div>
          )}
          {srcNow ? (
            <Badge tone={SOURCE_TONE[srcNow]} dot>
              {status?.running ? "feeding" : "stopped"} · {SOURCE_LABEL[srcNow]}
            </Badge>
          ) : (
            <Badge tone="neutral">feed idle</Badge>
          )}
          {status?.note && <span className="mono text-[11px] text-slate-400">{status.note}</span>}
        </div>
      </section>

      {!real && <p className="rounded-xl border border-warn/25 bg-warn-soft p-3 text-sm text-warn">Live data needs a real Kerala map. Choose Aluva or Chengannur under New run.</p>}

      {/* controls */}
      <div className="grid gap-3 lg:grid-cols-[1.1fr_1.6fr_0.9fr]">
        <Panel title="River right now · CWC telemetry" right={cwc?.available ? <Badge tone={cwc.stale ? "warn" : "ok"} dot>{cwc.stale ? "stale" : "real data"}</Badge> : <Badge tone="neutral">offline</Badge>}>
          <div className="space-y-2 p-3 text-sm">
            {cwc?.available && cwc.gauge ? (
              <>
                <div className="flex items-end justify-between gap-3">
                  <div>
                    <div className="label">
                      {cwc.gauge.name} · {cwc.gauge.river}
                    </div>
                    <div className="mono text-2xl font-semibold">{cwc.gauge.level_m?.toFixed(2)} m</div>
                    <div className="mono text-[11px] text-mute">reading {fmtT(cwc.gauge.time)} · hourly</div>
                  </div>
                  <Spark values={cwc.gauge.history.map((x) => x[1])} />
                </div>
                {cwc.dams?.map((d) => (
                  <div key={d.code} className="flex items-center justify-between rounded-lg bg-wash px-2.5 py-1.5">
                    <span className="flex items-center gap-1.5">
                      <Droplets className="size-3.5 text-info" />
                      {d.name}
                    </span>
                    <span className="mono text-[12px]">
                      outflow <b>{d.outflow_m3s?.toFixed(0) ?? "—"}</b> m³/s
                    </span>
                  </div>
                ))}
                <Button variant="solid" className="w-full" icon={<Radio className="size-4" />} loading={busy === "Go live"} disabled={!!busy} onClick={goLive}>
                  Go live on this river
                </Button>
              </>
            ) : (
              <p className="text-mute">{cwc ? `CWC not available: ${cwc.reason ?? "unknown"}. The drill and synthetic feeds still work.` : "Checking CWC…"}</p>
            )}
          </div>
        </Panel>

        <Panel title="Flood feed" sub="Streams readings and field traffic into the live pipeline">
          <div className="space-y-3 p-3">
            <Segmented
              size="sm"
              value={mode}
              onChange={setMode}
              options={[
                { value: "drill", label: "2018 drill · real CWC gauge readings" },
                { value: "synthetic", label: "Synthetic flood · your parameters" },
              ]}
            />
            <div className="grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-4">
              <Field label="seconds per hourly reading" hint={`${p.interval_s}s`}>
                <input type="range" min={1} max={10} step={0.5} value={p.interval_s} className="w-full accent-[#0a0a0a]" onChange={(e) => setP({ ...p, interval_s: Number(e.target.value) })} />
              </Field>
              <Field label="SOS calls / hour at peak" hint={`${p.sos_per_hour}`}>
                <input type="range" min={0} max={8} step={0.5} value={p.sos_per_hour} className="w-full accent-[#0a0a0a]" onChange={(e) => setP({ ...p, sos_per_hour: Number(e.target.value) })} />
              </Field>
              <Field label="duplicate reports" hint={`${Math.round(p.duplicate_rate * 100)}%`}>
                <input type="range" min={0} max={0.8} step={0.05} value={p.duplicate_rate} className="w-full accent-[#0a0a0a]" onChange={(e) => setP({ ...p, duplicate_rate: Number(e.target.value) })} />
              </Field>
              <Field label="contradictory reports" hint={`${Math.round(p.contradiction_rate * 100)}%`}>
                <input type="range" min={0} max={0.5} step={0.05} value={p.contradiction_rate} className="w-full accent-[#0a0a0a]" onChange={(e) => setP({ ...p, contradiction_rate: Number(e.target.value) })} />
              </Field>
              {mode === "synthetic" && (
                <>
                  <Field label="peak (share of 2018)" hint={`${Math.round(p.peak * 100)}%`}>
                    <input type="range" min={0.2} max={1.2} step={0.05} value={p.peak} className="w-full accent-[#0a0a0a]" onChange={(e) => setP({ ...p, peak: Number(e.target.value) })} />
                  </Field>
                  <Field label="rise (hours)" hint={`${p.rise_hours} h`}>
                    <input type="range" min={3} max={48} step={1} value={p.rise_hours} className="w-full accent-[#0a0a0a]" onChange={(e) => setP({ ...p, rise_hours: Number(e.target.value) })} />
                  </Field>
                  <Field label="at peak (hours)" hint={`${p.hold_hours} h`}>
                    <input type="range" min={0} max={24} step={1} value={p.hold_hours} className="w-full accent-[#0a0a0a]" onChange={(e) => setP({ ...p, hold_hours: Number(e.target.value) })} />
                  </Field>
                  <Field label="road reports" hint={`${Math.round(p.road_report_rate * 100)}%/h`}>
                    <input type="range" min={0} max={0.4} step={0.02} value={p.road_report_rate} className="w-full accent-[#0a0a0a]" onChange={(e) => setP({ ...p, road_report_rate: Number(e.target.value) })} />
                  </Field>
                </>
              )}
              <Field label="simulation speed" hint={`${Math.round(p.interval_s * speed)} sim-min / reading`}>
                <select className={inputCls} value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
                  {[1, 2, 4, 8].map((v) => (
                    <option key={v} value={v}>
                      {v}×
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <div className="flex flex-wrap gap-2">
              {status?.running && status.mode !== "live" ? (
                <Button variant="danger" icon={<Square className="size-4" />} loading={busy === "Stop"} onClick={stopFeed}>
                  Stop feed
                </Button>
              ) : (
                <Button variant="solid" icon={<CloudRain className="size-4" />} loading={busy === "Start flood feed"} disabled={!!busy || !real} onClick={startFeed}>
                  Start flood feed
                </Button>
              )}
              <span className="self-center text-xs text-mute">{mode === "drill" ? "The real hourly gauge record from 14 Aug 2018; SOS and field reports are synthetic and labelled." : "A flood curve from your parameters; every message is labelled synthetic."}</span>
            </div>
          </div>
        </Panel>

        <Panel title="Stress it">
          <div className="space-y-2 p-3">
            <Button className="w-full" variant="danger" icon={<Bomb className="size-4" />} disabled={!onLive || !frame || frame.finished} onClick={() => inject("TRIPLE_FAILURE", {}, "Triple failure")}>
              Bridge + boat + camp fail
            </Button>
            <Button className="w-full" icon={<MapPin className="size-4" />} disabled={!onLive} onClick={() => setPlacing((x) => !x)} variant={placing ? "info" : "outline"}>
              {placing ? "Click a junction on the map…" : "Send an SOS (external)"}
            </Button>
            <p className="text-[11.5px] leading-snug text-mute">
              Any system can post to the same feed: <span className="mono">POST /api/simulations/{"{id}"}/feed</span> with <span className="mono">{`{"type":"sos","node":"N12","people":4}`}</span>.
            </p>
          </div>
        </Panel>
      </div>

      {onLive && frame && (
        <ApprovalQueue
          frame={frame}
          inject={inject}
          openDecision={openDecision}
          onFocus={(id) => {
            const i = frame.incidents.find((x) => x.id === id);
            if (i) setFocus({ points: [[i.x, i.y]], zoom: 15 });
          }}
        />
      )}

      {/* board | map | feed */}
      {world && frame && (
        <div className="grid gap-3 xl:grid-cols-[360px_1fr_380px]">
          <PriorityBoard rows={board} recs={frame.recommendations ?? []} onPick={(r) => {
            setSelection({ type: "incident", id: r.incident });
            const i = frame.incidents.find((x) => x.id === r.incident);
            if (i) setFocus({ points: [[i.x, i.y]], zoom: 15 });
          }} onWhy={(id) => openDecision(id)} />
          <div className="panel relative h-[72vh] min-h-[420px] overflow-hidden">
            <CityMap world={world} frame={frame} focus={focus} selection={selection} onSelect={setSelection} mode={placing ? "new-incident" : "select"} onNodeClick={sendSos} transitionMs={Math.max(80, 1000 / (sim?.speed || 1))} />
            <GaugeCard msgs={gaugeMsgs} />
            <div className="absolute bottom-3 left-3 z-10 rounded-xl border border-line bg-white/90 px-3 py-1.5 backdrop-blur">
              <MapLegend />
            </div>
          </div>
          <div className="flex h-[72vh] min-h-[420px] flex-col gap-3">
            <ChangesCard msg={shownDelta.msg} delta={shownDelta.delta} history={gaugeMsgs.filter((x) => x.delta)} selected={deltaSeq} onSelect={setDeltaSeq} onWhy={openDecision} />
            <FeedStream feed={feed} />
          </div>
        </div>
      )}

      {m && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-7">
          {(
            [
              ["People rescued", m.people_rescued, "ok"],
              ["Still waiting", m.unmet_people, m.unmet_people > 20 ? "warn" : undefined],
              ["Mean time to service", `${m.mean_time_to_service ?? "—"} min`, undefined],
              ["Deadline misses", m.deadline_misses, m.deadline_misses ? "crit" : undefined],
              ["Fairness (Jain)", m.fairness_jain ?? "—", undefined],
              ["Safety violations", m.route_violations + m.invariant_violations, m.route_violations + m.invariant_violations ? "crit" : "ok"],
              ["Operator decisions", frame?.operator ? `${frame.operator.approvals} approved · ${frame.operator.overrides} changed · ${frame.operator.rejections} rejected` : "—", undefined],
            ] as [string, React.ReactNode, string | undefined][]
          ).map(([label, value, tone]) => (
            <div key={label} className="panel p-3">
              <div className="label">{label}</div>
              <div className={cx("mt-1 text-xl font-semibold tracking-tight", tone === "ok" && "text-ok", tone === "warn" && "text-warn", tone === "crit" && "text-crit")}>{value}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ river gauge on the map
function GaugeCard({ msgs }: { msgs: FeedMessage[] }) {
  const last = msgs[msgs.length - 1];
  if (!last) return null;
  const lv = msgs.slice(-48).map((x) => Number(x.payload.level_m));
  return (
    <div className="absolute top-3 right-3 z-10 rounded-xl border border-line-2 bg-white/90 px-3 py-2 text-right shadow-sm backdrop-blur">
      <div className="label">{String(last.payload.station ?? "river gauge")}</div>
      <div className="mono text-lg font-semibold">{Number(last.payload.level_m).toFixed(2)} m</div>
      <div className="mono text-[10.5px] text-mute">{fmtT(last.time)}</div>
      <div className="mt-1 flex justify-end">
        <Spark values={lv} w={130} h={28} />
      </div>
      <Badge tone={SOURCE_TONE[last.source]} className="mt-1">
        {SOURCE_LABEL[last.source]}
      </Badge>
    </div>
  );
}

// ------------------------------------------------------------------ priority board
function PriorityBoard({ rows, recs, onPick, onWhy }: { rows: PriorityRow[]; recs: Recommendation[]; onPick: (r: PriorityRow) => void; onWhy: (decisionId: string) => void }) {
  const recOf: Record<string, Recommendation> = Object.fromEntries(recs.map((r) => [r.incident, r]));
  const prev = useRef<Record<string, number>>({});
  const moves = useMemo(() => {
    const out: Record<string, number> = {};
    for (const r of rows) {
      const before = prev.current[r.incident];
      if (before !== undefined && before !== r.rank) out[r.incident] = before - r.rank;
    }
    return out;
  }, [rows]);
  useEffect(() => {
    prev.current = Object.fromEntries(rows.map((r) => [r.incident, r.rank]));
  }, [rows]);
  const waiting = rows.filter((r) => r.state !== "assigned").length;
  return (
    <Panel title="Priority board" sub={`${rows.length} open · ${waiting} waiting · ranked by the engine (urgency → deadline → confidence)`} className="h-[72vh] min-h-[420px]" bodyClass="flex min-h-0 flex-col">
      <ol className="scroll-thin min-h-0 flex-1 space-y-1.5 overflow-y-auto p-2" aria-label="Incidents by priority">
        {rows.length === 0 && <li className="p-4 text-sm text-mute">No open incidents yet. Start the flood feed or send an SOS.</li>}
        {rows.map((r) => {
          const mv = moves[r.incident];
          return (
            <li key={r.incident} className={cx("rounded-xl border p-2.5 transition-colors", mv ? "border-info/40 bg-info-soft/60" : "border-line bg-white")}>
              <button className="flex w-full items-start gap-2 text-left" onClick={() => onPick(r)}>
                <span className="mono w-6 shrink-0 pt-0.5 text-[12px] font-semibold text-mute">#{r.rank}</span>
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-1.5">
                    <span className="mono text-[13px] font-semibold">{r.incident}</span>
                    <span className="inline-flex size-4 items-center justify-center rounded text-[10px] font-bold text-white" style={{ background: URGENCY_COLOR(r.urgency) }}>
                      {r.urgency}
                    </span>
                    <span className="text-[12px] text-ink-2">
                      {r.unassigned}/{r.people} people waiting · {r.neighbourhood}
                    </span>
                    {mv ? (
                      <span className={cx("mono inline-flex items-center text-[10.5px] font-semibold", mv > 0 ? "text-ok" : "text-crit")}>
                        {mv > 0 ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />}
                        {Math.abs(mv)}
                      </span>
                    ) : null}
                  </span>
                  <span className="mono mt-0.5 block text-[10.5px] text-mute">
                    confidence {r.confidence.toFixed(2)} · {r.reports} report{r.reports === 1 ? "" : "s"} ({r.sources.join(", ")}){r.contradicted ? " · contradicted" : ""} · deadline {r.deadline_in >= 0 ? `in ${r.deadline_in} min` : `${-r.deadline_in} min ago`}
                  </span>
                  {r.assigned.map((a) => (
                    <span key={a.resource} className="mt-1 block text-[12px] text-ok">
                      {a.resource} → {a.shelter} · {a.people} people · {a.phase} {a.eta_min ? `(${a.eta_min} min)` : ""}
                    </span>
                  ))}
                  {recOf[r.incident] ? (
                    <span className="mt-1 block text-[12px] font-medium text-info">
                      AI recommends {recOf[r.incident].resource} → camp {recOf[r.incident].shelter} · awaiting you
                    </span>
                  ) : (
                    r.state !== "assigned" && r.why_waiting && <span className="mt-1 block text-[12px] leading-snug text-warn">{r.why_waiting}</span>
                  )}
                </span>
              </button>
              {r.decision_id && (
                <button className="mt-1 ml-8 text-[11px] text-info underline" onClick={() => onWhy(r.decision_id!)}>
                  why this decision?
                </button>
              )}
            </li>
          );
        })}
      </ol>
    </Panel>
  );
}

// ------------------------------------------------------------------ what changed after a reading
function ChangesCard({ msg, delta, history, selected, onSelect, onWhy }: { msg: FeedMessage | null; delta: FeedDelta | null; history: FeedMessage[]; selected: number | null; onSelect: (s: number | null) => void; onWhy: (id: string) => void }) {
  return (
    <Panel
      title="After the last reading"
      sub={msg ? `${msg.payload.station ?? "gauge"} ${Number(msg.payload.level_m).toFixed(2)} m · ${fmtT(msg.time)}${delta ? ` · next ${delta.minutes} sim-min` : ""}` : "waiting for the first gauge reading"}
      right={
        history.length ? (
          <select className={cx(inputCls, "h-7 w-36 py-0 text-xs")} value={selected ?? ""} onChange={(e) => onSelect(e.target.value ? Number(e.target.value) : null)}>
            <option value="">latest (live)</option>
            {[...history].reverse().slice(0, 30).map((h) => (
              <option key={h.seq} value={h.seq}>
                {fmtT(h.time)} · {Number(h.payload.level_m).toFixed(1)} m
              </option>
            ))}
          </select>
        ) : null
      }
      className="max-h-[46%]"
      bodyClass="scroll-thin overflow-y-auto"
    >
      {!delta ? (
        <p className="p-3 text-sm text-mute">After each river reading, this lists the decisions that changed: roads, rescue routes (old vs new), commitments, camps and supplies.</p>
      ) : (
        <div className="space-y-2.5 p-3 text-[12.5px]">
          <div className="grid grid-cols-4 gap-1.5 text-center">
            {(
              [
                ["roads closed", delta.roads_closed_count],
                ["boat streets", `+${delta.boat_streets_opened}`],
                ["new missions", delta.new_assignments.length],
                ["re-planned", delta.rerouted.length + delta.reassigned.length],
              ] as [string, React.ReactNode][]
            ).map(([k, v]) => (
              <div key={k} className="rounded-lg bg-wash px-1 py-1.5">
                <div className="mono text-base font-semibold">{v}</div>
                <div className="text-[10px] text-mute">{k}</div>
              </div>
            ))}
          </div>
          {delta.roads_closed.length > 0 && <p className="text-ink-2">Closed for ground teams: {delta.roads_closed.slice(0, 5).join(", ")}{delta.roads_closed_count > 5 ? ` +${delta.roads_closed_count - 5} more` : ""}.</p>}
          {delta.rerouted.map((r) => (
            <p key={r.assignment} className="text-ink-2">
              <b>{r.resource}</b> re-routed for {r.incident}: {r.old_km ?? "?"} km → {r.new_km ?? "?"} km
            </p>
          ))}
          {delta.reassigned.map((r, i) => (
            <p key={i} className="text-ink-2">
              <b>{r.incident}</b> reassigned {r.from} → {r.to}
            </p>
          ))}
          {delta.cancelled.map((c) => (
            <p key={c.assignment} className="text-crit">
              {c.resource} released from {c.incident}: {c.reason}
            </p>
          ))}
          {delta.new_assignments.slice(0, 4).map((a) => (
            <div key={a.assignment} className="rounded-lg border border-line p-2">
              <div>
                <b>{a.resource}</b> → {a.incident} ({a.people} people) → camp {a.shelter}{" "}
                <button className="text-[11px] text-info underline" onClick={() => onWhy(a.decision_id)}>
                  why
                </button>
              </div>
              {a.why && <div className="mt-0.5 text-[11.5px] leading-snug text-mute">{a.why}</div>}
            </div>
          ))}
          {delta.camps.length > 0 && (
            <div>
              <div className="label mb-1">camps</div>
              {delta.camps.map((c) => (
                <div key={c.shelter} className="mb-1">
                  <div className="flex justify-between text-[11.5px]">
                    <span className="truncate">
                      {c.shelter} {c.name}
                    </span>
                    <span className={cx("mono", c.full && "text-crit")}>
                      {c.occupancy}/{c.capacity} {c.occ_delta ? `(+${c.occ_delta})` : ""}
                    </span>
                  </div>
                  <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-black/[0.07]">
                    <div className={cx("h-full", c.full ? "bg-crit" : "bg-ink")} style={{ width: `${Math.min(100, (c.occupancy / Math.max(1, c.capacity)) * 100)}%` }} />
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="rounded-lg bg-info-soft/60 p-2" title={delta.supplies.standard}>
            <div className="label mb-0.5">supplies for camps (per day)</div>
            <div>
              {delta.supplies.people_in_camps} people sheltered → <b>{delta.supplies.water_l_per_day.toLocaleString()} L water</b>, <b>{delta.supplies.ration_kits_per_day} ration kits</b>
              {delta.supplies.new_arrivals ? ` (+${delta.supplies.added_water_l_per_day} L, +${delta.supplies.added_kits_per_day} kits since this reading)` : ""}
            </div>
            <div className="mt-0.5 text-[10.5px] text-mute">Sphere minimum standard: 15 L water and one ration kit per person per day</div>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ------------------------------------------------------------------ incoming messages
function FeedStream({ feed }: { feed: FeedMessage[] }) {
  return (
    <Panel title="Incoming feed" sub={`${feed.length} messages`} className="min-h-0 flex-1" bodyClass="scroll-thin min-h-0 overflow-y-auto">
      <ul className="divide-y divide-line" aria-live="polite">
        {feed.length === 0 && <li className="p-3 text-sm text-mute">Nothing received yet.</li>}
        {[...feed].reverse().slice(0, 120).map((m) => (
          <li key={m.seq} className="flex gap-2 px-3 py-2 text-[12px]">
            <span className="mt-0.5 text-mute">{TYPE_ICON[m.type]}</span>
            <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium capitalize">{m.type}</span>
                <Badge tone={SOURCE_TONE[m.source]}>{m.source}</Badge>
                <span className="mono text-[10.5px] text-mute">
                  #{m.seq} · t={m.tick}
                  {m.time ? ` · ${fmtT(m.time)}` : ""}
                </span>
              </span>
              <span className="mt-0.5 block leading-snug text-ink-2">{m.effects.join(" · ")}</span>
            </span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

// ------------------------------------------------------------------ the operator's queue
const REJECT_REASONS = ["unit needed elsewhere", "route unsafe (local knowledge)", "already handled / duplicate", "other"];

function ApprovalQueue({ frame, inject, openDecision, onFocus }: { frame: Frame; inject: (t: string, p: Record<string, unknown>, label?: string) => Promise<boolean>; openDecision: (id: string) => void; onFocus: (incident: string) => void }) {
  const recs = frame.recommendations ?? [];
  const [busy, setBusy] = useState<string | null>(null);
  const idle = frame.resources.filter((r) => r.status === "IDLE" && r.load === 0 && !r.blackout_until);
  const act = async (id: string, fn: () => Promise<boolean>) => {
    setBusy(id);
    try {
      await fn();
    } finally {
      setBusy(null);
    }
  };
  if (!frame.approval_mode)
    return (
      <div className="panel flex items-center gap-2 px-4 py-2.5 text-sm text-mute">
        <UserCheck className="size-4" /> Autonomous mode: the agents dispatch on their own. Switch to <b className="text-ink">AI recommends · you decide</b> to approve each dispatch.
      </div>
    );
  return (
    <Panel
      title="Decisions waiting for you"
      sub={recs.length ? `${recs.length} AI recommendation${recs.length === 1 ? "" : "s"} · nothing is dispatched until you approve; the safety shield re-checks every approval` : "No recommendations waiting. New ones appear as calls come in and units free up."}
      right={
        recs.length > 1 ? (
          <Button
            size="sm"
            variant="solid"
            icon={<Check className="size-3.5" />}
            loading={busy === "all"}
            onClick={() =>
              act("all", async () => {
                for (const r of recs) await inject("APPROVE_RECOMMENDATION", { recommendation_id: r.id }, `Approved ${r.resource} → ${r.incident}`);
                return true;
              })
            }
          >
            Approve all
          </Button>
        ) : null
      }
    >
      {recs.length > 0 && (
        <div className="scroll-thin flex gap-3 overflow-x-auto p-3">
          {recs.map((r) => {
            const inc = frame.incidents.find((i) => i.id === r.incident);
            const kind = frame.resources.find((x) => x.id === r.resource)?.kind;
            const others = idle.filter((u) => u.id !== r.resource && u.kind === kind);
            return (
              <div key={r.id} className="w-[340px] shrink-0 rounded-xl border border-info/30 bg-info-soft/30 p-3">
                <button className="w-full text-left" onClick={() => onFocus(r.incident)}>
                  <div className="flex items-center justify-between">
                    <span className="text-[14px] font-semibold">
                      {r.resource} → {r.incident}
                    </span>
                    <span className="mono text-[10.5px] text-mute">waiting {r.age} min</span>
                  </div>
                  <div className="mt-0.5 text-[12.5px] text-ink-2">
                    {r.people} people{inc ? ` · urgency ${inc.urgency}` : ""} · camp {r.shelter} · pickup {r.eta_pickup} min · total {r.eta_total} min
                  </div>
                  <div className="mono mt-1 flex gap-2 text-[10.5px] text-mute">
                    <span>cost {r.cost.toFixed(3)}</span>
                    <span>T {r.components.T.toFixed(2)}</span>
                    <span>R {r.components.R.toFixed(2)}</span>
                    <span>U {r.components.U.toFixed(2)}</span>
                    <span>F {r.components.F.toFixed(2)}</span>
                  </div>
                  {r.why && <div className="mt-1.5 line-clamp-3 text-[11.5px] leading-snug text-ink-2">{r.why}</div>}
                </button>
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <Button size="sm" variant="solid" icon={<Check className="size-3.5" />} loading={busy === r.id} onClick={() => act(r.id, () => inject("APPROVE_RECOMMENDATION", { recommendation_id: r.id }, `Approved ${r.resource} → ${r.incident}`))}>
                    Approve
                  </Button>
                  {others.length > 0 && (
                    <select
                      className={cx(inputCls, "h-8 w-28 py-0 text-xs")}
                      value=""
                      onChange={(e) => e.target.value && act(r.id, () => inject("APPROVE_RECOMMENDATION", { recommendation_id: r.id, resource_id: e.target.value }, `Sent ${e.target.value} to ${r.incident} instead of ${r.resource}`))}
                      aria-label="Send a different unit"
                    >
                      <option value="">other unit…</option>
                      {others.map((u) => (
                        <option key={u.id} value={u.id}>
                          {u.id}
                        </option>
                      ))}
                    </select>
                  )}
                  <select
                    className={cx(inputCls, "h-8 w-24 py-0 text-xs")}
                    value=""
                    onChange={(e) => e.target.value && act(r.id, () => inject("REJECT_RECOMMENDATION", { recommendation_id: r.id, reason: e.target.value }, `Rejected ${r.resource} → ${r.incident}`))}
                    aria-label="Reject with a reason"
                  >
                    <option value="">reject…</option>
                    {REJECT_REASONS.map((x) => (
                      <option key={x} value={x}>
                        {x}
                      </option>
                    ))}
                  </select>
                  <button className="text-[11px] text-info underline" onClick={() => openDecision(r.decision_id)}>
                    full reasoning
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
