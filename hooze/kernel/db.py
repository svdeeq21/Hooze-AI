"""Tenant-scoped database access.

THE structural fix for the audit's H6 ("no multi-tenancy anywhere"). Instead of
trusting every query in ~150 call sites to remember `.eq("tenant_id", ...)`,
all tenant data goes through `TenantDB`, which:

  * injects `tenant_id` on every insert,
  * appends `.eq("tenant_id", ...)` to every select/update/delete,
  * refuses to touch global tables (those need `PlatformDB`, used only by
    code that legitimately runs *before* a tenant is known, e.g. resolving an
    inbound webhook's channel -> tenant).

A forgotten filter therefore becomes impossible to write, not merely unlikely.
The database backs this up with composite FKs + deny-all RLS (see migrations).
"""
from __future__ import annotations
from typing import Any, Optional

# Tables that are NOT scoped by tenant_id. Everything else is.
GLOBAL_TABLES = frozenset({"tenants", "channels", "api_keys"})


class TenantScopeError(RuntimeError):
    pass


def is_unique_violation(exc: BaseException) -> bool:
    """True ONLY for a Postgres unique-constraint violation (SQLSTATE 23505).

    Idempotency claims and get-or-create races rely on the DB unique index; anything ELSE that fails (connection reset,
    check-constraint, FK, timeout) is a real error and must never be reported as "duplicate" - that silently dropped
    customer messages. Matches supabase/postgrest-py (`.code`), the raw dict-in-message form, and Postgres' own text.
    """
    if getattr(exc, "code", None) == "23505":
        return True
    msg = str(exc).lower()
    return "23505" in msg or "duplicate key value violates unique constraint" in msg


def first(result: Any) -> Optional[dict]:
    """First row of a supabase-py result, or None. Avoids `.single()`, which raises on 0 rows."""
    data = getattr(result, "data", None)
    if not data:
        return None
    return data[0] if isinstance(data, list) else data


class _ScopedTable:
    def __init__(self, raw, tenant_id: str):
        self._raw, self._tid = raw, tenant_id

    def select(self, *a, **k):
        return self._raw.select(*a, **k).eq("tenant_id", self._tid)

    def insert(self, payload, **k):
        rows = payload if isinstance(payload, list) else [payload]
        scoped = [{**r, "tenant_id": self._tid} for r in rows]
        return self._raw.insert(scoped if isinstance(payload, list) else scoped[0], **k)

    def update(self, payload, **k):
        if "tenant_id" in payload and payload["tenant_id"] != self._tid:
            raise TenantScopeError("cannot re-parent a row to another tenant")
        return self._raw.update(payload, **k).eq("tenant_id", self._tid)

    def delete(self, *a, **k):
        return self._raw.delete(*a, **k).eq("tenant_id", self._tid)


class TenantDB:
    def __init__(self, client: Any, tenant_id: str):
        if not tenant_id:
            raise TenantScopeError("TenantDB requires a tenant_id")
        self._client, self.tenant_id = client, str(tenant_id)

    def table(self, name: str) -> _ScopedTable:
        if name in GLOBAL_TABLES:
            raise TenantScopeError(f"'{name}' is a global table; use PlatformDB")
        return _ScopedTable(self._client.table(name), self.tenant_id)

    def rpc(self, name: str, params: dict | None = None):
        # Every tenant-scoped RPC takes p_tenant_id as its first argument.
        return self._client.rpc(name, {"p_tenant_id": self.tenant_id, **(params or {})})


class PlatformDB:
    """Unscoped access. Only for global tables and platform jobs. Grep for it in review."""
    def __init__(self, client: Any):
        self.client = client

    def table(self, name: str):
        return self.client.table(name)

    def rpc(self, name: str, params: dict | None = None):
        return self.client.rpc(name, params or {})

    def for_tenant(self, tenant_id: str) -> TenantDB:
        return TenantDB(self.client, tenant_id)


_client: Any = None


async def get_client() -> Any:
    """Lazily-built cached *async* supabase client (the audit's C2 fix, kept)."""
    global _client
    if _client is None:
        from supabase import create_async_client  # lazy: tests never need the package
        from hooze.settings import get_settings
        s = get_settings()
        _client = await create_async_client(s.supabase_url, s.supabase_service_role_key)
    return _client
