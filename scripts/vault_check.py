"""Which VAULT_MASTER_KEY encrypted the credentials already stored in the database?

Why this exists: losing/mixing up the vault key makes every stored credential (channel API keys, Paystack keys) unrecoverable.
A `.env` was found with a second, unnamed Fernet-format value below the real settings. Before rotating or deleting anything,
find out whether any stored row needs that other key.

Usage (from the repo root, with the normal .env present):
    python scripts/vault_check.py                      # tests the key in .env
    python scripts/vault_check.py <other_fernet_key>   # also tests extra candidate key(s)
Prints only OK / NOT-DECRYPTABLE per row and which candidate worked. It never prints keys or plaintext.
"""
import asyncio, sys

from hooze.kernel.db import get_client
from hooze.security import decrypts
from hooze.settings import get_settings


async def main(extra_keys: list[str]) -> int:
    s = get_settings()
    candidates = {"VAULT_MASTER_KEY (.env)": s.vault_master_key, **{f"extra #{i + 1}": k for i, k in enumerate(extra_keys)}}
    client = await get_client()
    rows = []
    for table in ("channels", "tenant_integrations"):
        for r in (await client.table(table).select("id, tenant_id, credentials_enc").execute()).data or []:
            if r.get("credentials_enc"):
                rows.append((table, r["id"], r["credentials_enc"]))
    if not rows:
        print("No encrypted credentials stored yet: the vault key can be changed freely right now.")
        return 0
    bad = 0
    for table, rid, token in rows:
        ok = [name for name, key in candidates.items() if key and decrypts(key, token)]
        print(f"{table:20} {rid}  ->  {'OK via ' + ', '.join(ok) if ok else 'NOT-DECRYPTABLE with any candidate'}")
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
