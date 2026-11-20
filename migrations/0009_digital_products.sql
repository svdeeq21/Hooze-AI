-- 0009: digital products (e-books, courses, templates, tickets...).
-- Run ONCE, BEFORE deploying the code that uses it.
--
-- * products.delivery_url: what the customer receives automatically the moment their payment is verified (a download link or a
--   PDF). It is never shown to the AI or the customer before payment.
-- * orders.fulfillment_method gains 'DIGITAL': an order made only of digital products needs no pickup or delivery.

alter table products add column if not exists delivery_url text not null default '';

alter table orders drop constraint if exists orders_fulfillment_method_check;
alter table orders add constraint orders_fulfillment_method_check check (fulfillment_method in ('PICKUP', 'DELIVERY', 'DIGITAL'));
