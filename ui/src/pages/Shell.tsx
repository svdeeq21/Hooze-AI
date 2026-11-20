import { useState } from "react";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { clearSession, getSession } from "../lib/api";
import { getTheme, setTheme, type ThemeChoice } from "../lib/theme";
import { cx } from "../lib/util";
import { Button } from "../components/ui";

const nav = [{ to: "/", label: "Overview", icon: "◎" }, { to: "/clients", label: "Clients", icon: "▤" }, { to: "/events", label: "Events", icon: "≋" }] as const;

function ThemeSwitch() {
  const [t, setT] = useState<ThemeChoice>(getTheme);
  const pick = (v: ThemeChoice) => { setTheme(v); setT(v); };
  return (
    <div role="radiogroup" aria-label="Theme" className="grid grid-cols-3 gap-1 rounded-lg border border-line bg-ink p-1 text-[11px]">
      {(["system", "light", "dark"] as const).map((v) => (
        <button key={v} type="button" role="radio" aria-checked={t === v} onClick={() => pick(v)}
          className={cx("cursor-pointer rounded-md py-1 capitalize transition", t === v ? "bg-raised text-amber" : "text-muted hover:text-text")}>{v === "system" ? "Auto" : v}</button>
      ))}
    </div>
  );
}

function NavLinks({ onPick }: { onPick?: () => void }) {
  return (
    <nav className="flex flex-col gap-1">
      {nav.map((n) => (
        <Link key={n.to} to={n.to} onClick={onPick} activeOptions={{ exact: n.to === "/" }} className="flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm text-muted transition hover:bg-raised hover:text-text"
          activeProps={{ className: "bg-raised !text-amber" }}><span className="w-4 text-center" aria-hidden="true">{n.icon}</span>{n.label}</Link>
      ))}
    </nav>
  );
}

export default function Shell() {
  const s = getSession();
  const [open, setOpen] = useState(false);
  const path = useRouterState({ select: (r) => r.location.pathname });
  const brand = <span className="font-display text-lg font-bold">Hooze <span className="text-amber">Op</span></span>;
  const footer = (
    <div className="space-y-3 text-xs text-muted">
      <ThemeSwitch />
      <div className="truncate" title={s?.baseUrl}>API: {s?.baseUrl}</div>
      <Button size="sm" variant="ghost" onClick={clearSession} className="w-full justify-start">Sign out</Button>
    </div>
  );
  return (
    <div className="flex h-full">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-line bg-panel p-4 md:flex">
        <div className="mb-8">{brand}</div>
        <NavLinks />
        <div className="mt-auto">{footer}</div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex items-center justify-between border-b border-line bg-panel px-4 py-3 md:hidden">
          {brand}
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted">{nav.find((n) => (n.to === "/" ? path === "/" : path.startsWith(n.to)))?.label}</span>
            <button type="button" aria-expanded={open} aria-controls="mobile-nav" aria-label={open ? "Close menu" : "Open menu"} onClick={() => setOpen((o) => !o)}
              className="grid h-9 w-9 cursor-pointer place-items-center rounded-lg border border-line text-lg">{open ? "✕" : "☰"}</button>
          </div>
        </header>
        {open && (
          <div id="mobile-nav" className="border-b border-line bg-panel p-4 md:hidden">
            <NavLinks onPick={() => setOpen(false)} />
            <div className="mt-4">{footer}</div>
          </div>
        )}
        <main className="min-w-0 flex-1 overflow-y-auto px-4 py-5 md:p-8"><div className="mx-auto max-w-6xl"><Outlet /></div></main>
      </div>
    </div>
  );
}
