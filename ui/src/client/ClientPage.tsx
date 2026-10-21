import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, getRouteApi, useNavigate } from "@tanstack/react-router";
import { api } from "../lib/api";
import { ErrorBox, Loading, StatusBadge, Tabs } from "../components/ui";
import OverviewTab from "./OverviewTab";
import SetupTab from "./SetupTab";
import { ErrorBoundary } from "../components/ErrorBoundary";
import WhatsAppTab from "./WhatsAppTab";
import ConversationsTab from "./ConversationsTab";
import BookingsTab from "./BookingsTab";
import OrdersTab from "./OrdersTab";
import MenuTab from "./MenuTab";
import PaymentsTab from "./PaymentsTab";
import SettingsTab from "./SettingsTab";

const route = getRouteApi("/clients/$tenantId");

export default function ClientPage() {
  const { tenantId } = route.useParams();
  const { tab } = route.useSearch();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["tenant", tenantId], queryFn: () => api.tenant(tenantId), refetchInterval: 20000 });
  const stats = useQuery({ queryKey: ["stats", tenantId], queryFn: () => api.stats(tenantId), refetchInterval: 15000 });

  const goal = q.data?.active_config?.config.sales_process.goal;
  const hasBooking = !!q.data?.active_config && (goal === "consult_and_book" || q.data.active_config.config.booking.enabled);
  const tabs = useMemo(() => {
    const t: { id: string; label: string; badge?: number }[] = [
      { id: "overview", label: "Overview" }, { id: "setup", label: "Setup" }, { id: "whatsapp", label: "WhatsApp" },
      { id: "conversations", label: "Conversations", badge: stats.data?.conversations.handoff || undefined },
    ];
    if (hasBooking) t.push({ id: "bookings", label: "Bookings", badge: stats.data?.bookings.pending || undefined });
    if (goal === "order") t.push({ id: "orders", label: "Orders", badge: stats.data?.orders.PAID || undefined }, { id: "menu", label: "Menu" });
    // Always visible: it is where Paystack is connected, and that must be possible BEFORE going live
    // (a draft "order" config can't pass readiness without it, whatever the currently-live config is).
    t.push({ id: "payments", label: "Payments" });
    t.push({ id: "settings", label: "Settings" });
    return t;
  }, [hasBooking, goal, stats.data]);

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} retry={() => q.refetch()} />;
  const d = q.data!;
  const current = tabs.some((t) => t.id === tab) ? tab : "overview";
  return (
    <>
      <div className="mb-1 text-xs text-muted"><Link to="/clients" className="hover:text-amber">Clients</Link> / {d.tenant.name}</div>
      <div className="mb-5 flex items-center gap-3"><h1 className="font-display text-2xl font-bold">{d.tenant.name}</h1><StatusBadge status={d.tenant.status} /></div>
      <Tabs tabs={tabs} value={current} onChange={(t) => nav({ to: "/clients/$tenantId", params: { tenantId }, search: { tab: t } })} />
      {current === "overview" && <OverviewTab id={tenantId} detail={d} stats={stats.data} go={(t) => nav({ to: "/clients/$tenantId", params: { tenantId }, search: { tab: t } })} />}
      {current === "setup" && <ErrorBoundary label="The Setup tab" resetKey={tenantId}><SetupTab id={tenantId} detail={d} go={(t) => nav({ to: "/clients/$tenantId", params: { tenantId }, search: { tab: t } })} /></ErrorBoundary>}
      {current === "whatsapp" && <WhatsAppTab id={tenantId} detail={d} />}
      {current === "conversations" && <ConversationsTab id={tenantId} />}
      {current === "bookings" && <BookingsTab id={tenantId} tz={d.active_config?.config.identity.timezone ?? "Africa/Lagos"} />}
      {current === "orders" && <OrdersTab id={tenantId} currency={d.active_config?.config.identity.currency ?? "NGN"} />}
      {current === "menu" && <MenuTab id={tenantId} currency={d.active_config?.config.identity.currency ?? "NGN"} />}
      {current === "payments" && <PaymentsTab id={tenantId} />}
      {current === "settings" && <SettingsTab id={tenantId} detail={d} />}
    </>
  );
}
