-- Menu presentation: categories (so the menu can be shown in tidy sections) and photos/videos per product.
-- Safe to run more than once. Run this in the Supabase SQL editor BEFORE activating a config that uses categories or media.
alter table products add column if not exists category text  not null default '';
alter table products add column if not exists media    jsonb not null default '[]';   -- [{"type":"image"|"video","url":"https://..."}]
