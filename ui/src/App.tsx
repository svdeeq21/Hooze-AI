import { useEffect, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { getSession } from "./lib/api";
import { router } from "./router";
import { ToastProvider } from "./components/ui";
import Login from "./pages/Login";
import PublicDemo from "./pages/PublicDemo";

export const makeQueryClient = () => new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 3000 } } });

// /try/<token>: the public demo page for prospects. It needs no sign-in, so it is decided before the operator login.
const demoToken = (): string | null => {
  const m = window.location.pathname.match(/^\/try\/([A-Za-z0-9_-]{16,100})\/?$/);
  return m ? m[1] : null;
};

export function App({ client }: { client: QueryClient }) {
  const [authed, setAuthed] = useState(!!getSession());
  useEffect(() => { const h = () => { client.clear(); setAuthed(false); }; window.addEventListener("hooze:logout", h); return () => window.removeEventListener("hooze:logout", h); }, [client]);
  const token = demoToken();
  if (token) return <ToastProvider><PublicDemo token={token} /></ToastProvider>;
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>{authed ? <RouterProvider router={router} /> : <Login onDone={() => setAuthed(true)} />}</ToastProvider>
    </QueryClientProvider>
  );
}
