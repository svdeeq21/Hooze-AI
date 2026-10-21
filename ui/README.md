# Hooze Operator Dashboard

Internal control room for hooze-core: create clients, set up their assistant, connect WhatsApp, watch conversations, take over a chat, confirm bookings, run the kitchen, and see what's failing. Replaces the PowerShell commands.

Stack (same family as the company site): Vite · React 19 · TypeScript · TanStack Router + Query · Tailwind v4 · Framer Motion. Brand: amber `#F5A623` on near-black `#0E0E0F`, Syne + Outfit.

## Try it in 2 minutes (no keys, no WhatsApp, no database)
```bash
# terminal 1: the REAL API on seeded fake data (from the hooze-core folder)
python -m scripts.demo_server              # http://127.0.0.1:8000  key: demo-key
# terminal 2
cd ui && npm install && npm run dev        # http://127.0.0.1:5173
```
Sign in with API address `http://127.0.0.1:8000` and key `demo-key`. Two clients are pre-loaded (a property agency with a pending booking, a food business with a paid order). Everything resets when you stop the demo server; sends go to a recording fake.

## Use it with your real API
1. In hooze-core's `.env` set `CORS_ALLOWED_ORIGINS=http://127.0.0.1:5173` (comma-separate more; no `*`). Restart the API.
2. `npm run dev`, then sign in with the API address (your ngrok URL works) and your **platform key** (`PLATFORM_ADMIN_KEY`).
3. Run `migrations/0002_…sql` first if you haven't.

## Deploy later
`npm run build` produces `dist/` (static files; Vercel works, as for the main site). Put it on **HTTPS** and add its exact origin to `CORS_ALLOWED_ORIGINS`. Set `VITE_API_URL` to prefill the API address.

## Security notes
- The platform key can do anything to every client. It is kept in `sessionStorage` (cleared when the tab closes) and sent only as the `x-api-key` header. Use HTTPS, one operator, and rotate it if a laptop is lost. A per-client login (tenant keys already work on the `/v1/tenants/...` routes) is the next step before clients use this themselves.
- The app is `noindex`. Don't expose it publicly without also restricting access at the host.

## Tests
`npm run typecheck` · `npm test` (starts the real API on a seeded in-memory database on port 8765 and drives the real components through 14 workflows: sign-in, client creation, kitchen flow, sold-out, Paystack connect, human takeover, booking confirmation, config round-trip + go-live, WhatsApp provisioning, keys, pause/resume). Set `HOOZE_PYTHON` to your venv's python if `python3` lacks hooze-core's dependencies.

## Not verified
Visual layout/responsiveness (no browser was available when it was built: open it and look), the Evolution calls behind QR / webhook / create-instance (written to Evolution v2's API from documentation, never run against a live Evolution), and anything on a real phone.
