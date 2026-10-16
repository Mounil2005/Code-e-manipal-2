import { useState } from "react";
import { Bomb, Construction, Radio, Ship, Tent, Waves, Zap } from "lucide-react";
import { useSim } from "../store/sim";
import { Button } from "./ui";

/** One-click fault injection. Each button posts a real event to the engine;
 * targets for the triple failure are chosen by the backend from live state. */
export function ChaosButtons({ layout = "row" }: { layout?: "row" | "grid" }) {
  const frame = useSim((s) => s.frame);
  const world = useSim((s) => s.world);
  const inject = useSim((s) => s.inject);
  const busy = useSim((s) => s.busy);
  if (!frame || !world) return null;
  const critical = world.map.critical_bridge;
  const bridgeIds = world.edges.filter((e) => e.kind === "bridge").map((e) => e.id).sort((a, b) => (a === critical ? -1 : b === critical ? 1 : 0));
  const openBridge = bridgeIds.find((b) => frame.edges[b] && !frame.edges[b][2]);
  const boat = frame.resources.find((r) => r.kind === "boat" && r.status !== "DISABLED" && r.assignment_id) ?? frame.resources.find((r) => r.kind === "boat" && r.status !== "DISABLED");
  const shelter = [...frame.shelters].sort((a, b) => b.reserved - a.reserved)[0];
  const blackout = frame.resources.find((r) => r.status !== "DISABLED" && r.assignment_id && !r.blackout_until);
  return (
    <div className={layout === "grid" ? "grid grid-cols-2 gap-2" : "flex flex-wrap gap-1.5"}>
      <Button variant="danger" size="sm" icon={<Bomb className="size-3.5" />} loading={busy.TRIPLE_FAILURE} onClick={() => inject("TRIPLE_FAILURE", {}, "Triple failure injected")}>
        Triple failure
      </Button>
      <Button size="sm" icon={<Construction className="size-3.5" />} disabled={!openBridge} loading={busy.CLOSE_EDGE} onClick={() => openBridge && inject("CLOSE_EDGE", { edge_id: openBridge, reason: "bridge collapse" }, `Close ${openBridge}`)}>
        Close {openBridge ?? "bridge"}
      </Button>
      <Button size="sm" icon={<Ship className="size-3.5" />} disabled={!boat} loading={busy.RESOURCE_FAILURE} onClick={() => boat && inject("RESOURCE_FAILURE", { resource_id: boat.id, reason: "engine failure" }, `Fail ${boat.id}`)}>
        Fail {boat?.id ?? "boat"}
      </Button>
      <Button size="sm" icon={<Tent className="size-3.5" />} loading={busy.SHELTER_CAPACITY} onClick={() => inject("SHELTER_CAPACITY", { shelter_id: shelter.id, capacity: "occupancy", reason: "overflow" }, `Overflow ${shelter.id}`)}>
        Overflow {shelter.id}
      </Button>
      <Button size="sm" icon={<Waves className="size-3.5" />} loading={busy.FLOOD_SURGE} onClick={() => inject("FLOOD_SURGE", { delta: 0.5 }, "Flood surge +0.5 m")}>
        Surge +0.5m
      </Button>
      <Button size="sm" icon={<Radio className="size-3.5" />} disabled={!blackout} loading={busy.COMM_BLACKOUT} onClick={() => blackout && inject("COMM_BLACKOUT", { resource_id: blackout.id, duration: 15 }, `${blackout.id} comms blackout`)}>
        Blackout {blackout?.id ?? ""}
      </Button>
      {layout === "grid" && (
        <Button size="sm" variant="info" icon={<Zap className="size-3.5" />} onClick={() => {
          const i = frame.incidents.find((x) => x.status !== "RESCUED" && x.status !== "FALSE_ALARM");
          if (i) inject("CONTRADICTORY_REPORT", { incident_id: i.id, claim: "NO_VICTIMS", source: "resident" }, `Contradict ${i.id}`);
        }}>
          Contradict report
        </Button>
      )}
    </div>
  );
}

/** Chaos buttons behind one menu, so the control bar stays calm. */
export function ChaosMenu() {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <Button size="sm" variant="danger" icon={<Bomb className="size-3.5" />} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        Inject failure
      </Button>
      {open && (
        <div className="panel fade-in absolute right-0 z-40 mt-2 w-[340px] p-3">
          <div className="mb-2 text-xs text-mute">Break something and watch the agents re-plan.</div>
          <ChaosButtons layout="grid" />
        </div>
      )}
    </div>
  );
}
