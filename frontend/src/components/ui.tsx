import clsx from "clsx";
import { Loader2 } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

export const cx = clsx;

export function Panel({ title, right, children, className, bodyClass, sub }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; bodyClass?: string; sub?: ReactNode }) {
  return (
    <section className={cx("panel flex min-h-0 flex-col", className)}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0">
            <div className="truncate text-[14px] font-semibold tracking-tight text-ink">{title}</div>
            {sub && <div className="mt-0.5 truncate text-xs text-mute">{sub}</div>}
          </div>
          <div className="flex shrink-0 items-center gap-1.5">{right}</div>
        </header>
      )}
      <div className={cx("min-h-0 flex-1", bodyClass)}>{children}</div>
    </section>
  );
}

type BtnVariant = "solid" | "outline" | "ghost" | "danger" | "info";
export function Button({ variant = "outline", size = "md", loading, icon, children, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BtnVariant; size?: "sm" | "md"; loading?: boolean; icon?: ReactNode }) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || loading}
      className={cx(
        "inline-flex shrink-0 items-center justify-center gap-1.5 rounded-full font-medium transition disabled:cursor-not-allowed disabled:opacity-45",
        size === "sm" ? "h-7 px-2.5 text-xs" : "h-9 px-3.5 text-sm",
        variant === "solid" && "bg-ink text-white hover:bg-ink-2",
        variant === "outline" && "border border-line-2 bg-white text-ink hover:border-ink/30 hover:bg-wash",
        variant === "ghost" && "text-ink-2 hover:bg-black/[0.04]",
        variant === "danger" && "border border-crit/25 bg-crit-soft/60 text-crit hover:bg-crit-soft",
        variant === "info" && "border border-info/25 bg-info-soft/70 text-info hover:bg-info-soft",
        className,
      )}
    >
      {loading ? <Loader2 className="size-3.5 animate-spin" /> : icon}
      {children}
    </button>
  );
}

const TONES: Record<string, string> = {
  neutral: "bg-black/[0.04] text-ink-2 border-black/[0.06]",
  info: "bg-info-soft text-info border-info/20",
  warn: "bg-warn-soft text-warn border-warn/20",
  crit: "bg-crit-soft text-crit border-crit/20",
  ok: "bg-ok-soft text-ok border-ok/20",
  ink: "bg-ink text-white border-ink",
};
export function Badge({ tone = "neutral", children, className, dot }: { tone?: keyof typeof TONES | string; children: ReactNode; className?: string; dot?: boolean }) {
  return (
    <span className={cx("inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium", TONES[tone] ?? TONES.neutral, className)}>
      {dot && <span className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

export function Stat({ label, value, unit, tone, hint, className }: { label: string; value: ReactNode; unit?: string; tone?: "crit" | "warn" | "ok" | "info"; hint?: ReactNode; className?: string }) {
  return (
    <div className={cx("min-w-0", className)}>
      <div className="label truncate">{label}</div>
      <div className={cx("num mt-1 font-display text-[22px] leading-none font-semibold tracking-tight", tone === "crit" && "text-crit", tone === "warn" && "text-warn", tone === "ok" && "text-ok", tone === "info" && "text-info")}>
        {value ?? "—"}
        {unit && <span className="ml-0.5 text-xs font-medium text-mute">{unit}</span>}
      </div>
      {hint && <div className="mt-1 truncate text-[11px] text-mute">{hint}</div>}
    </div>
  );
}

export function Empty({ title, body, icon, action }: { title: string; body?: ReactNode; icon?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex h-full min-h-32 flex-col items-center justify-center gap-2 p-6 text-center">
      {icon && <div className="text-faint">{icon}</div>}
      <div className="text-sm font-medium text-ink-2">{title}</div>
      {body && <div className="max-w-sm text-xs text-mute">{body}</div>}
      {action}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-4 text-sm text-mute">
      <Loader2 className="size-4 animate-spin" />
      {label}
    </div>
  );
}

export function Segmented<T extends string | number>({ value, options, onChange, size = "md" }: { value: T; options: { value: T; label: ReactNode }[]; onChange: (v: T) => void; size?: "sm" | "md" }) {
  return (
    <div className="inline-flex rounded-full border border-line-2 bg-white p-0.5">
      {options.map((o) => (
        <button
          key={String(o.value)}
          onClick={() => onChange(o.value)}
          className={cx("rounded-full font-medium transition", size === "sm" ? "px-3 py-1 text-xs" : "px-3.5 py-1.5 text-sm", value === o.value ? "bg-ink text-white" : "text-ink-2 hover:bg-black/[0.04]")}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Meter({ value, max, tone = "ink", reserved = 0, className }: { value: number; max: number; tone?: string; reserved?: number; className?: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 100;
  const rpct = max > 0 ? Math.min(100 - pct, (reserved / max) * 100) : 0;
  return (
    <div className={cx("flex h-1.5 w-full overflow-hidden rounded-full bg-black/[0.06]", className)}>
      <div className={cx("h-full", tone === "crit" ? "bg-crit" : tone === "warn" ? "bg-warn" : tone === "ok" ? "bg-ok" : tone === "info" ? "bg-info" : "bg-ink")} style={{ width: `${pct}%` }} />
      {rpct > 0 && <div className="h-full bg-info/45" style={{ width: `${rpct}%` }} />}
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="label">{label}</span>
        {hint && <span className="mono text-[11px] text-ink-2">{hint}</span>}
      </div>
      {children}
    </label>
  );
}

export const inputCls = "h-9 w-full rounded-lg border border-line-2 bg-white px-2.5 text-sm outline-none focus:border-info";

export function KV({ k, v, mono = true }: { k: ReactNode; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1 text-sm">
      <span className="text-mute">{k}</span>
      <span className={cx("text-right text-ink", mono && "mono num text-[12.5px]")}>{v}</span>
    </div>
  );
}

export function PageHeader({ eyebrow, title, sub, right }: { eyebrow: string; title: ReactNode; sub?: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 pb-2" data-section={eyebrow}>
      <div className="min-w-0">
        <h1 className="font-display text-[26px] leading-tight font-semibold tracking-[-0.02em] text-ink">{title}</h1>
        {sub && <p className="mt-1 max-w-2xl text-sm text-mute">{sub}</p>}
      </div>
      {right && <div className="flex flex-wrap items-center gap-2">{right}</div>}
    </div>
  );
}
