import { Fragment, useMemo, useState } from "react";
import { Anchor, ChevronDown, Truck } from "lucide-react";
import { useSim } from "../store/sim";
import { SimControls } from "../components/Shell";
import { Badge, Button, cx, Empty, Field, inputCls, KV, Meter, PageHeader, Panel, Segmented, Spinner } from "../components/ui";
import { isOpen, statusLabel, UNIT_TONE } from "../lib/format";
import type { Assignment, Resource } from "../types";

export default function Dispatch() {
  const { frame, inject, openDecision, setSelection } = useSim();
  const [unit, setUnit] = useState("");
  const [inc, setInc] = useState("");
  const [filter, setFilter] = useState<"active" | "all" | "cancelled">("active");
  const [openUnit, setOpenUnit] = useState<string | null>(null);
  const assignments = useMemo(() => {
    if (!frame) return [];
    const xs = frame.assignments.filter((a) => (filter === "active" ? a.status === "COMMITTED" || a.status === "LOADED" : filter === "cancelled" ? a.status === "CANCELLED" : true));
    return [...xs].reverse();
  }, [frame, filter]);
  if (!frame) return <Spinner label="Loading fleet…" />;
  const idle = frame.resources.filter((r) => r.status === "IDLE" && !r.blackout_until);
  const open = frame.incidents.filter((i) => isOpen(i) && i.unassigned > 0);

  return (
    <div className="space-y-5 p-5">
      <SimControls />
      <PageHeader eyebrow="fleet" title="Fleet" sub="Every unit, what it is doing and why. Manual actions are checked by the safety shield like any agent proposal." />
      <Panel title="Units" sub={`${frame.resources.length} units · ${idle.length} idle`}>
        <table className="w-full text-left text-sm">
          <thead className="label">
            <tr className="border-b border-line">
              {["Unit", "Status", "Aboard", "Mission", "Arrives", ""].map((h) => (
                <th key={h} className="px-4 py-2 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {frame.resources.map((r) => {
              const a = frame.assignments.find((x) => x.id === r.assignment_id);
              const isOpen = openUnit === r.id;
              return (
                <Fragment key={r.id}>
                  <tr className={cx("cursor-pointer border-b border-line/70 hover:bg-wash", isOpen && "bg-wash")} onClick={() => setOpenUnit(isOpen ? null : r.id)}>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-3">
                        <span className="grid size-8 place-items-center rounded-lg bg-ink text-white">{r.kind === "boat" ? <Anchor className="size-4" /> : <Truck className="size-4" />}</span>
                        <span>
                          <span className="font-medium">{r.id}</span> <span className="text-mute">{r.name}</span>
                        </span>
                      </div>
                    </td>
                    <td className="px-4">
                      <Badge tone={UNIT_TONE[r.status]}>{statusLabel(r.status)}</Badge>
                    </td>
                    <td className="w-40 px-4">
                      <div className="flex items-center gap-2">
                        <Meter value={r.load} max={r.capacity} tone="ok" />
                        <span className="mono text-xs text-mute">
                          {r.load}/{r.capacity}
                        </span>
                      </div>
                    </td>
                    <td className="px-4 text-ink-2">{a ? `${a.incident_id} → ${a.shelter_id}` : "—"}</td>
                    <td className="px-4 text-ink-2">{r.eta !== null ? `minute ${r.eta}` : "—"}</td>
                    <td className="px-4 text-right">
                      <ChevronDown className={cx("inline size-4 text-faint transition", isOpen && "rotate-180")} />
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="border-b border-line bg-wash">
                      <td colSpan={6} className="px-4 pb-4">
                        <UnitCard r={r} a={a} history={frame.assignments.filter((x) => x.resource_id === r.id)} onInspect={() => setSelection({ type: "resource", id: r.id })} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </Panel>
      <div className="grid gap-5 xl:grid-cols-[360px_1fr]">
        <Panel title="Assign a unit" sub="Checked by the safety shield before it is accepted">
          <div className="space-y-5 p-5">
            <Field label="Idle unit">
              <select className={inputCls} value={unit} onChange={(e) => setUnit(e.target.value)}>
                <option value="">Select…</option>
                {idle.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.id} · {r.name} ({r.kind}, {r.capacity} seats)
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Request that needs help">
              <select className={inputCls} value={inc} onChange={(e) => setInc(e.target.value)}>
                <option value="">Select…</option>
                {open.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.id} · u{i.urgency} · {i.unassigned} uncovered · {i.node}
                  </option>
                ))}
              </select>
            </Field>
            <Button variant="solid" disabled={!unit || !inc} onClick={() => inject("MANUAL_ASSIGN", { resource_id: unit, incident_id: inc }, `Directed ${unit} → ${inc}`).then((ok) => ok && (setUnit(""), setInc("")))}>
              Assign mission
            </Button>
            {idle.length === 0 && <p className="text-xs text-mute">No idle units right now — every operational unit holds a commitment.</p>}
            <p className="text-xs leading-relaxed text-mute">An infeasible directive (no route for that unit type, no shelter capacity, stale state) is rejected with the shield's reason; nothing is animated unless the engine commits it.</p>
          </div>
        </Panel>
        <Panel title="Missions" right={<Segmented size="sm" value={filter} onChange={setFilter} options={[{ value: "active", label: "Active" }, { value: "cancelled", label: "Cancelled" }, { value: "all", label: "All" }]} />}>
          <div className="scroll-thin max-h-[420px] overflow-auto">
            {assignments.length === 0 ? (
              <Empty title="No assignments in this view" />
            ) : (
              <table className="w-full text-left text-[12.5px]">
                <thead className="label sticky top-0 bg-white">
                  <tr className="border-b border-line">
                    {["Unit", "Request", "Camp", "People", "Status", "Arrives", ""].map((h) => (
                      <th key={h} className="px-3 py-2 font-normal">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {assignments.map((a) => (
                    <tr key={a.id} className="border-b border-line/60 hover:bg-wash">
                        <td className="mono px-3 py-2">{a.resource_id}</td>
                      <td className="mono px-3">{a.incident_id}</td>
                      <td className="mono px-3">{a.shelter_id}</td>
                      <td className="mono px-3">{a.loaded || a.people}</td>
                      <td className="px-3">
                        <Badge tone={a.status === "CANCELLED" ? "crit" : a.status === "COMPLETED" ? "ok" : a.status === "LOADED" ? "warn" : "info"}>{a.status.toLowerCase()}</Badge>
                        {a.cancel_reason && <div className="max-w-[260px] truncate text-[11px] text-mute" title={a.cancel_reason}>{a.cancel_reason}</div>}
                      </td>
                      <td className="mono px-3 text-mute">{a.status === "COMMITTED" ? `t=${a.eta_pickup}` : a.status === "LOADED" ? `t=${a.eta_dropoff}` : "—"}</td>
                        <td className="px-3 whitespace-nowrap">
                        <button className="text-xs text-info hover:underline" onClick={() => openDecision(a.decision_id)}>
                          why?
                        </button>
                        {a.status === "COMMITTED" && (
                          <button className="ml-2 text-xs text-crit hover:underline" onClick={() => inject("CANCEL_ASSIGNMENT", { assignment_id: a.id }, `Cancelled ${a.id}`)}>
                            cancel
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}

function UnitCard({ r, a, history, onInspect }: { r: Resource; a?: Assignment; history: Assignment[]; onInspect: () => void }) {
  const inject = useSim((s) => s.inject);
  const openDecision = useSim((s) => s.openDecision);
  const util = r.busy_ticks + r.idle_ticks ? r.busy_ticks / (r.busy_ticks + r.idle_ticks) : 0;
  return (
    <div className={cx("flex flex-col pt-3", r.status === "DISABLED" && "opacity-70")}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2.5">
          <div className="grid size-9 place-items-center rounded-xl bg-ink text-white">{r.kind === "boat" ? <Anchor className="size-4" /> : <Truck className="size-4" />}</div>
          <div>
            <div className="font-display text-[15px] font-semibold">{r.id} <span className="font-normal text-mute">{r.name}</span></div>
            <div className="mono text-[11px] text-mute">
              {r.kind} · {r.capacity} seats · {r.speed_kmh} km/h
            </div>
          </div>
        </div>
        <Badge tone={UNIT_TONE[r.status]}>{statusLabel(r.status)}</Badge>
      </div>
      <div className="mt-3">
        <div className="mb-1 flex justify-between text-[11px] text-mute">
          <span>aboard</span>
          <span className="mono">
            {r.load}/{r.capacity}
          </span>
        </div>
        <Meter value={r.load} max={r.capacity} tone="ok" />
      </div>
      <div className="mt-2">
        <KV k="Mission" v={a ? `${a.incident_id} → ${a.shelter_id}` : "—"} />
        <KV k="ETA" v={r.eta !== null ? `t=${r.eta}` : "—"} />
        <KV k="Route left" v={`${r.route.length} nodes`} />
        <KV k="Utilisation" v={`${Math.round(util * 100)}% · ${r.distance_km} km`} />
        {r.blackout_until !== null && <KV k="Comms" v={<span className="text-warn">down → t={r.blackout_until}</span>} />}
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {history.slice(-6).map((h) => (
          <button key={h.id} onClick={() => openDecision(h.decision_id)} title={`${h.id} ${h.status} ${h.cancel_reason}`} className={cx("mono rounded-md border px-1.5 text-[10.5px]", h.status === "COMPLETED" ? "border-ok/30 text-ok" : h.status === "CANCELLED" ? "border-crit/30 text-crit line-through" : "border-info/30 text-info")}>
            {h.incident_id}
          </button>
        ))}
      </div>
      <div className="mt-auto flex flex-wrap gap-1.5 pt-3">
        {r.status === "DISABLED" ? (
          <Button size="sm" variant="info" onClick={() => inject("RESOURCE_RESTORE", { resource_id: r.id }, `${r.id} restored`)}>
            Restore
          </Button>
        ) : (
          <Button size="sm" onClick={() => window.confirm(`Mark ${r.id} as unavailable? Its mission will be re-planned.`) && inject("RESOURCE_FAILURE", { resource_id: r.id, reason: "operator: unavailable" }, `${r.id} unavailable`)}>
            Mark unavailable
          </Button>
        )}
        {a?.status === "COMMITTED" && (
          <Button size="sm" onClick={() => inject("REQUEST_REASSIGN", { resource_id: r.id }, `${r.id} released`)}>
            Reassign
          </Button>
        )}
        {a && (
          <Button size="sm" variant="ghost" onClick={() => openDecision(a.decision_id)}>
            Bids
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={onInspect}>
          Inspect
        </Button>
      </div>
    </div>
  );
}
