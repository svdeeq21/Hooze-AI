import { useEffect, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { getSession } from "./lib/api";
import { router } from "./router";
import { ToastProvider } from "./components/ui";
import Login from "./pages/Login";

export const makeQueryClient = () => new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 3000 } } });

export function App({ client }: { client: QueryClient }) {
  const [authed, setAuthed] = useState(!!getSession());
  useEffect(() => { const h = () => { client.clear(); setAuthed(false); }; window.addEventListener("hooze:logout", h); return () => window.removeEventListener("hooze:logout", h); }, [client]);
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>{authed ? <RouterProvider router={router} /> : <Login onDone={() => setAuthed(true)} />}</ToastProvider>
    </QueryClientProvider>
  );
}
