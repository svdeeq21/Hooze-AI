-- 0002: two correctness fixes found by re-auditing 0001 against a real Postgres 16 + pgvector.
--
-- (1) activate_config_tx — config activation was TWO statements (archive old active, insert new active). A failure between them
--     left a tenant with NO active config: the pipeline ignores every message (NO_ACTIVE_CONFIG) and nothing alerts. Now it is one
--     transaction, and it stamps activated_at (which the old insert always wrote as NULL).
--
-- (2) match_knowledge — the H8 fix ("rank the WHOLE tenant corpus, then limit") held only while the planner avoided the HNSW index.
--     When it chose HNSW, the tenant filter was applied AFTER the approximate scan: measured on 12k rows / 41 tenants, a request for
--     20 records returned 2. Ranking now happens over a MATERIALIZED, tenant-filtered set, so it is exact by construction and does not
--     depend on planner statistics or the pgvector version. Cost is a per-tenant exact scan: fine to tens of thousands of records
--     per tenant; revisit (pgvector >= 0.8 iterative scan, or partitioning) beyond that. knowledge_embedding_idx is now unused by this
--     function; it is left in place (dropping it is a separate, reversible decision).

create or replace function activate_config_tx(p_tenant_id uuid, p_version int, p_config jsonb, p_compiled jsonb, p_report jsonb, p_created_by text)
returns setof tenant_configs
language plpgsql as $$
declare r tenant_configs;
begin
  update tenant_configs set status = 'archived' where tenant_id = p_tenant_id and status = 'active';
  insert into tenant_configs (tenant_id, version, status, config, compiled, validation_report, created_by, activated_at)
  values (p_tenant_id, p_version, 'active', p_config, p_compiled, coalesce(p_report, '{}'::jsonb), p_created_by, now())
  returning * into r;                       -- a version clash raises here and the archive above is rolled back with it
  return next r;
end $$;

create or replace function match_knowledge(p_tenant_id uuid, p_embedding vector(768), p_threshold float, p_count int, p_kind text default null)
returns table (id uuid, kind text, title text, content text, data jsonb, media jsonb, similarity float)
language sql stable as $$
  with mine as materialized (
    select k.id, k.kind, k.title, k.content, k.data, k.media, k.embedding
    from knowledge_items k
    where k.tenant_id = p_tenant_id and k.is_active and k.embedding is not null
      and (p_kind is null or k.kind = p_kind)
  )
  select m.id, m.kind, m.title, m.content, m.data, m.media, (1 - (m.embedding <=> p_embedding))::float
  from mine m
  where (1 - (m.embedding <=> p_embedding)) >= p_threshold
  order by m.embedding <=> p_embedding
  limit p_count
$$;

revoke all on function activate_config_tx(uuid, int, jsonb, jsonb, jsonb, text) from anon, authenticated, public;
