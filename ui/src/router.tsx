import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import Shell from "./pages/Shell";
import Overview from "./pages/Overview";
import Clients from "./pages/Clients";
import Events from "./pages/Events";
import ClientPage from "./client/ClientPage";

const root = createRootRoute({ component: Shell });
const index = createRoute({ getParentRoute: () => root, path: "/", component: Overview });
const clients = createRoute({ getParentRoute: () => root, path: "/clients", component: Clients });
const client = createRoute({
  getParentRoute: () => root, path: "/clients/$tenantId", component: ClientPage,
  validateSearch: (s: Record<string, unknown>): { tab: string } => ({ tab: typeof s.tab === "string" ? s.tab : "overview" }),
});
const events = createRoute({ getParentRoute: () => root, path: "/events", component: Events });

export const router = createRouter({ routeTree: root.addChildren([index, clients, client, events]) });
declare module "@tanstack/react-router" { interface Register { router: typeof router } }
