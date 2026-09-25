CREATE TABLE IF NOT EXISTS video_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    prompt TEXT NOT NULL,
    provider VARCHAR(100),
    provider_job_id VARCHAR(255),
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    duration_seconds INTEGER,
    resolution VARCHAR(32),
    video_url TEXT,
    thumbnail_url TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_video_jobs_customer_id
    ON video_jobs(customer_id);

CREATE INDEX IF NOT EXISTS idx_video_jobs_status
    ON video_jobs(status);

CREATE TABLE IF NOT EXISTS video_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    video_job_id UUID NOT NULL REFERENCES video_jobs(id) ON DELETE CASCADE,
    seconds_generated INTEGER NOT NULL DEFAULT 0,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_video_usage_customer_id_timestamp
    ON video_usage(customer_id, timestamp);
