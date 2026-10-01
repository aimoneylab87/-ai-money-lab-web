ALTER TABLE traffic_events
    ALTER COLUMN visitor_id DROP NOT NULL,
    ALTER COLUMN event_type DROP NOT NULL;

ALTER TABLE traffic_events
    ADD COLUMN IF NOT EXISTS event TEXT,
    ADD COLUMN IF NOT EXISTS content TEXT,
    ADD COLUMN IF NOT EXISTS page_url TEXT,
    ADD COLUMN IF NOT EXISTS session_id TEXT,
    ADD COLUMN IF NOT EXISTS destination TEXT,
    ADD COLUMN IF NOT EXISTS revenue NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS currency TEXT,
    ADD COLUMN IF NOT EXISTS cost NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS customer_id UUID;

CREATE INDEX IF NOT EXISTS idx_traffic_customer
    ON traffic_events(customer_id);

CREATE INDEX IF NOT EXISTS idx_traffic_event
    ON traffic_events(event);

CREATE INDEX IF NOT EXISTS idx_traffic_campaign
    ON traffic_events(campaign);

CREATE INDEX IF NOT EXISTS idx_traffic_session
    ON traffic_events(session_id);
