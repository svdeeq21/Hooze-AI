import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { errMsg, naira } from "../lib/util";
import { Badge, Card, Empty, ErrorBox, Loading, Toggle, useToast } from "../components/ui";

export default function MenuTab({ id, currency }: { id: string; currency: string }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["products", id], queryFn: () => api.products(id) });
  const done = { onSuccess: () => qc.invalidateQueries({ queryKey: ["products", id] }), onError: (e: unknown) => { toast("bad", errMsg(e)); qc.invalidateQueries({ queryKey: ["products", id] }); } };
  const prod = useMutation({ mutationFn: (v: { id: string; available: boolean }) => api.setProductAvailable(id, v.id, v.available), ...done });
  const mod = useMutation({ mutationFn: (v: { id: string; available: boolean }) => api.setModifierAvailable(id, v.id, v.available), ...done });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} retry={() => q.refetch()} />;
  if (!q.data?.length) return <Empty>No menu yet. Add items under Setup and press Go live.</Empty>;
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">Switch an item off when it sells out. The assistant stops offering it straight away. To change names or prices, edit them under Setup.</p>
      {q.data.map((p) => (
        <Card key={p.id}>
          <div className="flex items-center justify-between gap-3">
            <div><div className="font-medium">{p.name} {!p.available && <Badge tone="bad">sold out</Badge>}</div><div className="text-sm text-muted">{naira(p.price_minor, currency)}</div></div>
            <Toggle checked={p.available} onChange={(v) => prod.mutate({ id: p.id, available: v })} label={p.available ? "Available" : "Sold out"} />
          </div>
          {p.groups.length > 0 && (
            <div className="mt-4 grid gap-4 border-t border-line pt-4 md:grid-cols-2">{p.groups.map((g) => (
              <div key={g.id}><div className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">{g.name}{g.required && " · required"}</div>
                <ul className="space-y-1.5">{g.modifiers.map((m) => (
                  <li key={m.id} className="flex items-center justify-between text-sm"><span className={m.available ? "" : "text-muted line-through"}>{m.name}{m.price_minor > 0 && <span className="ml-2 text-xs text-muted">+{naira(m.price_minor, currency)}</span>}</span>
                    <Toggle checked={m.available} onChange={(v) => mod.mutate({ id: m.id, available: v })} /></li>))}</ul></div>))}</div>)}
        </Card>))}
    </div>
  );
}
