-- 0010: scale fixes. Run ONCE, BEFORE deploying the code that uses it (the code also works without it, just without the benefit).
--
-- * inbound_events.contact_phone: lets a customer's rapid-fire messages ("Hi" / "I want a house" / "in Lekki") be answered together in ONE
--   reply instead of one reply each. Without the column the code falls back to answering each message separately.
-- * events: the log table had no clean-up and grows with every message; the code now deletes old rows hourly. These indexes keep that
--   clean-up and the dashboard's "recent problems" query fast as the table grows.

alter table inbound_events add column if not exists contact_phone text;
create index if not exists inbound_events_by_contact on inbound_events (channel_id, contact_phone, received_at) where status in ('pending', 'processing');

create index if not exists events_severity_time_idx on events (severity, created_at);
