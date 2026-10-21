import { Link, Outlet } from "@tanstack/react-router";
import { clearSession, getSession } from "../lib/api";
import { Button } from "../components/ui";

const nav = [{ to: "/", label: "Overview", icon: "◎" }, { to: "/clients", label: "Clients", icon: "▤" }, { to: "/events", label: "Events", icon: "≋" }] as const;

export default function Shell() {
  const s = getSession();
  return (
    <div className="flex h-full">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-line bg-panel p-4 md:flex">
        <div className="mb-8 font-display text-lg font-bold">Hooze <span className="text-amber">Op</span></div>
        <nav className="flex flex-col gap-1">
          {nav.map((n) => (
            <Link key={n.to} to={n.to} activeOptions={{ exact: n.to === "/" }} className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted transition hover:bg-raised hover:text-text"
              activeProps={{ className: "bg-raised !text-amber" }}><span className="w-4 text-center">{n.icon}</span>{n.label}</Link>
          ))}
        </nav>
        <div className="mt-auto space-y-2 text-xs text-muted">
          <div className="truncate" title={s?.baseUrl}>{s?.baseUrl}</div>
          <Button size="sm" variant="ghost" onClick={clearSession} className="w-full justify-start">Sign out</Button>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-line px-4 py-3 md:hidden">
          <span className="font-display font-bold">Hooze <span className="text-amber">Op</span></span>
          <div className="flex gap-3 text-sm">{nav.map((n) => <Link key={n.to} to={n.to} className="text-muted" activeProps={{ className: "text-amber" }} activeOptions={{ exact: n.to === "/" }}>{n.label}</Link>)}</div>
        </header>
        <main className="min-w-0 flex-1 overflow-y-auto p-5 md:p-8"><div className="mx-auto max-w-6xl"><Outlet /></div></main>
      </div>
    </div>
  );
}
