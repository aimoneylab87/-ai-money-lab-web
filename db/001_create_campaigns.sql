CREATE TABLE IF NOT EXISTS campaigns (
    id UUID PRIMARY KEY,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    destination_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_campaigns_customer_id
    ON campaigns(customer_id);

CREATE INDEX IF NOT EXISTS idx_campaigns_customer_status
    ON campaigns(customer_id, status);
