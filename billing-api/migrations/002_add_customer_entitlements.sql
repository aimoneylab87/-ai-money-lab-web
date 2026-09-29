ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free',
    ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'inactive';

UPDATE customers
SET
    plan = COALESCE(plan, 'free'),
    subscription_status = COALESCE(subscription_status, 'inactive');
