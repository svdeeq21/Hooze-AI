-- 0003: durable inbound queue.
--
-- Until now a WhatsApp message was acknowledged and then processed in an in-memory background task. A restart/deploy/crash in that
-- window lost the customer's message (Evolution does not retry). Now the webhook INSERTS the message here first (one row, deduped
-- by provider id), acknowledges, and processes it; anything left unfinished is re-claimed by a recovery loop.
--
-- Claiming is a single atomic statement (lease): two workers can never process the same row at once, and a worker that dies simply
-- lets its lease expire. `attempts` counts claims; after the configured maximum the row becomes 'failed' and an alert fires.

create table inbound_events (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references tenants(id) on delete cascade,
  channel_id          uuid not null references channels(id) on delete cascade,
  provider_message_id text,
  payload             jsonb not null,
  status              text not null default 'pending' check (status in ('pending', 'processing', 'done', 'failed')),
  attempts            int  not null default 0,
  locked_until        timestamptz,
  last_error          text,
  received_at         timestamptz not null default now(),
  finished_at         timestamptz
);
-- Evolution redelivering the same message must not create a second row
create unique index inbound_events_dedupe on inbound_events (channel_id, provider_message_id) where provider_message_id is not null;
create index inbound_events_open on inbound_events (received_at) where status in ('pending', 'processing');
create index inbound_events_done_age on inbound_events (finished_at) where status = 'done';

alter table inbound_events enable row level security;      -- server-side (service role) only, like every other table
revoke all on inbound_events from anon, authenticated;

-- claim ONE specific row (the webhook's own background task). Returns nothing if it is not claimable right now.
create or replace function claim_inbound_event(p_id uuid, p_lease_seconds int)
returns setof inbound_events language sql as $$
  update inbound_events
     set status = 'processing', attempts = attempts + 1, locked_until = now() + make_interval(secs => p_lease_seconds)
   where id = p_id and (status = 'pending' or (status = 'processing' and locked_until < now()))
  returning *
$$;

-- claim up to p_limit rows that nobody is working on: 'pending' older than p_min_age_seconds (so we never steal a row the webhook's own
-- task is about to process) and past any retry backoff, or 'processing' with an expired lease (the worker died). SKIP LOCKED => concurrent recoverers don't collide.
create or replace function claim_stale_inbound_events(p_limit int, p_lease_seconds int, p_min_age_seconds int)
returns setof inbound_events language sql as $$
  update inbound_events e
     set status = 'processing', attempts = e.attempts + 1, locked_until = now() + make_interval(secs => p_lease_seconds)
   where e.id in (
     select id from inbound_events
      where (status = 'pending' and received_at < now() - make_interval(secs => p_min_age_seconds)
             and (locked_until is null or locked_until < now()))             -- a retry's backoff is stored in locked_until
         or (status = 'processing' and locked_until < now())
      order by received_at
      limit p_limit
      for update skip locked)
  returning e.*
$$;

revoke all on function claim_inbound_event(uuid, int) from anon, authenticated, public;
revoke all on function claim_stale_inbound_events(int, int, int) from anon, authenticated, public;
