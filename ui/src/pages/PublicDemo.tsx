// The page a prospect opens from a demo link: a live chat with a business's assistant (real AI replies), plus what happens behind the
// scenes. No sign-in: the link's token is the key, and the server caps what one link can spend.
import { useCallback, useEffect, useState } from "react";
import { demoApi, demoApiBase, type DemoInfo } from "../lib/api";
import { errMsg } from "../lib/util";
import ChatPanel from "../components/ChatPanel";
import { Spinner } from "../components/ui";
import { newSession } from "../client/TestChatTab";

const SITE = "https://ai.hoozeenterprises.com";

function sessionFor(token: string): string {
  const k = `hooze.demo.${token.slice(0, 12)}`;
  try { const v = localStorage.getItem(k); if (v) return v; const s = newSession(); localStorage.setItem(k, s); return s; } catch { return newSession(); }
}

export default function PublicDemo({ token }: { token: string }) {
  const [info, setInfo] = useState<DemoInfo | null>(null);
  const [err, setErr] = useState("");
  const [left, setLeft] = useState<number | null>(null);
  const [session, setSession] = useState(() => sessionFor(token));

  useEffect(() => {
    document.title = "Hooze live demo";
    if (!demoApiBase()) { setErr("This demo isn't set up on this site yet (the API address is missing)."); return; }
    demoApi.info(token).then((i) => { setInfo(i); setLeft(i.messages_left); document.title = `${i.business} · Hooze live demo`; }).catch((e) => setErr(errMsg(e)));
  }, [token]);

  const send = useCallback(async (t: string) => { const r = await demoApi.send(token, session, t); if (typeof r.messages_left === "number") setLeft(r.messages_left); return r; }, [token, session]);
  const pay = useCallback(() => demoApi.pay(token, session), [token, session]);
  const history = useCallback(() => demoApi.history(token, session), [token, session]);
  const restart = () => { const s = newSession(); try { localStorage.setItem(`hooze.demo.${token.slice(0, 12)}`, s); } catch { /* this tab only */ } setSession(s); };

  return (
    <div className="min-h-full bg-ink">
      <div className="mx-auto max-w-6xl px-4 py-6 md:py-10">
        <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <a href={SITE} target="_blank" rel="noopener" className="font-display text-lg font-bold">Hooze <span className="text-amber">AI</span></a>
          <span className="rounded-full border border-amber/40 bg-amber/10 px-3 py-1 text-xs text-amber">Live demo · real AI replies</span>
        </header>
        {err ? (
          <div className="mx-auto max-w-md rounded-2xl border border-line bg-panel p-6 text-center">
            <h1 className="font-display text-xl font-bold">This demo isn't available</h1>
            <p className="mt-2 text-sm text-muted">{err}</p>
            <a href={SITE} className="mt-4 inline-block text-sm font-medium text-amber underline" target="_blank" rel="noopener">Get in touch with Hooze</a>
          </div>
        ) : !info ? (
          <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted"><Spinner className="h-4 w-4" />Opening the demo…</div>
        ) : (
          <>
            <div className="mb-6 max-w-3xl">
              <h1 className="font-display text-2xl font-bold md:text-4xl" style={{ textWrap: "balance" }}>Chat with the {info.business} assistant</h1>
              <p className="mt-2 text-muted">{info.description ? `${info.description} ` : ""}Message it the way a customer would. Alongside the chat you'll see what it captures and what the owner's phone receives. Nothing is charged; payments here are pretend.</p>
            </div>
            <ChatPanel key={session} title={info.business} subtitle={left !== null ? `${left} demo message${left === 1 ? "" : "s"} left` : "replies instantly"}
              suggestions={info.suggestions} send={send} pay={pay} history={history} onNewChat={restart}
              disabled={info.live ? null : "This assistant is being updated. Try again in a few minutes."}
              footer={<p className="px-1 text-center text-[11px] text-muted">A demo by Hooze. Your messages are kept with this demo for the business that shared it.</p>} />
            <section className="mt-8 grid gap-4 rounded-2xl border border-line bg-panel p-5 md:grid-cols-[1fr_auto] md:items-center">
              <div><h2 className="font-display text-lg font-bold">Want this on your own WhatsApp number?</h2>
                <p className="text-sm text-muted">Hooze answers your customers in seconds, day and night: questions, orders, payments and bookings, and it hands over to your team when it should.</p></div>
              <a href={SITE} target="_blank" rel="noopener" className="inline-flex items-center justify-center rounded-lg bg-amber px-4 py-2.5 text-sm font-medium text-on-amber hover:bg-amber-dim">Talk to Hooze</a>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
