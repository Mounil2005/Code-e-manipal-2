import { useEffect, useMemo, useState } from "react";
import { Background, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { useSim } from "../store/sim";
import { SimControls } from "../components/Shell";
import { Badge, Button, cx, Empty, inputCls, PageHeader, Panel, Segmented, Spinner } from "../components/ui";
import type { Decision, Message, RoundSummary } from "../types";
import { fmt } from "../lib/format";

const TYPE_STYLE: Record<string, { color: string; tone: string; width?: number; dash?: string }> = {
  REQUEST: { color: "#a1a1aa", tone: "neutral" },
  OFFER: { color: "#a1a1aa", tone: "neutral" },
  STATUS: { color: "#d4d4d8", tone: "neutral", dash: "3 3" },
  BID: { color: "#71717a", tone: "neutral", dash: "4 3" },
  DIRECTIVE: { color: "#0a0a0a", tone: "ink", width: 1.6 },
  PROVISIONAL_ACCEPT: { color: "#0891b2", tone: "info" },
  REJECT: { color: "#dc2626", tone: "crit", dash: "2 3" },
  RESERVE: { color: "#22d3ee", tone: "info" },
  ACCEPT_RESERVATION: { color: "#0891b2", tone: "info", width: 1.6 },
  REJECT_RESERVATION: { color: "#dc2626", tone: "crit" },
  SAFETY_REJECT: { color: "#dc2626", tone: "crit", width: 2.5 },
  COMMIT: { color: "#15803d", tone: "ok", width: 2.6 },
  RENEGOTIATE: { color: "#d97706", tone: "warn" },
};

interface RoundDetail extends RoundSummary {
  messages: Message[];
  decisions: Decision[];
}

export default function Agents() {
  const sim = useSim((s) => s.sim);
  const frame = useSim((s) => s.frame);
  const rounds = useSim((s) => s.rounds);
  const inject = useSim((s) => s.inject);
  const openDecision = useSim((s) => s.openDecision);
  const [list, setList] = useState<RoundSummary[]>([]);
  const [rid, setRid] = useState<string | null>(null);
  const [detail, setDetail] = useState<RoundDetail | null>(null);
  const [agents, setAgents] = useState<any>(null);
  const [onlyCommits, setOnlyCommits] = useState(true);
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [policy, setPolicy] = useState<string>(sim?.config.policy ?? "balanced");
  const [view, setView] = useState<"why" | "graph" | "messages">("why");
  const [lower, setLower] = useState<"rescue" | "shelters" | "routes">("rescue");

  const refresh = () => {
    if (!sim) return;
    api.get<{ rounds: RoundSummary[] }>(`/api/simulations/${sim.id}/negotiations?limit=200`).then((r) => setList(r.rounds)).catch(() => {});
    api.get<any>(`/api/simulations/${sim.id}/agents`).then(setAgents).catch(() => {});
  };
  useEffect(refresh, [sim?.id, rounds.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const shown = useMemo(() => list.filter((r) => !onlyCommits || r.commits.length || r.rejections), [list, onlyCommits]);
  useEffect(() => {
    if (!rid && shown.length) setRid(shown[0].id);
  }, [shown, rid]);
  useEffect(() => {
    if (!rid || !sim) return;
    setDetail(null);
    api.get<RoundDetail>(`/api/simulations/${sim.id}/negotiations/${rid}`).then(setDetail).catch(() => setDetail(null));
  }, [rid, sim]);

  const graph = useMemo(() => (detail ? buildGraph(detail) : { nodes: [], edges: [] }), [detail]);
  const msgs = useMemo(() => (detail ? detail.messages.filter((m) => typeFilter === "all" || m.type === typeFilter) : []), [detail, typeFilter]);

  if (!sim || !frame) return <Spinner />;
  return (
    <div className="space-y-5 p-5">
      <SimControls />
      <PageHeader
        eyebrow="negotiation"
        title="Negotiation"
        sub="How rescue units bid for each request and how conflicts are resolved. Every message here was produced by the engine."
        right={
          <>
            <select className="h-8 w-44 rounded-lg border border-line-2 bg-white px-2 text-sm outline-none focus:border-info" value={policy} onChange={(e) => { const v = e.target.value; inject("SET_POLICY", { policy: v }, `Policy → ${v}`).then((ok) => ok && setPolicy(v)); }}>
              {["balanced", "fastest", "urgency_first", "equity"].map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
            <Button size="sm" variant="solid" icon={<RefreshCw className="size-3.5" />} onClick={() => inject("SET_POLICY", { policy, renegotiate: true }, "Forced renegotiation")}>
              Force renegotiation
            </Button>
          </>
        }
      />
      <div className="grid gap-4 xl:grid-cols-[300px_1fr]">
        <Panel title="Decision rounds" sub={`${list.length} rounds · ${frame.metrics.negotiation_messages} messages`} right={<label className="flex items-center gap-1.5 text-xs text-mute"><input type="checkbox" checked={onlyCommits} onChange={(e) => setOnlyCommits(e.target.checked)} />Only eventful</label>} className="h-[600px]">
          <div className="scroll-thin h-full overflow-y-auto">
            {shown.length === 0 ? (
              <Empty title="No rounds yet" body="Start or step the simulation." />
            ) : (
              shown.map((r) => (
                <button key={r.id} onClick={() => setRid(r.id)} className={cx("block w-full border-b border-line/70 px-4 py-3 text-left hover:bg-wash", rid === r.id && "bg-info-soft/50")}>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">Minute {r.tick}</span>
                    <span className="ml-auto flex gap-1">
                      {r.commits.length > 0 && <Badge tone="ok">{r.commits.length} committed</Badge>}
                      {r.rejections > 0 && <Badge tone="crit">{r.rejections} rejected</Badge>}
                    </span>
                  </div>
                  <div className="mt-0.5 truncate text-xs text-mute">
                    {r.id} · {r.trigger} · {r.message_count} messages
                  </div>
                </button>
              ))
            )}
          </div>
        </Panel>

        <Panel
          title={detail ? `Round ${detail.id}` : "Select a round"}
          sub={detail ? `${detail.incidents.length} requests · ${detail.resources.length} units · ${detail.commits.length} commitments` : undefined}
          right={<Segmented size="sm" value={view} onChange={setView} options={[{ value: "why", label: "Why" }, { value: "graph", label: "Graph" }, { value: "messages", label: "Messages" }]} />}
          className="h-[600px]"
        >
          {!detail ? (
            rid ? <Spinner label="Loading round…" /> : <Empty title="No decisions yet" body="Press Start (or step the run). Each decision appears here with the reason it was made." />
          ) : view === "why" ? (
            <div className="scroll-thin h-full space-y-3 overflow-y-auto p-4">
              {detail.decisions.length === 0 && <Empty title="No decisions in this round" body="Nothing was matched. Try an eventful round." />}
              {detail.decisions.map((d) => (
                <div key={d.id} className="rounded-xl border border-line p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[15px] font-semibold">
                      {d.selected ? `${d.selected.resource_id} → ${d.incident_id}` : d.incident_id}
                    </span>
                    <Badge tone={d.outcome === "COMMITTED" ? "ok" : d.outcome === "UNMATCHED" ? "neutral" : "crit"}>{d.outcome.toLowerCase().replace(/_/g, " ")}</Badge>
                    <span className="text-xs text-mute">
                      {d.incident.people} people · urgency {d.incident.urgency} · {d.incident.neighbourhood}
                    </span>
                  </div>
                  <p className="mt-2 text-sm leading-relaxed text-ink-2">{d.explanation}</p>
                  {d.what_would_change && (
                    <p className="mt-2 rounded-lg bg-wash px-3 py-2 text-xs leading-relaxed text-mute">
                      <b className="text-ink-2">What would change this: </b>
                      {d.what_would_change}
                    </p>
                  )}
                  <div className="mt-3 flex items-center gap-3 text-xs text-mute">
                    <span>{d.candidates.length} units bid</span>
                    {d.rejections.length > 0 && <span>{d.rejections.length} ruled out</span>}
                    <button className="ml-auto text-info hover:underline" onClick={() => openDecision(d.id)}>
                      Open full decision record
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : view === "graph" ? (
            <div className="h-full">
              <ReactFlow nodes={graph.nodes} edges={graph.edges} fitView fitViewOptions={{ padding: 0.15 }} nodesDraggable proOptions={{ hideAttribution: true }} minZoom={0.3}>
                <Background gap={18} color="#e7e7e4" />
              </ReactFlow>
            </div>
          ) : (
            <div className="scroll-thin h-full overflow-y-auto">
              <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-line bg-white px-4 py-2 text-xs text-mute">
                Show
                <select className="rounded-lg border border-line px-2 py-1 text-xs" value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
                  <option value="all">all message types</option>
                  {Object.keys(TYPE_STYLE).map((t) => (
                    <option key={t} value={t}>
                      {t.replace(/_/g, " ").toLowerCase()}
                    </option>
                  ))}
                </select>
                <span className="ml-auto">{msgs.length} messages</span>
              </div>
              <ol>
                {msgs.map((m) => {
                  const st = TYPE_STYLE[m.type] ?? TYPE_STYLE.REQUEST;
                  return (
                    <li key={m.seq} className="flex gap-3 border-b border-line/50 px-4 py-2">
                      <span className="mt-1.5 size-2 shrink-0 rounded-full" style={{ background: st.color }} />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge tone={st.tone}>{m.type.replace(/_/g, " ").toLowerCase()}</Badge>
                          <span className="text-[13px]">
                            {m.from} <span className="text-faint">→</span> {m.to}
                          </span>
                        </div>
                        {(m.reason || Object.keys(m.payload).length > 0) && (
                          <div className="mt-0.5 text-xs leading-relaxed text-mute">
                            {m.reason && <span className="text-ink-2">{m.reason} </span>}
                            {Object.entries(m.payload)
                              .filter(([k]) => k !== "reason")
                              .map(([k, v]) => `${k}=${v}`)
                              .join(" · ")}
                          </div>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
        </Panel>
      </div>

      {agents && (
        <Panel
          title="Who is in the negotiation"
          right={
            <Segmented
              size="sm"
              value={lower}
              onChange={setLower}
              options={[
                { value: "rescue", label: "Rescue units" },
                { value: "shelters", label: "Camps" },
                { value: "routes", label: "Routes" },
              ]}
            />
          }
        >
          {lower === "rescue" && (
            <div className="divide-y divide-line">
              {agents.resources.map((r: any) => (
                <div key={r.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                  <span className="w-10 font-semibold">{r.id}</span>
                  <span className="text-mute">
                    {r.bids_submitted} bids · {r.commits} commitments
                  </span>
                  <span className="ml-auto flex gap-1">
                    {r.belief_mismatch_edges.length > 0 && <Badge tone="warn">{r.belief_mismatch_edges.length} outdated roads in its view</Badge>}
                    {r.discovered_closures.length > 0 && <Badge tone="info">{r.discovered_closures.length} closures found</Badge>}
                    {r.blackout_until && <Badge tone="crit">no comms</Badge>}
                  </span>
                </div>
              ))}
            </div>
          )}
          {lower === "shelters" && (
            <div className="divide-y divide-line">
              {agents.shelters.map((s: any) => (
                <div key={s.id} className="px-4 py-2.5 text-sm">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold">{s.id}</span>
                    <span className="truncate text-mute">{s.name}</span>
                    <Badge tone={s.free <= 0 ? "crit" : "ok"} className="ml-auto">
                      {s.free} free
                    </Badge>
                  </div>
                  <div className="mt-0.5 text-xs text-mute">
                    {s.occupancy}/{s.capacity} occupied · {s.reserved} reserved · {s.accepts} accepted · {s.rejects} refused
                  </div>
                </div>
              ))}
            </div>
          )}
          {lower === "routes" && (
            <div className="space-y-2 p-4 text-sm">
              <div>
                <span className="text-mute">Closed for ground teams: </span>
                {agents.route_agent.closed_for_ground.length} roads
              </div>
              <div>
                <span className="text-mute">Flooded roads boats can use: </span>
                {agents.route_agent.navigable_flooded_roads.length}
              </div>
              {agents.route_agent.bridges.map((b: any) => (
                <div key={b.id} className="flex items-center gap-2">
                  <span className="w-10 font-medium">{b.id}</span>
                  <span>{b.name}</span>
                  <Badge tone={b.ground_ok ? "ok" : "crit"} className="ml-auto">
                    {b.ground_ok ? "open" : "closed"}
                  </Badge>
                </div>
              ))}
              {agents.route_agent.manual_closures.map((c: any) => (
                <div key={c.id} className="text-crit">
                  {c.name || c.id}: {c.reason}
                </div>
              ))}
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}

function buildGraph(d: RoundDetail): { nodes: Node[]; edges: Edge[] } {
  const ids = new Set<string>();
  for (const m of d.messages) {
    if (m.from !== "broadcast") ids.add(m.from);
    if (m.to !== "broadcast" && m.to !== "shelters") ids.add(m.to);
  }
  d.incidents.forEach((i) => ids.add(i));
  const col = (id: string) => (id.startsWith("I-") ? 0 : id.startsWith("S") && /^S\d/.test(id) ? 2 : /^(coordination|coordinator|safety-shield|operator)$/.test(id) ? 3 : 1);
  const buckets: Record<number, string[]> = { 0: [], 1: [], 2: [], 3: [] };
  [...ids].sort().forEach((id) => buckets[col(id)].push(id));
  const nodes: Node[] = [];
  const X = [0, 300, 600, 300];
  for (const [c, list] of Object.entries(buckets)) {
    list.forEach((id, i) => {
      const cc = Number(c);
      const y = cc === 3 ? -110 - i * 0 : i * 74;
      const x = cc === 3 ? X[cc] + (i - (list.length - 1) / 2) * 170 : X[cc];
      const committed = d.matching.some((m) => m.incident === id || m.resource === id);
      nodes.push({
        id,
        position: { x, y },
        data: { label: id },
        style: {
          fontFamily: "JetBrains Mono",
          fontSize: 12,
          padding: "6px 10px",
          borderRadius: cc === 1 ? 999 : 10,
          border: `1px solid ${cc === 3 ? "#0a0a0a" : committed ? "#15803d" : "#d9d9d5"}`,
          background: cc === 3 ? "#0a0a0a" : cc === 0 ? "#fff7ed" : cc === 2 ? "#ecfeff" : "#fff",
          color: cc === 3 ? "#fff" : "#0a0a0a",
          width: 130,
          textAlign: "center",
        },
      });
    });
  }
  const agg = new Map<string, { m: Message; n: number }>();
  for (const m of d.messages) {
    if (m.to === "broadcast" || m.to === "shelters" || m.type === "STATUS" || m.type === "OFFER" || m.type === "REQUEST") continue;
    const k = `${m.from}|${m.to}|${m.type}`;
    const cur = agg.get(k);
    agg.set(k, { m, n: (cur?.n ?? 0) + 1 });
  }
  const edges: Edge[] = [...agg.entries()].map(([k, { m, n }]) => {
    const st = TYPE_STYLE[m.type] ?? TYPE_STYLE.REQUEST;
    const label = m.type === "BID" ? `bid ${m.payload.cost}` : m.type === "COMMIT" ? "COMMIT" : m.type.replace(/_/g, " ").toLowerCase() + (n > 1 ? ` ×${n}` : "");
    return {
      id: k,
      source: m.from,
      target: m.to,
      label,
      animated: m.type === "COMMIT",
      labelStyle: { fontFamily: "JetBrains Mono", fontSize: 9.5, fill: st.color },
      labelBgStyle: { fill: "#fff", fillOpacity: 0.9 },
      style: { stroke: st.color, strokeWidth: st.width ?? 1.2, strokeDasharray: st.dash },
      markerEnd: { type: MarkerType.ArrowClosed, color: st.color, width: 14, height: 14 },
    };
  });
  return { nodes, edges };
}
