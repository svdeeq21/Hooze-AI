# Rotate every platform secret — do this once, today

**Why:** the project `.env` (containing live production secrets) was included in at least one zip of this repo that
left your machine. Treat every value in it as exposed. The code never leaked anything — the archive did. `.env` is
already in `.gitignore` and `.dockerignore`; the gap is hand-made zips. From now on build archives with
`git archive -o hooze-core.zip HEAD` (it only packs tracked files, so `.env` can never ride along).

Also fixed in this round: the `.env` contained a **bare Fernet key on its own line** (an old vault master key pasted
loose) and a duplicated `CORS_ALLOWED_ORIGINS`. Both removed. If that bare key ever encrypted anything, treat it as
exposed too.

Rotate in this order — each step says what breaks if you skip the follow-through.

## 1. Supabase service-role key (most powerful: full database, bypasses RLS)
- Supabase dashboard → Project Settings → API → rotate the `service_role` key.
- Update `SUPABASE_SERVICE_ROLE_KEY` on Render (and any local `.env`), redeploy.
- Until updated, the app cannot reach the database at all (`/health/ready` goes 503).

## 2. Vault master key (decrypts every client's Evolution + Paystack credentials)
Rotating this is not just "generate a new one" — existing ciphertexts must be re-encrypted:
1. Generate: `python -c "from hooze.security import Vault; print(Vault.generate_key())"`
2. Re-encrypt every `channels.credentials_enc` and `tenant_integrations.credentials_enc` row: decrypt with the old
   key, encrypt with the new (a 15-line script with `hooze.security.Vault`; run it against the DB once).
3. Set the new `VAULT_MASTER_KEY`, redeploy. `python scripts/vault_check.py` verifies every row decrypts.
- Skipping step 2 = every WhatsApp send and Paystack call fails with a decryption error.

## 3. Platform admin key
- Generate a long random value (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
- Set `PLATFORM_ADMIN_KEY`, redeploy, sign into the dashboard with the new key.

## 4. Internal cron secret
- Same generation; update `INTERNAL_CRON_SECRET` and the header your cron job sends (`x-cron-secret`).

## 5. LLM provider keys
- Google AI Studio → delete the old Gemini key, create a new one → `GEMINI_API_KEY`.
- Groq console → revoke + recreate → `GROQ_API_KEY`.

## 6. Tenant API keys (if any were in circulation)
- Dashboard → client → API keys: revoke and re-issue. Hashes only are stored, so nothing to rotate server-side beyond that.

## 7. Evolution API keys and webhook secrets
- If the Evolution server's global API key was ever in a shared `.env`, rotate it on the Evolution server, then
  update each channel (re-provision or edit the row's encrypted credential).
- Webhook secrets: dashboard → client → WhatsApp → **Reconnect webhook** rotates the per-channel secret in one click.

## 8. Upstash / Resend (when configured)
- Rotate the REST token / API key in their consoles; update env; redeploy.

## Afterwards
- `python scripts/vault_check.py` and `python scripts/llm_check.py` both pass.
- Dashboard → Overview → "Send test alert" delivers.
- Send one WhatsApp message end-to-end on a test number.
