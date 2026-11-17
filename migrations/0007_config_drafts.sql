-- 0007: setup drafts.
--
-- Onboarding could only SAVE by going live, and going live is (correctly) blocked until WhatsApp / payments are
-- connected — so business setup done before connecting anything lived only in the operator's browser and was lost on
-- refresh. tenant_configs has allowed status='draft' since 0001; the application now uses it: one draft per tenant,
-- stored as version 0 (activations start at 1, so it can never collide), updated in place.
--
-- Schema change needed: a save timestamp. tenant_configs had created_at/activated_at but nothing that moves when a
-- draft row is UPDATED. The application writes updated_at explicitly on each draft save; the trigger keeps it right
-- even for writes that forget.

alter table tenant_configs add column if not exists updated_at timestamptz not null default now();
drop trigger if exists t_tenant_configs_upd on tenant_configs;
create trigger t_tenant_configs_upd before update on tenant_configs for each row execute function set_updated_at();

-- Belt and braces: the application enforces one draft per tenant by updating in place; this makes a race (two tabs
-- saving the very first draft at once) harmless at the database level too.
create unique index if not exists tenant_configs_one_draft on tenant_configs (tenant_id) where status = 'draft';
