ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS objective TEXT,
    ADD COLUMN IF NOT EXISTS budget NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS channel TEXT,
    ADD COLUMN IF NOT EXISTS audience TEXT,
    ADD COLUMN IF NOT EXISTS offer TEXT,
    ADD COLUMN IF NOT EXISTS start_date DATE,
    ADD COLUMN IF NOT EXISTS end_date DATE;

CREATE INDEX IF NOT EXISTS idx_campaigns_customer_channel
    ON campaigns(customer_id, channel);

CREATE INDEX IF NOT EXISTS idx_campaigns_customer_objective
    ON campaigns(customer_id, objective);

CREATE INDEX IF NOT EXISTS idx_campaigns_dates
    ON campaigns(start_date, end_date);
