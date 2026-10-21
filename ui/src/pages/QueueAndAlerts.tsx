import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Overview } from "../lib/api";
import { ago, errMsg } from "../lib/util";
import { Badge, Button, Card, Empty, useToast } from "../components/ui";

export default function QueueAndAlerts({ overview }: { overview: Overview }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = overview.inbound;
  const failed = useQuery({ queryKey: ["inbound", "failed"], queryFn: () => api.inbound("failed"), enabled: q.failed > 0, refetchInterval: 15000 });
  const retry = useMutation({
    mutationFn: (id: string) => api.retryInbound(id),
    onSuccess: () => { toast("ok", "Back in the queue. It will be processed now."); qc.invalidateQueries(); },
    onError: (e) => toast("bad", errMsg(e)),
  });
  const test = useMutation({
    mutationFn: api.testAlert,
    onSuccess: (r) => {
      if (!r.configured) return toast("bad", "No alert destination is set up yet.");
      const bad = r.results.filter((x) => !x.ok);
      bad.length ? toast("bad", `Not delivered to: ${bad.map((b) => `${b.sink} (${b.error})`).join("; ")}`) : toast("ok", `Test alert sent to ${r.results.map((x) => x.sink).join(", ")}. Check your phone/inbox.`);
    },
    onError: (e) => toast("bad", errMsg(e)),
  });
  const inFlight = q.pending + q.processing;
  return (
    <div className="mt-5 grid gap-5 lg:grid-cols-2">
      <Card title="Message queue" action={q.failed > 0 ? <Badge tone="bad">{q.failed} failed</Badge> : <Badge tone="ok">healthy</Badge>}>
        <p className="mb-3 text-sm text-muted">Every customer message is saved the moment it arrives, so a restart can't lose it. {inFlight > 0 ? <>{inFlight} being processed or waiting to retry.</> : "Nothing waiting."}</p>
        {q.failed === 0 ? <Empty>No failed messages.</Empty> : (
          <ul className="divide-y divide-line">{(failed.data ?? []).map((r) => (
            <li key={r.id} className="flex items-start justify-between gap-3 py-2.5 text-sm">
              <div className="min-w-0"><div className="font-medium">{r.tenant_name ?? "Unknown client"} <span className="text-xs text-muted">· {ago(r.received_at)} · {r.attempts} attempts</span></div>
                <div className="truncate text-xs text-bad" title={r.last_error ?? ""}>{r.last_error ?? "no detail"}</div></div>
              <Button size="sm" busy={retry.isPending && retry.variables === r.id} onClick={() => retry.mutate(r.id)}>Retry</Button>
            </li>))}</ul>)}
      </Card>
      <Card title="Alerts" action={overview.alerts.configured ? <Badge tone="ok">on</Badge> : <Badge tone="bad">not set up</Badge>}>
        {overview.alerts.configured ? (
          <>
            <p className="mb-3 text-sm text-muted">You'll be messaged when something breaks (a message can't be processed, a payment looks wrong, WhatsApp disconnects…), once per problem, with what to do.</p>
            <ul className="mb-4 space-y-1 text-sm">{overview.alerts.destinations.map((d) => <li key={d} className="text-text">• {d}</li>)}</ul>
            <Button busy={test.isPending} onClick={() => test.mutate()}>Send test alert</Button>
          </>
        ) : (
          <div className="space-y-3 text-sm">
            <p className="rounded-lg border border-bad/30 bg-bad/10 p-3 text-bad">Alerts are not set up. If something breaks you will find out from a customer.</p>
            <p className="text-muted">Add one or both to the API's <code className="text-amber">.env</code> and restart:</p>
            <pre className="overflow-x-auto rounded-lg bg-ink p-3 text-xs text-muted">{`ALERT_WHATSAPP_NUMBERS=2348012345678\nALERT_EMAIL_TO=you@example.com\nRESEND_API_KEY=re_...`}</pre>
          </div>)}
      </Card>
    </div>
  );
}
