import { NavLink, useLocation } from "react-router-dom";
import { CircleAlert, CircleCheck, Info, Pause, Play, RotateCcw, SkipForward, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useSim } from "../store/sim";
import { clock, STRATEGY_LABEL } from "../lib/format";
import { Badge, Button, cx, Segmented } from "./ui";
import type { Strategy } from "../types";

const NAV = [
  { to: "/judge", label: "Judge Mode" },
  { to: "/live", label: "Live ops" },
  { to: "/command", label: "Command" },
  { to: "/dispatch", label: "Fleet" },
  { to: "/agents", label: "Negotiation" },
  { to: "/scenarios", label: "Chaos Lab" },
  { to: "/decision-lab", label: "Decision Lab" },
  { to: "/arena", label: "Arena" },
  { to: "/replay", label: "Replay" },
  { to: "/safety", label: "Safety" },
  { to: "/reports", label: "Report" },
  { to: "/data", label: "Data & evidence" },
];

export function Logo() {
  return (
    <div className="flex items-center gap-2">
      <svg viewBox="0 0 32 32" className="size-6" aria-hidden>
        <rect width="32" height="32" rx="8" fill="#0a0a0a" />
        <path d="M6 20c3.3-4 6.7-4 10 0s6.7 4 10 0" stroke="#22d3ee" strokeWidth="2.6" fill="none" strokeLinecap="round" />
        <circle cx="16" cy="11" r="3.2" fill="#fff" />
      </svg>
      <span className="font-display text-[19px] font-semibold tracking-[-0.03em]">resqnet</span>
    </div>
  );
}

export function TopBar() {
  const conn = useSim((s) => s.conn);
  const world = useSim((s) => s.world);
  const loc = useLocation();
  return (
    <div className="no-print sticky top-0 z-30">
      <div className="mono bg-gradient-to-r from-[#06b6d4] via-[#22d3ee] to-[#06b6d4] py-1 text-center text-[11px] font-medium tracking-wide text-ink">
        {world?.map.real
          ? `${world.map.name} · real roads & terrain (OpenStreetMap, Copernicus DEM) · floods and incidents simulated · not an operational dispatch system`
          : "synthetic simulation · research & education only · not an operational dispatch system"}
      </div>
      <header className="border-b border-line bg-paper/85 backdrop-blur-md">
        <div className="mx-auto flex h-14 max-w-[1680px] items-center gap-4 px-4">
          <NavLink to="/command" className="shrink-0">
            <Logo />
          </NavLink>
          <nav className="scroll-thin flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
            {NAV.map((n) => {
              const active = loc.pathname.startsWith(n.to);
              return (
                <NavLink key={n.to} to={n.to} className={cx("flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-[13.5px] transition", active ? "bg-white text-ink shadow-[0_0_0_1px_var(--color-line-2)]" : "text-ink-2 hover:text-ink")}>
                  {active && <span className="size-1.5 rounded-full bg-info" />}
                  {n.label}
                </NavLink>
              );
            })}
          </nav>
          <div className="flex shrink-0 items-center gap-2">
            <span className={cx("mono inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px]", conn === "live" ? "border-ok/25 bg-ok-soft text-ok" : conn === "offline" ? "border-crit/25 bg-crit-soft text-crit" : "border-warn/25 bg-warn-soft text-warn")}>
              <span className={cx("size-1.5 rounded-full bg-current", conn !== "live" && "animate-pulse")} />
              {conn === "live" ? "live" : conn}
            </span>
          </div>
        </div>
      </header>
    </div>
  );
}

const SPEEDS = [0.5, 1, 2, 4, 8, 16];

export function SimControls({ extra }: { extra?: React.ReactNode }) {
  const sim = useSim((s) => s.sim);
  const frame = useSim((s) => s.frame);
  const control = useSim((s) => s.control);
  const setSpeed = useSim((s) => s.setSpeed);
  const busy = useSim((s) => s.busy);
  if (!sim || !frame) return null;
  const running = sim.status === "running";
  const pctDone = Math.min(100, (frame.tick / sim.duration) * 100);
  return (
    <div className="panel flex flex-wrap items-center gap-x-4 gap-y-2 px-3 py-2">
      <div className="flex items-center gap-3 pr-1">
        <div>
          <div className="label">mission clock</div>
          <div className="mono num text-[17px] font-semibold tracking-tight">{clock(frame.tick)}</div>
        </div>
        <div className="hidden w-28 sm:block">
          <div className="mono flex justify-between text-[10.5px] text-mute">
            <span>t={frame.tick}</span>
            <span>{sim.duration}</span>
          </div>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-black/[0.07]">
            <div className="h-full bg-ink transition-all" style={{ width: `${pctDone}%` }} />
          </div>
        </div>
      </div>
      <div className="flex items-center gap-1.5">
        {running ? (
          <Button variant="solid" icon={<Pause className="size-4" />} onClick={() => control("pause")} loading={busy.pause}>
            Pause
          </Button>
        ) : (
          <Button variant="solid" icon={<Play className="size-4" />} onClick={() => control("start")} disabled={frame.finished} loading={busy.start}>
            {frame.tick === 0 ? "Start" : "Resume"}
          </Button>
        )}
        <Button icon={<SkipForward className="size-4" />} onClick={() => control("step", 1)} disabled={running || frame.finished} loading={busy.step} title="Advance one tick (1 simulated minute)">
          Step
        </Button>
        <Button variant="ghost" icon={<RotateCcw className="size-4" />} onClick={() => control("reset")} loading={busy.reset} title="Reset this run to t=0">
          Reset
        </Button>
      </div>
      <Segmented size="sm" value={sim.speed} options={SPEEDS.map((s) => ({ value: s, label: `${s}×` }))} onChange={(v) => setSpeed(v)} />
      <div className="flex items-center gap-1.5">
        <Badge tone={running ? "ok" : sim.status === "finished" ? "ink" : "neutral"} dot>
          {sim.status}
        </Badge>
        <Badge tone="info">{STRATEGY_LABEL[sim.strategy]}</Badge>
        <span className="mono hidden text-[11px] text-mute lg:inline">
          {sim.scenario_name} · seed {sim.seed} · v{frame.version}
        </span>
      </div>
      <div className="ml-auto flex items-center gap-2">{extra}</div>
    </div>
  );
}

export function NewRunMenu() {
  const createSim = useSim((s) => s.createSim);
  const sim = useSim((s) => s.sim);
  const busy = useSim((s) => s.busy.create);
  const maps = useSim((s) => s.maps);
  const mapId = useSim((s) => s.mapId);
  const [open, setOpen] = useState(false);
  const [mapSel, setMapSel] = useState(mapId);
  useEffect(() => setMapSel(mapId), [mapId]);
  const [scenario, setScenario] = useState(sim?.scenario ?? "normal");
  const [strategy, setStrategy] = useState<Strategy>(sim?.strategy ?? "decentralized");
  const SC = [
    ["kerala_2018", "Kerala 2018 · real flood replay"],
    ["bridge_dependency", "Across the river"],
    ["coordinator_outage", "Coordinator outage (t=15 to 75)"],
    ["normal", "A · Normal flood"],
    ["contradictory", "B · Conflicting information"],
    ["triple_failure", "C · Triple failure (scheduled t=22)"],
    ["flash_flood", "Flash flood"],
    ["bridge_closure", "Critical bridge closure"],
    ["resource_shortage", "Resource shortage"],
    ["shelter_overflow", "Shelter overflow"],
  ];
  return (
    <div className="relative">
      <Button size="sm" onClick={() => setOpen((o) => !o)}>
        New run
      </Button>
      {open && (
        <div className="panel fade-in absolute right-0 z-40 mt-2 w-80 p-3">
          {maps.length > 1 && (
            <>
              <div className="label mb-1.5">map</div>
              <div className="mb-3 space-y-1">
                {maps.map((m) => (
                  <button key={m.id} onClick={() => setMapSel(m.id)} className={cx("flex w-full flex-col rounded-lg px-2.5 py-1.5 text-left text-sm", mapSel === m.id ? "bg-ink text-white" : "hover:bg-black/[0.04]")}>
                    <span>{m.name}</span>
                    <span className={cx("text-[11px]", mapSel === m.id ? "text-white/70" : "text-mute")}>{m.real ? `${m.region} · ${m.population ? `≈${Math.round(m.population / 1000)}k residents` : "real OSM roads"}` : "hand-built grid · tests & benchmarks"}</span>
                  </button>
                ))}
              </div>
            </>
          )}
          <div className="label mb-1.5">scenario</div>
          <div className="space-y-1">
            {SC.map(([k, l]) => (
              <button key={k} onClick={() => setScenario(k)} className={cx("flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-sm", scenario === k ? "bg-ink text-white" : "hover:bg-black/[0.04]")}>
                {l}
              </button>
            ))}
          </div>
          <div className="label mt-3 mb-1.5">coordination strategy</div>
          <Segmented size="sm" value={strategy} onChange={setStrategy} options={(["decentralized", "centralized", "greedy"] as Strategy[]).map((s) => ({ value: s, label: STRATEGY_LABEL[s] }))} />
          <div className="mt-3 flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant="solid"
              loading={busy}
              onClick={async () => {
                await createSim(scenario, strategy, {}, mapSel).catch(() => {});
                try {
                  localStorage.setItem("resqnet.map", mapSel);
                } catch {
                  /* ignore */
                }
                setOpen(false);
              }}
            >
              Launch
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function Toasts() {
  const toasts = useSim((s) => s.toasts);
  const dismiss = useSim((s) => s.dismiss);
  return (
    <div className="no-print pointer-events-none fixed right-4 bottom-4 z-50 flex w-[380px] max-w-[calc(100vw-2rem)] flex-col gap-2" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className="panel fade-in pointer-events-auto flex gap-2.5 p-3">
          <div className="pt-0.5">
            {t.kind === "ok" ? <CircleCheck className="size-4 text-ok" /> : t.kind === "warn" ? <TriangleAlert className="size-4 text-warn" /> : t.kind === "crit" ? <CircleAlert className="size-4 text-crit" /> : <Info className="size-4 text-info" />}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium first-letter:uppercase">{t.title}</div>
            {t.body && <div className="mt-0.5 text-xs leading-relaxed text-mute">{t.body}</div>}
          </div>
          <button aria-label="Dismiss" onClick={() => dismiss(t.id)} className="text-faint hover:text-ink">
            <X className="size-4" />
          </button>
        </div>
      ))}
    </div>
  );
}
