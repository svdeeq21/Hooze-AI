-- 0004: one round trip instead of ~25.
--
-- The dashboard's stats call issued ~25 separate COUNT queries per refresh (visible in the first real logs), and the client list issued
-- 2 per client (N+1). Both are now a single aggregate query. Same numbers, same shape; the Python side keeps the response format.

create or replace function tenant_stats(p_tenant_id uuid, p_since24 timestamptz, p_since7d timestamptz)
returns jsonb language sql stable as $$
  select jsonb_build_object(
    'conversations', (select coalesce(jsonb_object_agg(status, n), '{}') from (select status, count(*) n from conversations where tenant_id = p_tenant_id group by status) c),
    'orders',        (select coalesce(jsonb_object_agg(status, n), '{}') from (select status, count(*) n from orders where tenant_id = p_tenant_id group by status) o),
    'bookings',      (select coalesce(jsonb_object_agg(status, n), '{}') from (select status, count(*) n from bookings where tenant_id = p_tenant_id group by status) b),
    'inbound_24h',   (select count(*) from messages where tenant_id = p_tenant_id and direction = 'inbound' and created_at >= p_since24),
    'bot_24h',       (select count(*) from messages where tenant_id = p_tenant_id and direction = 'outbound' and sender_type = 'bot' and created_at >= p_since24),
    'revenue_total', (select coalesce(sum(subtotal_minor), 0) from orders where tenant_id = p_tenant_id and status in ('PAID', 'PREPARING', 'READY', 'COMPLETED')),
    'revenue_7d',    (select coalesce(sum(subtotal_minor), 0) from orders where tenant_id = p_tenant_id and status in ('PAID', 'PREPARING', 'READY', 'COMPLETED') and paid_at >= p_since7d)
  )
$$;

create or replace function platform_conversation_counts()
returns table (tenant_id uuid, total bigint, handoff bigint) language sql stable as $$
  select tenant_id, count(*), count(*) filter (where status = 'handoff') from conversations group by tenant_id
$$;

revoke all on function tenant_stats(uuid, timestamptz, timestamptz) from anon, authenticated, public;
revoke all on function platform_conversation_counts() from anon, authenticated, public;
