import { createContext, useCallback, useContext, useEffect, useState, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { cx } from "../lib/util";

// ── buttons ──────────────────────────────────────────────────────────
type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger" | "subtle"; size?: "sm" | "md"; busy?: boolean };
export function Button({ variant = "subtle", size = "md", busy, className, children, disabled, ...p }: BtnProps) {
  const v = {
    primary: "bg-amber text-ink hover:bg-amber-dim font-medium",
    subtle: "bg-raised text-text hover:bg-line border border-line",
    ghost: "text-muted hover:text-text hover:bg-raised",
    danger: "bg-bad/10 text-bad border border-bad/30 hover:bg-bad/20",
  }[variant];
  return (
    <button {...p} disabled={disabled || busy}
      className={cx("inline-flex items-center justify-center gap-2 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer", size === "sm" ? "px-2.5 py-1 text-xs" : "px-3.5 py-2 text-sm", v, className)}>
      {busy && <Spinner className="h-3.5 w-3.5" />}{children}
    </button>
  );
}
export const Spinner = ({ className = "h-5 w-5" }: { className?: string }) => (
  <svg className={cx("animate-spin", className)} viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity=".25" /><path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" /></svg>
);

// ── layout bits ──────────────────────────────────────────────────────
export const Card = ({ children, className, title, action }: { children: ReactNode; className?: string; title?: ReactNode; action?: ReactNode }) => (
  <section className={cx("rounded-xl border border-line bg-panel", className)}>
    {(title || action) && <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3"><h3 className="font-display text-sm tracking-wide">{title}</h3>{action}</header>}
    <div className="p-4">{children}</div>
  </section>
);
export const PageHeader = ({ title, sub, action }: { title: ReactNode; sub?: ReactNode; action?: ReactNode }) => (
  <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
    <div><h1 className="font-display text-2xl font-bold">{title}</h1>{sub && <p className="mt-1 text-sm text-muted">{sub}</p>}</div>{action}
  </div>
);
export const Stat = ({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: "ok" | "bad" | "warn" }) => (
  <div className="rounded-xl border border-line bg-panel p-4">
    <div className="text-xs uppercase tracking-wider text-muted">{label}</div>
    <div className={cx("mt-1 font-display text-2xl font-bold", tone === "ok" && "text-ok", tone === "bad" && "text-bad", tone === "warn" && "text-warn")}>{value}</div>
    {hint && <div className="mt-0.5 text-xs text-muted">{hint}</div>}
  </div>
);
export const Empty = ({ children }: { children: ReactNode }) => <div className="rounded-xl border border-dashed border-line px-6 py-10 text-center text-sm text-muted">{children}</div>;
export const Loading = ({ label = "Loading…" }: { label?: string }) => <div className="flex items-center gap-2 py-10 text-sm text-muted"><Spinner className="h-4 w-4" />{label}</div>;
export const ErrorBox = ({ error, retry }: { error: unknown; retry?: () => void }) => (
  <div className="rounded-xl border border-bad/30 bg-bad/10 p-4 text-sm text-bad">
    {error instanceof Error ? error.message : String(error)}{retry && <button onClick={retry} className="ml-3 underline cursor-pointer">Retry</button>}
  </div>
);

const tones: Record<string, string> = {
  ok: "bg-ok/10 text-ok border-ok/30", bad: "bg-bad/10 text-bad border-bad/30", warn: "bg-amber/10 text-amber border-amber/30", mute: "bg-raised text-muted border-line",
};
export const Badge = ({ tone = "mute", children }: { tone?: keyof typeof tones; children: ReactNode }) => (
  <span className={cx("inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium", tones[tone])}>{children}</span>
);
const STATUS_TONE: Record<string, keyof typeof tones> = {
  active: "ok", connected: "ok", open: "ok", confirmed: "ok", PAID: "ok", COMPLETED: "ok", READY: "ok", success: "ok", INFO: "mute",
  pending: "warn", onboarding: "warn", handoff: "warn", PAYMENT_PENDING: "warn", PREPARING: "warn", WARNING: "warn", UNKNOWN: "warn", CONNECTING: "warn",
  suspended: "bad", declined: "bad", cancelled: "bad", CANCELLED: "bad", EXPIRED: "bad", ERROR: "bad", CRITICAL: "bad", CLOSED: "bad", DISCONNECTED: "bad",
};
export const StatusBadge = ({ status }: { status: string }) => <Badge tone={STATUS_TONE[status] ?? STATUS_TONE[status.toLowerCase()] ?? "mute"}>{status.replace(/_/g, " ")}</Badge>;

// ── form controls ────────────────────────────────────────────────────
const ctl = "w-full rounded-lg border border-line bg-ink px-3 py-2 text-sm text-text placeholder:text-muted/60 focus:border-amber focus:outline-none";
export const Field = ({ label, hint, children, className }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) => (
  <label className={cx("block", className)}><span className="mb-1 block text-xs font-medium text-muted">{label}</span>{children}{hint && <span className="mt-1 block text-xs text-muted/80">{hint}</span>}</label>
);
export const Input = ({ className, ...p }: InputHTMLAttributes<HTMLInputElement>) => <input {...p} className={cx(ctl, className)} />;
export const Textarea = ({ className, ...p }: TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...p} className={cx(ctl, "min-h-[80px] resize-y", className)} />;
export const Select = ({ className, ...p }: SelectHTMLAttributes<HTMLSelectElement>) => <select {...p} className={cx(ctl, className)} />;
export const Toggle = ({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label?: ReactNode; disabled?: boolean }) => (
  <button type="button" role="switch" aria-checked={checked} disabled={disabled} onClick={() => onChange(!checked)} className="inline-flex items-center gap-2 text-sm disabled:opacity-50 cursor-pointer">
    <span className={cx("relative h-5 w-9 rounded-full transition", checked ? "bg-amber" : "bg-line")}><span className={cx("absolute top-0.5 h-4 w-4 rounded-full bg-ink transition-all", checked ? "left-[18px]" : "left-0.5")} /></span>{label}
  </button>
);

// ── modal ────────────────────────────────────────────────────────────
export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 pt-[8vh]" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={onClose}>
          <motion.div onMouseDown={(e) => e.stopPropagation()} initial={{ y: 12, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: 12, opacity: 0 }}
            className={cx("w-full rounded-xl border border-line bg-panel shadow-2xl", wide ? "max-w-3xl" : "max-w-lg")} role="dialog" aria-modal="true">
            <header className="flex items-center justify-between border-b border-line px-5 py-3"><h2 className="font-display text-base">{title}</h2><button onClick={onClose} className="text-muted hover:text-text cursor-pointer" aria-label="Close">✕</button></header>
            <div className="p-5">{children}</div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

// ── toasts ───────────────────────────────────────────────────────────
type Toast = { id: number; kind: "ok" | "bad"; text: string };
const ToastCtx = createContext<(kind: Toast["kind"], text: string) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setItems((s) => [...s, { id, kind, text }]);
    setTimeout(() => setItems((s) => s.filter((t) => t.id !== id)), kind === "bad" ? 8000 : 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-[60] flex max-w-sm flex-col gap-2">
        <AnimatePresence>{items.map((t) => (
          <motion.div key={t.id} initial={{ x: 20, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ opacity: 0 }}
            className={cx("rounded-lg border px-4 py-3 text-sm shadow-lg", t.kind === "ok" ? "border-ok/40 bg-panel text-ok" : "border-bad/40 bg-panel text-bad")}>{t.text}</motion.div>
        ))}</AnimatePresence>
      </div>
    </ToastCtx.Provider>
  );
}

export function CopyField({ value, label }: { value: string; label?: string }) {
  const toast = useToast();
  return (
    <div>
      {label && <div className="mb-1 text-xs text-muted">{label}</div>}
      <div className="flex gap-2"><code className="min-w-0 flex-1 truncate rounded-lg border border-line bg-ink px-3 py-2 text-xs text-amber">{value}</code>
        <Button size="sm" onClick={() => navigator.clipboard.writeText(value).then(() => toast("ok", "Copied"))}>Copy</Button></div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: string; badge?: number }[]; value: T; onChange: (t: T) => void }) {
  return (
    <div className="mb-5 flex gap-1 overflow-x-auto border-b border-line">
      {tabs.map((t) => (
        <button key={t.id} onClick={() => onChange(t.id)}
          className={cx("relative whitespace-nowrap px-3.5 py-2.5 text-sm transition cursor-pointer", value === t.id ? "text-amber" : "text-muted hover:text-text")}>
          {t.label}{!!t.badge && <span className="ml-1.5 rounded-full bg-amber px-1.5 py-0.5 text-[10px] font-semibold text-ink">{t.badge}</span>}
          {value === t.id && <motion.span layoutId="tab-underline" className="absolute inset-x-0 -bottom-px h-0.5 bg-amber" />}
        </button>
      ))}
    </div>
  );
}
