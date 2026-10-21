-- Hooze platform schema (multi-tenant from row one).
--
-- Fixes vs the two source schemas:
--  * pd had NO tenant column anywhere (audit H6); savoury had org_id but nothing enforced it.
--    Here every child row carries tenant_id AND the FK is COMPOSITE (tenant_id, parent_id) -> parent(tenant_id, id),
--    so a row can never point at another tenant's parent even if application code is wrong.
--  * CHECK constraints on every status/enum column (audit M-series: free-text status columns).
--  * Money is bigint minor units (kobo). No numeric/float.
--  * Idempotency is a partial UNIQUE index on (tenant_id, channel_id, provider_message_id).
--  * RLS enabled everywhere with NO policies: only the service role (which bypasses RLS) can touch data.
--  * Every RPC referenced by application code is defined here (audit H10: pd's RPCs were missing from migrations).

create extension if not exists pgcrypto;
create extension if not exists vector;

create or replace function set_updated_at() returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

-- ───────────────────────── platform (global) tables ─────────────────────────
create table tenants (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique check (slug ~ '^[a-z0-9-]{2,60}$'),
  name text not null,
  status text not null default 'onboarding' check (status in ('onboarding','active','suspended')),
  plan text not null default 'pilot',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table channels (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  kind text not null check (kind in ('whatsapp_evolution')),
  instance_name text not null,
  config jsonb not null default '{}',
  credentials_enc text,                                  -- Fernet-encrypted provider API key
  secret_hash text not null check (length(secret_hash) = 64),  -- sha256 of the webhook secret: a channel CANNOT exist without one (audit C1)
  status text not null default 'pending' check (status in ('pending','active','disabled')),
  created_at timestamptz not null default now(),
  unique (kind, instance_name),
  unique (tenant_id, id)
);

create table api_keys (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  key_hash text not null unique,
  label text not null default '',                        -- one key per person/service => per-actor attribution (audit M-series)
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

-- ───────────────────────── tenant configuration ─────────────────────────
create table tenant_configs (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  version int not null,
  status text not null check (status in ('draft','active','archived')),
  config jsonb not null,                                 -- BusinessConfig (what onboarding produced)
  compiled jsonb not null,                               -- RuntimeConfig (what the pipeline executes)
  validation_report jsonb not null default '{}',
  created_by text,
  created_at timestamptz not null default now(),
  activated_at timestamptz,
  unique (tenant_id, version)
);
create unique index tenant_configs_one_active on tenant_configs (tenant_id) where status = 'active';

create table tenant_integrations (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  kind text not null check (kind in ('paystack')),
  credentials_enc text not null,
  config jsonb not null default '{}',
  status text not null default 'active' check (status in ('active','disabled')),
  created_at timestamptz not null default now(),
  unique (tenant_id, kind)
);

-- ───────────────────────── conversations ─────────────────────────
create table contacts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  phone text not null,
  display_name text,
  attributes jsonb not null default '{}',                -- slots live under attributes.slots
  ai_paused boolean not null default false,
  opted_out boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, phone),
  unique (tenant_id, id)
);

create table conversations (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  contact_id uuid not null,
  channel_id uuid not null,
  state text not null,
  status text not null default 'active' check (status in ('active','handoff','closed')),
  handoff_reason text,
  summary text not null default '',
  summary_upto int not null default 0,
  user_message_count int not null default 0,
  message_count int not null default 0,
  last_inbound_at timestamptz,
  last_outbound_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, contact_id) references contacts (tenant_id, id) on delete cascade,
  foreign key (tenant_id, channel_id) references channels (tenant_id, id) on delete cascade,
  unique (tenant_id, contact_id, channel_id),
  unique (tenant_id, id)
);
create index conversations_state_idx on conversations (tenant_id, state, status, last_inbound_at);

create table messages (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  conversation_id uuid not null,
  contact_id uuid,
  channel_id uuid,
  direction text not null check (direction in ('inbound','outbound')),
  sender_type text not null check (sender_type in ('contact','bot','human','system')),
  content text not null default '',
  content_type text not null default 'text',
  provider_message_id text,
  meta jsonb not null default '{}',
  created_at timestamptz not null default now(),
  foreign key (tenant_id, conversation_id) references conversations (tenant_id, id) on delete cascade
);
create unique index messages_idempotency on messages (tenant_id, channel_id, provider_message_id) where provider_message_id is not null;
create index messages_conversation_idx on messages (conversation_id, created_at);

create table nudge_state (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  conversation_id uuid not null,
  status text not null default 'ACTIVE' check (status in ('ACTIVE','IDLE','EXHAUSTED','OPTED_OUT','LOST')),
  step int not null default 0 check (step >= 0),
  last_nudge_at timestamptz,
  next_nudge_at timestamptz,
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, conversation_id) references conversations (tenant_id, id) on delete cascade,
  unique (conversation_id)
);

-- ───────────────────────── knowledge (RAG) ─────────────────────────
create table knowledge_items (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  kind text not null default 'general',
  title text not null,
  content text not null default '',
  data jsonb not null default '{}',
  media jsonb not null default '[]',
  embedding vector(768),
  content_hash text,
  is_active boolean not null default true,
  source text not null default 'config',
  external_id text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, source, external_id)
);
create index knowledge_embedding_idx on knowledge_items using hnsw (embedding vector_cosine_ops);
create index knowledge_tenant_idx on knowledge_items (tenant_id, is_active);

-- ───────────────────────── catalog + orders ─────────────────────────
create table products (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  name text not null,
  description text not null default '',
  price_minor bigint not null check (price_minor >= 0),
  available boolean not null default true,
  sort_order int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, id)
);
create table modifier_groups (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  product_id uuid not null,
  name text not null,
  selection text not null default 'single' check (selection in ('single','multiple')),
  required boolean not null default false,
  max_selections int check (max_selections is null or max_selections > 0),
  sort_order int not null default 0,
  foreign key (tenant_id, product_id) references products (tenant_id, id) on delete cascade,
  unique (tenant_id, id)
);
create table modifiers (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  group_id uuid not null,
  name text not null,
  price_minor bigint not null default 0 check (price_minor >= 0),
  available boolean not null default true,
  sort_order int not null default 0,
  foreign key (tenant_id, group_id) references modifier_groups (tenant_id, id) on delete cascade
);

create table orders (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  contact_id uuid not null,
  status text not null default 'CART' check (status in ('CART','PAYMENT_PENDING','PAID','PREPARING','READY','COMPLETED','CANCELLED','EXPIRED')),
  subtotal_minor bigint not null default 0 check (subtotal_minor >= 0),
  delivery_fee_minor bigint not null default 0 check (delivery_fee_minor >= 0),
  fulfillment_method text check (fulfillment_method in ('PICKUP','DELIVERY')),
  delivery_address text,
  delivery_area text,
  draft jsonb,                                           -- persisted partial item (savoury's key fix)
  paid_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, contact_id) references contacts (tenant_id, id) on delete cascade,
  unique (tenant_id, id)
);
create unique index orders_one_cart on orders (tenant_id, contact_id) where status = 'CART';
create unique index orders_one_pending on orders (tenant_id, contact_id) where status = 'PAYMENT_PENDING';

create table order_items (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  order_id uuid not null,
  product_id uuid,
  product_name text not null,
  unit_price_minor bigint not null check (unit_price_minor >= 0),
  quantity int not null check (quantity > 0),
  modifiers jsonb not null default '[]',                 -- snapshot: later menu edits never rewrite history
  line_total_minor bigint not null check (line_total_minor >= 0),
  created_at timestamptz not null default now(),
  foreign key (tenant_id, order_id) references orders (tenant_id, id) on delete cascade
);

create table payments (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  order_id uuid not null,
  provider text not null,
  reference text not null unique,
  status text not null default 'pending' check (status in ('pending','success','failed')),
  amount_minor bigint not null check (amount_minor >= 0),
  currency text not null default 'NGN',
  authorization_url text,
  verified_at timestamptz,
  created_at timestamptz not null default now(),
  foreign key (tenant_id, order_id) references orders (tenant_id, id) on delete cascade
);

create table availability (
  tenant_id uuid primary key references tenants(id) on delete cascade,
  status text not null default 'OPEN' check (status in ('OPEN','PAUSED','CLOSED')),
  pause_reason text,
  pause_message text,
  updated_at timestamptz not null default now()
);

-- ───────────────────────── bookings ─────────────────────────
create table bookings (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  contact_id uuid not null,
  conversation_id uuid not null,
  subject text not null,
  preferred_text text not null default '',               -- the customer's own words
  suggested_at timestamptz,                              -- a parsed GUESS; never a booking time until a human confirms (audit M4)
  confirmed_at timestamptz,
  status text not null default 'pending' check (status in ('pending','confirmed','declined','cancelled')),
  staff_notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (tenant_id, contact_id) references contacts (tenant_id, id) on delete cascade,
  foreign key (tenant_id, conversation_id) references conversations (tenant_id, id) on delete cascade
);
create index bookings_pending_idx on bookings (tenant_id, status, created_at);

-- ───────────────────────── observability + metering ─────────────────────────
create table events (
  id bigint generated always as identity primary key,
  tenant_id uuid,                                        -- no FK: platform events and deleted tenants must still log
  event text not null,
  severity text not null default 'INFO' check (severity in ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
  ref_type text,
  ref_id text,
  trace_id text,
  metadata jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index events_tenant_time_idx on events (tenant_id, created_at desc);
create index events_name_idx on events (event, created_at desc);

create table usage_events (
  id bigint generated always as identity primary key,
  tenant_id uuid not null references tenants(id) on delete cascade,
  kind text not null,                                    -- llm_call | embedding | message_out | ...
  quantity numeric not null default 1,
  metadata jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index usage_tenant_time_idx on usage_events (tenant_id, created_at);

-- ───────────────────────── updated_at triggers ─────────────────────────
create trigger t_tenants_upd before update on tenants for each row execute function set_updated_at();
create trigger t_contacts_upd before update on contacts for each row execute function set_updated_at();
create trigger t_conversations_upd before update on conversations for each row execute function set_updated_at();
create trigger t_nudge_upd before update on nudge_state for each row execute function set_updated_at();
create trigger t_knowledge_upd before update on knowledge_items for each row execute function set_updated_at();
create trigger t_products_upd before update on products for each row execute function set_updated_at();
create trigger t_orders_upd before update on orders for each row execute function set_updated_at();
create trigger t_bookings_upd before update on bookings for each row execute function set_updated_at();
create trigger t_availability_upd before update on availability for each row execute function set_updated_at();

-- ───────────────────────── RPCs ─────────────────────────
-- Ranking happens over the WHOLE tenant corpus (audit H8: pd capped at LIMIT 20 *before* scoring).
create or replace function match_knowledge(p_tenant_id uuid, p_embedding vector(768), p_threshold float, p_count int, p_kind text default null)
returns table (id uuid, kind text, title text, content text, data jsonb, media jsonb, similarity float)
language sql stable as $$
  select k.id, k.kind, k.title, k.content, k.data, k.media, (1 - (k.embedding <=> p_embedding))::float
  from knowledge_items k
  where k.tenant_id = p_tenant_id and k.is_active and k.embedding is not null
    and (p_kind is null or k.kind = p_kind)
    and (1 - (k.embedding <=> p_embedding)) >= p_threshold
  order by k.embedding <=> p_embedding
  limit p_count
$$;

-- Booking decision + transcript entry commit together or not at all (audit H5). Only a 'pending' booking can be decided,
-- which also makes a double-click / retry a harmless no-op.
create or replace function confirm_booking_tx(p_tenant_id uuid, p_booking_id uuid, p_status text, p_confirmed_at timestamptz, p_notes text)
returns setof bookings
language plpgsql as $$
declare b bookings;
begin
  if p_status not in ('confirmed','declined') then
    raise exception 'invalid status %', p_status;
  end if;
  update bookings
     set status = p_status,
         confirmed_at = case when p_status = 'confirmed' then p_confirmed_at else null end,
         staff_notes = p_notes
   where id = p_booking_id and tenant_id = p_tenant_id and status = 'pending'
   returning * into b;
  if not found then
    return;
  end if;
  insert into messages (tenant_id, conversation_id, direction, sender_type, content, content_type, meta)
  values (p_tenant_id, b.conversation_id, 'outbound', 'system', 'Booking ' || p_status || ': ' || b.subject, 'system',
          jsonb_build_object('booking_id', b.id));
  return next b;
end $$;

-- ───────────────────────── RLS: deny by default ─────────────────────────
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

revoke all on all tables in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated, public;
