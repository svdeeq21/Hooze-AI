"""Spin up a demo client for a prospect from a ready-made template, in one command.

    python -m scripts.demo_tenant --template restaurant --name "Chops & Grill" --staff-phone 08012345678 \
        --api https://your-api.example.com --go-live --dashboard https://hooze-ai.vercel.app
    # the platform key is read from PLATFORM_ADMIN_KEY (never put it on the command line)

Templates live in templates/demo/: restaurant, retail (order); real_estate, clinic, salon, school (enquiry + booking).
What it does: creates the client, turns on its web test chat, and saves the template as its setup (renamed to the prospect's business,
with your phone as the staff number). With --go-live it also activates it (real AI, no WhatsApp needed: payments in the demo are
simulated) and prints a private demo link you can send the prospect. Nothing is ever sent on WhatsApp by this script.
"""
from __future__ import annotations
import argparse, asyncio, json, os, pathlib, re, sys

import httpx

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "templates" / "demo"


def build_config(template: str, name: str | None = None, staff_phone: str | None = None) -> dict:
    path = TEMPLATES / f"{template}.json"
    if not path.exists():
        raise SystemExit(f"No template '{template}'. Choose one of: {', '.join(sorted(p.stem for p in TEMPLATES.glob('*.json')))}")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if name:
        old = cfg["identity"]["name"]
        cfg["identity"]["name"] = name
        cfg["communication"]["greeting"] = cfg["communication"].get("greeting", "").replace(old, name)
    if staff_phone:
        cfg["handoff"]["staff_phones"] = [staff_phone]
    return cfg


def slugify(name: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "demo") + "-demo"


async def create_demo_tenant(http: httpx.AsyncClient, key: str, template: str, name: str | None = None, staff_phone: str | None = None,
                             go_live: bool = False, link_days: int = 14) -> dict:
    cfg = build_config(template, name, staff_phone)
    biz = cfg["identity"]["name"]
    h = {"x-api-key": key}
    r = await http.post("/v1/platform/tenants", json={"slug": slugify(biz), "name": biz}, headers=h)
    if r.status_code != 200:
        raise RuntimeError(f"Could not create the client ({r.status_code}): {r.text[:200]}")
    tenant = r.json()["tenant"]
    e = await http.post(f"/v1/tenants/{tenant['id']}/test-chat/enable", headers=h)
    if e.status_code != 200:
        raise RuntimeError(f"Client created, but turning on its test chat failed ({e.status_code}): {e.text[:200]}")
    d = await http.put(f"/v1/tenants/{tenant['id']}/config/draft", json=cfg, headers=h)
    if d.status_code != 200:
        raise RuntimeError(f"Client created, but saving the setup failed ({d.status_code}): {d.text[:200]}")
    report = d.json()["report"]
    out = {"tenant_id": tenant["id"], "slug": tenant["slug"], "name": biz, "live": False, "demo_token": None,
           "still_needed": [i["message"] for i in report["blocking"]], "completeness": report["completeness"]}
    if go_live and not out["still_needed"]:
        a = await http.post(f"/v1/tenants/{tenant['id']}/config/activate", json=cfg, headers=h)
        if a.status_code != 200:
            raise RuntimeError(f"Setup saved, but going live failed ({a.status_code}): {a.text[:200]}")
        link = await http.post(f"/v1/tenants/{tenant['id']}/demo-links", json={"label": f"{biz} demo", "days": link_days, "max_messages": 150}, headers=h)
        if link.status_code != 200:
            raise RuntimeError(f"Live, but creating the demo link failed ({link.status_code}): {link.text[:200]}")
        out.update(live=True, demo_token=link.json()["token"])
    return out


async def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template", required=True)
    ap.add_argument("--name", help="the prospect's business name")
    ap.add_argument("--staff-phone", help="YOUR WhatsApp number, so hand-offs during the demo reach you")
    ap.add_argument("--api", default=os.environ.get("HOOZE_API_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--go-live", action="store_true", help="activate it now and create a demo link (no WhatsApp needed)")
    ap.add_argument("--dashboard", default=os.environ.get("HOOZE_DASHBOARD_URL", "https://hooze-ai.vercel.app"), help="where the demo page is served")
    a = ap.parse_args()
    key = os.environ.get("PLATFORM_ADMIN_KEY")
    if not key:
        sys.exit("Set PLATFORM_ADMIN_KEY in the environment first.")
    async with httpx.AsyncClient(base_url=a.api.rstrip("/"), timeout=60.0) as http:
        out = await create_demo_tenant(http, key, a.template, a.name, a.staff_phone, go_live=a.go_live)
    print(f"\n  Demo client ready: {out['name']}  (slug {out['slug']}, id {out['tenant_id']})")
    if out["live"]:
        print(f"  It is LIVE with real AI replies. Send the prospect this link (expires in 14 days):\n\n    {a.dashboard.rstrip('/')}/try/{out['demo_token']}\n")
        print("  Or try it yourself: dashboard -> Clients -> this client -> Test chat. Pair WhatsApp later to put it on a real number.\n")
        return
    print(f"  Setup is {round(out['completeness'] * 100)}% complete. Still needed before Go live:")
    for m in out["still_needed"]:
        print(f"    - {m}")
    print("\n  Next: dashboard -> Clients -> this client -> Setup, fill in the rest, then Go live.\n")


if __name__ == "__main__":
    asyncio.run(_main())
