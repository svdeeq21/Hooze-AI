-- 0006: roles for tenant API keys (least privilege).
--   admin  : everything inside the tenant (activate configs, connect Paystack, upload media, reset nudges)
--   staff  : day-to-day work (chats, orders, bookings, sold-out toggles, reply-as-human) - cannot change configuration or payment keys
--   viewer : read-only (GET only)
-- Existing keys become 'staff' on purpose: a client's key must not be able to change the Paystack key or activate configs.
-- Run this BEFORE deploying the code that reads/writes the column.
alter table api_keys add column if not exists role text not null default 'staff';
alter table api_keys drop constraint if exists api_keys_role_check;
alter table api_keys add constraint api_keys_role_check check (role in ('admin','staff','viewer'));
