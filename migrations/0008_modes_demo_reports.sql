-- 0008: operating modes, the web test chat + shareable demo links, and the owner report log.
-- Run ONCE, BEFORE deploying the code that uses it.
--
-- * Assist mode: the assistant's replies wait in reply_drafts until a person approves (or edits) them in the dashboard.
-- * Test chat / demo links: a client can be tried in a browser with REAL AI replies and no WhatsApp. Those chats live on a
--   per-client channel of kind 'web_demo'. It is created with status 'pending' and never made 'active', so nothing that
--   sends WhatsApp messages, monitors numbers or checks readiness ever picks it up.
-- * demo_links: a shareable link for a prospect (token stored as sha256 only, expires, capped number of messages).
-- * report_log: one row per report actually reserved for sending, so a weekly report can never go out twice.

alter table channels drop constraint if exists channels_kind_check;
alter table channels add constraint channels_kind_check check (kind in ('whatsapp_evolution', 'web_demo'));

create table reply_drafts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  conversation_id uuid not null,
  contact_id uuid,
  kind text not null default 'text' check (kind in ('text', 'media')),
  content text not null default '',
  media_url text,
  media_type text,
  status text not null default 'pending' check (status in ('pending', 'sending', 'sent', 'discarded')),
  decided_by text,
  created_at timestamptz not null default now(),
  decided_at timestamptz,
  foreign key (tenant_id, conversation_id) references conversations (tenant_id, id) on delete cascade
);
create index if not exists reply_drafts_pending on reply_drafts (tenant_id, status, created_at);

create table demo_links (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  token_hash text not null unique check (length(token_hash) = 64),
  label text not null default '',
  max_messages int not null default 100 check (max_messages between 1 and 5000),
  used_messages int not null default 0,
  expires_at timestamptz not null,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

create table report_log (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  kind text not null,
  period text not null,
  created_at timestamptz not null default now(),
  unique (tenant_id, kind, period)
);

alter table reply_drafts enable row level security;
alter table demo_links enable row level security;
alter table report_log enable row level security;
revoke all on reply_drafts, demo_links, report_log from anon, authenticated;
