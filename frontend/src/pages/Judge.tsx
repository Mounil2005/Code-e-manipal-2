import { useState } from "react";
import { Link } from "react-router-dom";
import { Check, ChevronRight, Play, RotateCcw } from "lucide-react";
import { useSim } from "../store/sim";
import { CityMap, MapLegend } from "../components/CityMap";
import { RiverCard } from "../components/RiverCard";
import { Button, cx } from "../components/ui";
import type { Frame, StaticWorld } from "../types";

/** Guided demonstration: the real August 2018 flood at Aluva, told in five chapters. Every
 * chapter drives the live engine (play to a tick, inject a failure, open a decision record). */

const STORY_MAP = "kerala_aluva";
const STORY_SCENARIO = "kerala_2018";

type Ctx = { frame: Frame; world: StaticWorld; rain: (day: string) => number | null; effects: string[] };

interface Chapter {
  when: string;
  title: string;
  cta: string;
  narrate: (c: Ctx) => React.ReactNode;
}

const fmtQ = (q?: number | null) => (q ? `${Math.round(q).toLocaleString()} m³/s` : "—");
const people = (f: Frame) => f.incidents.filter((i) => i.status !== "RESCUED" && i.status !== "FALSE_ALARM").reduce((n, i) => n + i.people, 0);
const groundClosed = (f: Frame, w: StaticWorld) => w.edges.filter((e) => (e.kind === "road" || e.kind === "bridge") && f.edges[e.id] && !f.edges[e.id][0]).length;
const boatStreets = (f: Frame, w: StaticWorld) => w.edges.filter((e) => e.kind === "road" && f.edges[e.id]?.[1]).length;

const CHAPTERS: Chapter[] = [
  {
    when: "14 Aug 2018 · 00:00",
    title: "Aluva, on the Periyar",
    cta: "Open the map",
    narrate: ({ frame, world, rain }) => (
      <>
        Real road network, bridges and terrain of Aluva. River discharge (GloFAS reanalysis) stands at <b>{fmtQ(frame.flood_discharge)}</b>, and IMD rain gauges recorded <b>{rain("2018-08-14") ?? "—"} mm</b> today.{" "}
        {frame.incidents.length} calls for help are already in, covering about {people(frame)} people. {frame.resources.filter((r) => r.kind === "boat").length} boats and {frame.resources.filter((r) => r.kind === "ground").length} ground teams negotiate who goes where; a safety shield checks every commitment. {world.shelters.length} relief camps are open.
      </>
    ),
  },
  {
    when: "15 Aug 2018",
    title: "The Periyar rises",
    cta: "Play to 15 Aug evening",
    narrate: ({ frame, world, rain }) => (
      <>
        Discharge has climbed to <b>{fmtQ(frame.flood_discharge)}</b> and IMD measured <b>{rain("2018-08-15") ?? "—"} mm</b> of rain on 15 Aug. <b>{groundClosed(frame, world)}</b> road segments are now impassable for ground teams, while <b>{boatStreets(frame, world)}</b> flooded streets are deep enough for boats. That is why boats take over the low ground and ground teams work the higher roads.{" "}
        {frame.metrics.people_rescued} people are safe so far.
      </>
    ),
  },
  {
    when: "16 Aug 2018 · flood peak",
    title: "Three failures at the flood peak",
    cta: "Play to the peak and break it",
    narrate: ({ frame, effects }) => (
      <>
        At <b>{fmtQ(frame.flood_discharge)}</b> three things break at the same moment: a bridge collapses, a working boat breaks down and a relief camp fills up. The engine picks the targets from live state (the bridge most routes cross, else the most critical one).{" "}
        {effects.length ? <span className="text-ink-2">What happened: {effects.map((x) => x.replace(/\[[A-Z_]+\]\s*/g, "")).join(" · ")}.</span> : null} Affected missions are renegotiated and the safety shield blocks anything unsafe.
      </>
    ),
  },
  {
    when: "16 Aug 2018",
    title: "Why that boat, why that camp?",
    cta: "Explain the latest decision",
    narrate: () => (
      <>Every commitment is stored as a decision record: the evidence behind the SOS, every bid with its cost breakdown, the camp negotiation, the safety checks and what would have changed the outcome. The panel that opens is generated from that record.</>
    ),
  },
  {
    when: "17 Aug 2018",
    title: "The water recedes: the final count",
    cta: "Play to the end",
    narrate: ({ frame }) => (
      <>
        <b>{frame.metrics.people_rescued}</b> people rescued, <b>{frame.metrics.unmet_people}</b> still waiting, mean time to service <b>{frame.metrics.mean_time_to_service ?? "—"} min</b>, {frame.metrics.deadline_misses} urgent deadlines missed and <b>{frame.metrics.route_violations + frame.metrics.invariant_violations}</b> safety violations ({frame.metrics.safety_rejections} unsafe commits blocked by the shield).{" "}
        Now see it run on a real-time feed in <Link className="font-semibold underline" to="/live">Live ops</Link>, try a different future in the <Link className="underline" to="/decision-lab">Decision Lab</Link>, or read <Link className="underline" to="/data">what data we used and why</Link>.
      </>
    ),
  },
];

const TARGET = [0, 62, 104, null, 180];

export default function Judge() {
  const { world, frame, sim, createSim, control, inject, openDecision, focus, setFocus, lastEvent } = useSim();
  const [chapter, setChapter] = useState(-1);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const onStory = sim?.scenario === STORY_SCENARIO && sim?.map === STORY_MAP;

  const playTo = (target: number, speed: number) =>
    new Promise<void>((resolve) => {
      const st = useSim.getState();
      if (!st.frame || st.frame.tick >= target || st.frame.finished) return resolve();
      const unsub = useSim.subscribe((s) => {
        if (s.frame && (s.frame.tick >= target || s.frame.finished)) {
          unsub();
          useSim.getState().control("pause").finally(() => resolve());
        }
      });
      st.setSpeed(speed).then(() => st.control("start"));
    });

  const floodedCore = () => {
    const f = useSim.getState().frame;
    const w = useSim.getState().world;
    if (!f || !w) return;
    const deep = Object.entries(f.depths ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 14);
    const pts = deep.map(([id]) => w.nodes.find((n) => n.id === id)).filter(Boolean).map((n) => [n!.x, n!.y] as [number, number]);
    if (pts.length) setFocus({ points: pts, zoom: 14.75 });
  };

  const breakBridge = async () => {
    const st = useSim.getState();
    const w = st.world;
    const f = st.frame;
    if (!w || !f) return;
    // the bridge most active routes cross right now; else the map's most critical one
    const counts = w.edges
      .filter((e) => e.kind === "bridge" && !f.edges[e.id]?.[2])
      .map((e) => ({
        id: e.id,
        n: f.resources.filter((r) => {
          const path = [r.node ?? "", ...r.route];
          return r.assignment_id && path.some((x, i) => i > 0 && ((path[i - 1] === e.u && x === e.v) || (path[i - 1] === e.v && x === e.u)));
        }).length,
      }))
      .sort((a, b) => b.n - a.n || a.id.localeCompare(b.id));
    const target = counts[0]?.n ? counts[0].id : w.map.critical_bridge ?? counts[0]?.id;
    await inject("TRIPLE_FAILURE", target ? { edge_id: target } : {}, "Triple failure at the flood peak");
  };

  const run = async (k: number) => {
    setWorking(true);
    setError(null);
    try {
      if (k === 0) {
        if (sim?.status === "running") await control("pause");
        await createSim(STORY_SCENARIO, "decentralized", { seed: 7 }, STORY_MAP);
      } else if (k === 1) {
        floodedCore();
        await playTo(TARGET[1]!, 8);
        floodedCore();
      } else if (k === 2) {
        await playTo(TARGET[2]!, 8);
        await breakBridge();
      } else if (k === 3) {
        const f = useSim.getState().frame;
        const a = f?.assignments[f.assignments.length - 1];
        if (a) openDecision(a.decision_id);
      } else if (k === 4) {
        setFocus({ points: [], fit: true });
        await playTo(TARGET[4]!, 16);
      }
      setChapter(k);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setWorking(false);
    }
  };

  const next = Math.min(chapter + 1, CHAPTERS.length - 1);
  const rain = (day: string) => sim?.flood_event?.rain_daily_mm?.[day] ?? null;
  const ctx: Ctx | null = world && frame ? { frame, world, rain, effects: lastEvent?.type === "TRIPLE_FAILURE" ? lastEvent.effects : [] } : null;
  const active = onStory ? chapter : -1;

  return (
    <div className="space-y-4 p-4">
      <section className="rounded-2xl bg-slate-950 p-6 text-white">
        <p className="mono text-xs uppercase tracking-[0.25em] text-cyan-300">resqnet · live demonstration · real data</p>
        <h1 className="my-2 text-3xl font-semibold tracking-tight">Kerala, August 2018: when the rescue plan breaks, who keeps coordinating?</h1>
        <p className="max-w-4xl text-slate-300">
          Real OpenStreetMap roads and bridges of Aluva, real terrain, the real river discharge and rainfall of the 2018 flood, replayed in 180 minutes. The rescuers, calls for help and decisions are simulated live; nothing is pre-recorded.
        </p>
      </section>

      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        {/* chapters */}
        <ol className="panel space-y-1 p-2" aria-label="Story chapters">
          {CHAPTERS.map((c, k) => {
            const done = active >= k;
            const isNext = (active === -1 && k === 0) || k === active + 1;
            return (
              <li key={k}>
                <button
                  disabled={working || (!isNext && !done) || (k > 0 && !onStory)}
                  onClick={() => run(k)}
                  className={cx("flex w-full items-start gap-3 rounded-xl p-3 text-left transition", isNext ? "bg-ink text-white" : done ? "hover:bg-black/[0.04]" : "opacity-50")}
                >
                  <span className={cx("mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full text-[12px] font-semibold", done ? "bg-ok text-white" : isNext ? "bg-white text-ink" : "bg-black/[0.06]")}>{done ? <Check className="size-3.5" /> : k + 1}</span>
                  <span className="min-w-0">
                    <span className={cx("mono block text-[10.5px] uppercase tracking-wide", isNext ? "text-cyan-300" : "text-mute")}>{c.when}</span>
                    <span className="block text-[14.5px] font-medium leading-snug">{c.title}</span>
                  </span>
                </button>
              </li>
            );
          })}
          <div className="flex gap-2 p-2 pt-3">
            <Button variant="solid" className="flex-1" loading={working} icon={working ? undefined : active === -1 ? <Play className="size-4" /> : <ChevronRight className="size-4" />} disabled={working || (active >= CHAPTERS.length - 1 && onStory)} onClick={() => run(active === -1 ? 0 : next)}>
              {working ? "Running the live engine…" : active === -1 ? "Start the story" : active >= CHAPTERS.length - 1 ? "Story complete" : CHAPTERS[next].cta}
            </Button>
            {active >= 0 && (
              <Button variant="ghost" icon={<RotateCcw className="size-4" />} disabled={working} onClick={() => run(0)} title="Restart from 14 Aug">
                Restart
              </Button>
            )}
          </div>
        </ol>

        {/* map + narrative */}
        <div className="space-y-3">
          {active >= 0 && ctx ? (
            <div className="panel p-4" role="status" aria-live="polite">
              <div className="mono text-[11px] uppercase tracking-wide text-info">
                Chapter {active + 1} · {CHAPTERS[active].when}
              </div>
              <div className="mt-0.5 text-lg font-semibold tracking-tight">{CHAPTERS[active].title}</div>
              <p className="mt-1.5 max-w-4xl text-[14.5px] leading-relaxed text-ink-2">{CHAPTERS[active].narrate(ctx)}</p>
            </div>
          ) : (
            <div className="panel p-4 text-sm text-mute">Press <b className="text-ink">Start the story</b>. Each chapter runs the actual simulation and the camera follows what happens.</div>
          )}
          {error && <p className="rounded-xl border border-crit/25 bg-crit-soft p-3 text-sm text-crit">{error}</p>}
          {world && frame && (
            <>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                {(
                  [
                    ["Real time", frame.flood_real_time ? new Date(frame.flood_real_time).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : `minute ${frame.tick}`],
                    ["People rescued", frame.metrics.people_rescued],
                    ["Still waiting", frame.metrics.unmet_people],
                    ["Active assignments", frame.assignments.filter((a) => a.status === "COMMITTED" || a.status === "LOADED").length],
                  ] as [string, React.ReactNode][]
                ).map(([label, value]) => (
                  <div key={label} className="panel p-3">
                    <div className="label">{label}</div>
                    <div className="mt-1 text-xl font-semibold tracking-tight">{value}</div>
                  </div>
                ))}
              </div>
              <div className="panel relative h-[60vh] min-h-[360px] overflow-hidden">
                <CityMap world={world} frame={frame} focus={focus} transitionMs={Math.max(80, 1000 / (sim?.speed || 1))} />
                <RiverCard />
                <div className="absolute bottom-3 left-3 z-10 rounded-xl border border-line bg-white/90 px-3 py-1.5 backdrop-blur">
                  <MapLegend />
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      <details className="panel p-4">
        <summary className="cursor-pointer text-sm font-medium">More experiments</summary>
        <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
          <Button size="sm" disabled={working} onClick={() => createSim("coordinator_outage", "decentralized", { seed: 7 })}>
            Coordinator outage (t=15 to 75)
          </Button>
          <Button size="sm" disabled={working} onClick={() => createSim("bridge_dependency", "decentralized", { seed: 7 })}>
            Across the river
          </Button>
          <Link className="underline" to="/arena">
            Measure all strategies in the Arena
          </Link>
          <Link className="underline" to="/decision-lab">
            Compare a what-if future
          </Link>
        </div>
        <p className="mt-3 text-xs text-mute">
          Outage assumption: central and greedy dispatch have no standby coordinator; peer communication and local safety stay available. Real data: OpenStreetMap, Copernicus DEM GLO-30, GloFAS discharge, IMD rainfall, WorldPop. Inundation depths come from the simulator's threshold model; see docs/flood_validation.md.
        </p>
      </details>
    </div>
  );
}
