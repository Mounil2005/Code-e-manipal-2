import { useMemo, useState } from "react";
import { useSim } from "../store/sim";
import type { AuditRecord } from "../types";
import { cx, Empty } from "./ui";

const SEV: Record<string, string> = { info: "bg-faint", warn: "bg-warn", critical: "bg-crit", ok: "bg-ok" };
const CATS = ["all", "event", "assignment", "cancel", "reroute", "shelter", "safety", "incident"];

export function ActivityFeed({ limit = 140, compact }: { limit?: number; compact?: boolean }) {
  const audit = useSim((s) => s.audit);
  const setSelection = useSim((s) => s.setSelection);
  const openDecision = useSim((s) => s.openDecision);
  const [cat, setCat] = useState("all");
  const rows = useMemo(() => audit.filter((a) => cat === "all" || a.category === cat).slice(-limit).reverse(), [audit, cat, limit]);
  return (
    <div className="flex h-full min-h-0 flex-col">
      {!compact && (
        <div className="flex flex-wrap gap-1 border-b border-line px-3 py-2">
          {CATS.map((c) => (
            <button key={c} onClick={() => setCat(c)} className={cx("shrink-0 rounded-full px-2.5 py-1 text-xs capitalize", cat === c ? "bg-ink text-white" : "text-mute hover:bg-black/[0.05]")}>
              {c}
            </button>
          ))}
        </div>
      )}
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {rows.length === 0 ? (
          <Empty title="No activity yet" body="Start the simulation — agent negotiations, reroutes and safety decisions stream here." />
        ) : (
          <ul className="divide-y divide-line/70">
            {rows.map((a) => (
              <FeedRow key={a.id} a={a} onRef={(k, v) => (k === "decision" ? openDecision(v) : setSelection({ type: k as any, id: v }))} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function FeedRow({ a, onRef }: { a: AuditRecord; onRef: (k: string, v: string) => void }) {
  const refs = Object.entries(a.refs).filter(([k, v]) => typeof v === "string" && ["incident", "resource", "shelter", "decision"].includes(k)) as [string, string][];
  return (
    <li className="fade-in flex gap-2.5 px-3 py-2">
      <span className={cx("mt-1.5 size-1.5 shrink-0 rounded-full", SEV[a.severity])} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5 text-xs text-faint">
          <span>Minute {a.tick}</span>
          <span>·</span>
          <span className="capitalize">{a.category}</span>
        </div>
        <div className={cx("text-[12.5px] leading-snug", a.severity === "critical" ? "text-crit" : "text-ink-2")}>{a.message}</div>
        {refs.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {refs.map(([k, v]) => (
              <button key={k} onClick={() => onRef(k, v)} className="mono rounded border border-line px-1.5 py-0.5 text-[11px] text-mute hover:border-info hover:text-info">
                {k === "decision" ? "why? " : ""}
                {v}
              </button>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}
