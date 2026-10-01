CREATE TABLE IF NOT EXISTS automation_executions (
    id UUID PRIMARY KEY,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    campaign_id UUID REFERENCES campaigns(id) ON DELETE CASCADE,
    decision_type TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_automation_executions_customer
    ON automation_executions(customer_id);

CREATE INDEX IF NOT EXISTS idx_automation_executions_campaign
    ON automation_executions(campaign_id);

CREATE INDEX IF NOT EXISTS idx_automation_executions_status
    ON automation_executions(status);
