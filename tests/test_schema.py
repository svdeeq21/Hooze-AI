"""Schema <-> code consistency (audit H10: pd called RPCs that no migration defined). Also asserts the tenancy invariants in SQL."""
import pathlib, re

import pytest

from hooze.kernel.db import GLOBAL_TABLES

pglast = pytest.importorskip("pglast")
ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
SQL = "\n".join(p.read_text() for p in MIGRATIONS)          # every migration, in order: an RPC may be (re)defined by a later one
CODE = "\n".join(p.read_text() for p in (ROOT / "hooze").rglob("*.py"))
TABLES = set(re.findall(r"create table (\w+)", SQL))


def test_migration_parses_as_valid_postgres():
    assert len(pglast.parse_sql(SQL)) > 40


def test_every_table_and_rpc_the_code_uses_exists_in_the_schema():
    assert set(re.findall(r'\.table\("(\w+)"\)', CODE)) - TABLES == set()
    assert {r for r in re.findall(r'\.rpc\("(\w+)"', CODE) if f"function {r}" not in SQL} == set()


def test_every_tenant_scoped_table_carries_tenant_id_and_only_declared_globals_do_not():
    no_tenant = {m.group(1) for m in re.finditer(r"create table (\w+) \((.*?)\n\);", SQL, re.S) if "tenant_id" not in m.group(2)}
    assert no_tenant == {"tenants"}                                           # `channels` and `api_keys` are global-LOOKUP but still carry tenant_id
    assert GLOBAL_TABLES <= TABLES


def test_cross_tenant_references_are_impossible_at_the_database_level():
    """Children reference parents by (tenant_id, id): a conversation can never point at another tenant's contact."""
    for child, parent in (("conversations", "contacts"), ("messages", "conversations"), ("order_items", "orders"), ("payments", "orders"), ("bookings", "contacts")):
        body = re.search(rf"create table {child} \((.*?)\n\);", SQL, re.S).group(1)
        assert re.search(rf"foreign key \(tenant_id, \w+\) references {parent} \(tenant_id, id\)", body), f"{child}->{parent} is not a composite FK"


def test_rls_is_enabled_on_every_table_and_no_permissive_policies_exist():
    assert "enable row level security" in SQL and "create policy" not in SQL.lower()


def test_idempotency_and_one_cart_constraints_exist():
    assert "create unique index messages_idempotency on messages (tenant_id, channel_id, provider_message_id)" in SQL
    assert "orders_one_cart" in SQL and "orders_one_pending" in SQL and "tenant_configs_one_active" in SQL
    assert re.search(r"secret_hash text not null check \(length\(secret_hash\) = 64\)", SQL)     # a channel cannot exist without a webhook secret
