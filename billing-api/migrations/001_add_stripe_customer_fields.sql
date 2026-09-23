ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
    ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT,
    ADD COLUMN IF NOT EXISTS stripe_price_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_stripe_customer_id
    ON customers(stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_customers_stripe_subscription_id
    ON customers(stripe_subscription_id)
    WHERE stripe_subscription_id IS NOT NULL;
