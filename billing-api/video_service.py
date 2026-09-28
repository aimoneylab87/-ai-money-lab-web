import uuid


def create_video_job(
    cur,
    customer_id: str,
    prompt: str,
    provider: str,
    duration_seconds: int,
    resolution: str,
):
    job_id = str(uuid.uuid4())

    cur.execute(
        """
        INSERT INTO video_jobs (
            id,
            customer_id,
            prompt,
            provider,
            status,
            duration_seconds,
            resolution
        )
        VALUES (%s, %s, %s, %s, 'queued', %s, %s)
        RETURNING
            id,
            customer_id,
            prompt,
            provider,
            status,
            duration_seconds,
            resolution,
            created_at
        """,
        (
            job_id,
            customer_id,
            prompt,
            provider,
            duration_seconds,
            resolution,
        ),
    )

    row = cur.fetchone()

    return {
        "id": str(row[0]),
        "customer_id": str(row[1]),
        "prompt": row[2],
        "provider": row[3],
        "status": row[4],
        "duration_seconds": row[5],
        "resolution": row[6],
        "created_at": row[7].isoformat(),
    }
