import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/util";
import { Badge, Button, Card, Empty, ErrorBox, Loading, PageHeader, Stat, StatusBadge, useToast } from "../components/ui";
import QueueAndAlerts from "./QueueAndAlerts";

export default function Overview() {
  const toast = useToast();
  const qc = useQueryClient();
  const ov = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 15000 });
  const tenants = useQuery({ queryKey: ["tenants"], queryFn: api.tenants, refetchInterval: 15000 });
  const tick = useMutation({
    mutationFn: api.tick,
    onSuccess: (r) => { toast("ok", r.status === "already_running" ? "A sweep is already running." : `Follow-up sweep finished for ${r.tenants} client(s).`); qc.invalidateQueries(); },
    onError: (e) => toast("bad", errMsg(e)),
  });
  if (ov.isLoading) return <Loading />;
  if (ov.error) return <ErrorBox error={ov.error} retry={() => ov.refetch()} />;
  const d = ov.data!;
  const allProvidersDown = d.llm_providers.length > 0 && d.llm_providers.every((p) => p.cooling_generate_s > 0);
  return (
    <>
      <PageHeader title="Overview" sub="Is everything healthy, and what needs attention?" action={
        <Button busy={tick.isPending} onClick={() => tick.mutate()} title="Runs expiry + follow-up checks across all clients now">Run follow-ups now</Button>} />
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Clients live" value={d.tenants.active} tone="ok" />
        <Stat label="Onboarding" value={d.tenants.onboarding} tone={d.tenants.onboarding ? "warn" : undefined} />
        <Stat label="Suspended" value={d.tenants.suspended} tone={d.tenants.suspended ? "bad" : undefined} />
        <Stat label="Environment" value={d.platform.app_env} hint={d.platform.shared_state} />
      </div>
      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="AI providers">
          {d.llm_providers.length === 0 ? <Empty>No provider information available.</Empty> : (
            <ul className="space-y-2">
              {d.llm_providers.map((p) => (
                <li key={p.name} className="flex items-center justify-between rounded-lg border border-line px-3 py-2 text-sm">
                  <div><div className="font-medium capitalize">{p.name}</div><div className="text-xs text-muted">{p.model ?? "—"}</div></div>
                  {p.cooling_generate_s > 0 ? <Badge tone="bad">failing — skipped for {p.cooling_generate_s}s</Badge> : <Badge tone="ok">healthy</Badge>}
                </li>))}
            </ul>)}
          {allProvidersDown && <p className="mt-3 text-sm text-bad">Every provider recently failed. Customers may be handed to a human. Check your Gemini/Groq keys and quotas.</p>}
          {d.llm_providers.length === 1 && <p className="mt-3 text-xs text-warn">Only one provider is configured, so there is no fallback if it goes down.</p>}
        </Card>
        <Card title="Recent problems" action={<Link to="/events" className="text-xs text-amber">All events →</Link>}>
          {d.recent_issues.length === 0 ? <Empty>Nothing wrong recently. 🎉</Empty> : (
            <ul className="space-y-2">{d.recent_issues.slice(0, 6).map((e, i) => (
              <li key={i} className="flex items-start justify-between gap-3 text-sm"><div><div className="font-medium">{e.event.replace(/_/g, " ").toLowerCase()}</div><div className="text-xs text-muted">{ago(e.created_at)}</div></div><StatusBadge status={e.severity} /></li>))}</ul>)}
        </Card>
      </div>
      <QueueAndAlerts overview={d} />
      <Card className="mt-5" title="Clients" action={<Link to="/clients" className="text-xs text-amber">Manage →</Link>}>
        {tenants.isLoading ? <Loading /> : !tenants.data?.length ? <Empty>No clients yet. <Link to="/clients" className="text-amber">Create the first one</Link>.</Empty> : (
          <ul className="divide-y divide-line">{tenants.data.map((t) => (
            <li key={t.id} className="flex items-center justify-between py-2.5 text-sm">
              <Link to="/clients/$tenantId" params={{ tenantId: t.id }} search={{ tab: "overview" }} className="font-medium hover:text-amber">{t.name}</Link>
              <div className="flex items-center gap-3">{t.handoff > 0 && <Badge tone="warn">{t.handoff} need a human</Badge>}{t.channels === 0 && <Badge tone="bad">no WhatsApp</Badge>}<StatusBadge status={t.status} /></div>
            </li>))}</ul>)}
      </Card>
      {!d.platform.nudges_globally_enabled && <p className="mt-4 text-xs text-warn">Follow-up messages are switched off platform-wide (NUDGES_GLOBALLY_ENABLED=false).</p>}
    </>
  );
}
