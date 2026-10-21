import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { errMsg } from "../lib/util";
import { Badge, Button, Card, CopyField, Field, Input, Loading, useToast } from "../components/ui";

export default function PaymentsTab({ id }: { id: string }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["integrations", id], queryFn: () => api.integrations(id) });
  const [key, setKey] = useState("");
  const [domain, setDomain] = useState("hooze-orders.com");
  const connect = useMutation({
    mutationFn: () => api.connectPaystack(id, key.trim(), domain.trim()),
    onSuccess: () => { toast("ok", "Paystack connected."); setKey(""); qc.invalidateQueries({ queryKey: ["integrations", id] }); qc.invalidateQueries({ queryKey: ["tenant", id] }); },
    onError: (e) => toast("bad", errMsg(e)),
  });
  if (q.isLoading) return <Loading />;
  const ps = q.data?.find((i) => i.kind === "paystack");
  const hook = ps?.payment_webhook_url;
  const unreachable = hook && /localhost|127\.0\.0\.1|host\.docker\.internal/.test(hook);
  const live = key.startsWith("sk_live_");
  return (
    <div className="space-y-5">
      <Card title="Paystack" action={ps ? <Badge tone="ok">connected</Badge> : <Badge tone="warn">not connected</Badge>}>
        <form className="grid gap-4 md:grid-cols-2" onSubmit={(e) => { e.preventDefault(); connect.mutate(); }}>
          <Field label={ps ? "Replace secret key" : "Paystack secret key"} hint="Use a TEST key (sk_test_…) until you've seen a full test payment work." className="md:col-span-2">
            <Input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="sk_test_…" required autoComplete="off" /></Field>
          <Field label="Email domain for payers" hint="WhatsApp customers have no email; Paystack needs one, so we make up phone@this-domain."><Input value={domain} onChange={(e) => setDomain(e.target.value)} /></Field>
          <div className="flex items-end"><Button type="submit" variant="primary" busy={connect.isPending}>{ps ? "Update key" : "Connect Paystack"}</Button></div>
        </form>
        {live && <p className="mt-3 rounded-lg border border-warn/30 bg-warn/10 p-3 text-sm text-warn">This is a LIVE key: real customers' money will move. Make sure you've completed a test-mode payment first.</p>}
        <p className="mt-3 text-xs text-muted">The key is stored encrypted and never shown again.</p>
      </Card>
      {hook && (
        <Card title="Tell Paystack where to send payment confirmations">
          <ol className="mb-4 list-decimal space-y-1 pl-5 text-sm text-muted"><li>Open the Paystack dashboard → Settings → API Keys &amp; Webhooks.</li><li>Paste this address into <b className="text-text">Test Webhook URL</b> (and Live later).</li><li>Save. Payments will now update orders automatically.</li></ol>
          <CopyField value={hook} />
          {unreachable && <p className="mt-3 rounded-lg border border-bad/30 bg-bad/10 p-3 text-sm text-bad">This address only works on your own computer; Paystack can't reach it. Set the server's PUBLIC_BASE_URL to your public (ngrok) address, restart, and come back.</p>}
        </Card>)}
    </div>
  );
}
