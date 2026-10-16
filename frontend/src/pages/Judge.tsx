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
        Now see it run on a real-time feed in <Link className="font-semibold underline" to="/live">Live ops</Link>, or read <Link className="underline" to="/data">what data we used and why</Link>.
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

  const mainLabel = working ? "Running the live engine…" : active === -1 ? "Start the story" : active >= CHAPTERS.length - 1 ? "Story complete" : CHAPTERS[next].cta;
  const stats: [string, React.ReactNode][] = frame
    ? [
        ["Rescued", frame.metrics.people_rescued],
        ["Still waiting", frame.metrics.unmet_people],
        ["Active missions", frame.assignments.filter((a) => a.status === "COMMITTED" || a.status === "LOADED").length],
      ]
    : [];

  return (
    <div className="space-y-5 p-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h1 className="font-display text-[26px] leading-tight font-semibold tracking-[-0.02em]">Kerala, August 2018</h1>
          <p className="mt-1 max-w-2xl text-sm text-mute">When the rescue plan breaks, who keeps coordinating? Real roads, terrain and river data; rescuers and calls for help are simulated live.</p>
        </div>
        <div className="flex items-center gap-2">
          {active >= 0 && (
            <Button variant="ghost" icon={<RotateCcw className="size-4" />} disabled={working} onClick={() => run(0)} title="Restart from 14 Aug">
              Restart
            </Button>
          )}
          <Button variant="solid" loading={working} icon={working ? undefined : active === -1 ? <Play className="size-4" /> : <ChevronRight className="size-4" />} disabled={working || (active >= CHAPTERS.length - 1 && onStory)} onClick={() => run(active === -1 ? 0 : next)}>
            {mainLabel}
          </Button>
        </div>
      </div>

      {/* story progress */}
      <ol className="panel grid grid-cols-5 gap-1 p-1.5" aria-label="Story chapters">
        {CHAPTERS.map((c, k) => {
          const done = active >= k;
          const isNext = (active === -1 && k === 0) || k === active + 1;
          const current = active === k;
          return (
            <li key={k}>
              <button
                disabled={working || (!isNext && !done) || (k > 0 && !onStory)}
                onClick={() => run(k)}
                aria-current={current ? "step" : undefined}
                className={cx("flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-left transition", current ? "bg-ink text-white" : isNext ? "bg-info-soft/60 hover:bg-info-soft" : done ? "hover:bg-black/[0.04]" : "text-mute")}
              >
                <span className={cx("flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold", done && !current ? "bg-ok text-white" : current ? "bg-white text-ink" : "bg-black/[0.07]")}>{done && !current ? <Check className="size-3.5" /> : k + 1}</span>
                <span className="hidden min-w-0 truncate text-sm font-medium md:block">{c.title}</span>
              </button>
            </li>
          );
        })}
      </ol>

      {error && <p className="rounded-xl border border-crit/25 bg-crit-soft p-3 text-sm text-crit">{error}</p>}

      {/* what is happening now */}
      {active >= 0 && ctx ? (
        <div className="panel p-5" role="status" aria-live="polite">
          <div className="text-xs font-medium text-info">
            Chapter {active + 1} of {CHAPTERS.length} · {CHAPTERS[active].when}
          </div>
          <div className="mt-1 text-xl font-semibold tracking-tight">{CHAPTERS[active].title}</div>
          <p className="mt-2 max-w-4xl text-[15px] leading-relaxed text-ink-2">{CHAPTERS[active].narrate(ctx)}</p>
        </div>
      ) : (
        <div className="panel p-5 text-sm text-mute">
          Press <b className="text-ink">Start the story</b>. Each chapter runs the real simulation and the camera follows what happens.
        </div>
      )}

      {world && frame && (
        <div className="panel relative h-[64vh] min-h-[400px] overflow-hidden">
          <CityMap world={world} frame={frame} focus={focus} transitionMs={Math.max(80, 1000 / (sim?.speed || 1))} />
          <RiverCard />
          <div className="absolute top-3 left-3 z-10 flex gap-px overflow-hidden rounded-xl border border-line bg-white/95 shadow-sm backdrop-blur">
            {stats.map(([label, value]) => (
              <div key={label} className="px-3.5 py-2">
                <div className="text-xs text-mute">{label}</div>
                <div className="num text-lg leading-tight font-semibold">{value}</div>
              </div>
            ))}
            <div className="border-l border-line px-3.5 py-2">
              <div className="text-xs text-mute">Time</div>
              <div className="text-sm leading-tight font-semibold">{frame.flood_real_time ? new Date(frame.flood_real_time).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : `minute ${frame.tick}`}</div>
            </div>
          </div>
          <div className="absolute bottom-3 left-3 z-10 rounded-xl border border-line bg-white/90 px-3 py-1.5 backdrop-blur">
            <MapLegend />
          </div>
        </div>
      )}

      <details className="panel p-4">
        <summary className="cursor-pointer text-sm font-medium">More experiments and data sources</summary>
        <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
          <Button size="sm" disabled={working} onClick={() => createSim("coordinator_outage", "decentralized", { seed: 7 })}>
            Coordinator outage (t=15 to 75)
          </Button>
          <Button size="sm" disabled={working} onClick={() => createSim("bridge_dependency", "decentralized", { seed: 7 })}>
            Across the river
          </Button>
        </div>
        <p className="mt-3 text-xs text-mute">
          Outage assumption: central and greedy dispatch have no standby coordinator; peer communication and local safety stay available. Real data: OpenStreetMap, Copernicus DEM GLO-30, GloFAS discharge, IMD rainfall, WorldPop. Inundation depths come from the simulator's threshold model; see docs/flood_validation.md.
        </p>
      </details>
    </div>
  );
}
