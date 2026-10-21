import { useState } from "react";
import { api, getSession, setSession } from "../lib/api";
import { errMsg } from "../lib/util";
import { Button, Field, Input } from "../components/ui";

export default function Login({ onDone }: { onDone: () => void }) {
  const prev = getSession();
  const [baseUrl, setBase] = useState(prev?.baseUrl ?? (import.meta.env.VITE_API_URL as string | undefined) ?? "http://127.0.0.1:8000");
  const [key, setKey] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  async function go(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setErr("");
    setSession({ baseUrl, key });
    try { await api.overview(); onDone(); }
    catch (x) { sessionStorage.clear(); setErr(errMsg(x)); } finally { setBusy(false); }
  }
  return (
    <div className="grid min-h-full place-items-center p-6">
      <form onSubmit={go} className="w-full max-w-sm space-y-4 rounded-2xl border border-line bg-panel p-7">
        <div><div className="font-display text-xl font-bold">Hooze <span className="text-amber">Operator</span></div><p className="mt-1 text-sm text-muted">Sign in with your platform key.</p></div>
        <Field label="API address" hint="Your hooze-core server (or ngrok address)."><Input value={baseUrl} onChange={(e) => setBase(e.target.value)} placeholder="https://…" required /></Field>
        <Field label="Platform key" hint="Kept in this tab only; gone when you close it."><Input type="password" value={key} onChange={(e) => setKey(e.target.value)} required autoFocus /></Field>
        {err && <div className="rounded-lg border border-bad/30 bg-bad/10 p-3 text-sm text-bad">{err}</div>}
        <Button type="submit" variant="primary" busy={busy} className="w-full">Sign in</Button>
      </form>
    </div>
  );
}
