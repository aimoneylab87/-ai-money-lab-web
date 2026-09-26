import os

import psycopg

from provider_client import (
    VideoProviderError,
    get_video_provider,
)
from storage import VideoStorage


def get_connection():
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        sslmode=os.environ.get("POSTGRES_SSLMODE", "require"),
    )


def get_job(cur, job_id: str):
    cur.execute(
        """
        SELECT
            id,
            customer_id,
            prompt,
            provider,
            status,
            duration_seconds,
            resolution
        FROM video_jobs
        WHERE id = %s
        """,
        (job_id,),
    )

    row = cur.fetchone()

    if not row:
        raise ValueError(f"Video job not found: {job_id}")

    return {
        "id": str(row[0]),
        "customer_id": str(row[1]),
        "prompt": row[2],
        "provider": row[3],
        "status": row[4],
        "duration_seconds": row[5],
        "resolution": row[6],
    }


def mark_processing(cur, job_id: str):
    cur.execute(
        """
        UPDATE video_jobs
        SET
            status = 'processing',
            started_at = COALESCE(started_at, NOW()),
            error_message = NULL
        WHERE id = %s
          AND status = 'queued'
        """,
        (job_id,),
    )

    return cur.rowcount == 1


def mark_completed(
    cur,
    job_id: str,
    video_url: str,
    seconds_generated: int,
):
    cur.execute(
        """
        UPDATE video_jobs
        SET
            status = 'completed',
            video_url = %s,
            completed_at = NOW(),
            error_message = NULL
        WHERE id = %s
        """,
        (video_url, job_id),
    )

    cur.execute(
        """
        INSERT INTO video_usage (
            customer_id,
            video_job_id,
            seconds_generated
        )
        SELECT
            customer_id,
            id,
            %s
        FROM video_jobs
        WHERE id = %s
        """,
        (seconds_generated, job_id),
    )


def mark_failed(cur, job_id: str, error_message: str):
    cur.execute(
        """
        UPDATE video_jobs
        SET
            status = 'failed',
            error_message = %s
        WHERE id = %s
        """,
        (error_message[:4000], job_id),
    )


def process_video_job(job_id: str):
    storage = VideoStorage()

    with get_connection() as conn:
        with conn.cursor() as cur:
            job = get_job(cur, job_id)

            if job["status"] == "completed":
                print(f"VIDEO_JOB_ALREADY_COMPLETED: {job_id}")
                return

            if job["status"] != "queued":
                raise ValueError(
                    f"Video job {job_id} has status "
                    f"{job['status']}"
                )

            if not mark_processing(cur, job_id):
                raise RuntimeError(
                    f"Unable to mark video job as processing: {job_id}"
                )

            conn.commit()

    try:
        provider = get_video_provider(job["provider"])

        content = provider.generate_video(
            prompt=job["prompt"],
            duration_seconds=job["duration_seconds"],
            resolution=job["resolution"],
        )

        video_url = storage.upload_video(
            job_id=job["id"],
            content=content,
        )

        with get_connection() as conn:
            with conn.cursor() as cur:
                mark_completed(
                    cur=cur,
                    job_id=job["id"],
                    video_url=video_url,
                    seconds_generated=job["duration_seconds"],
                )

            conn.commit()

        print(
            f"VIDEO_JOB_COMPLETED: "
            f"{job['id']} -> {video_url}"
        )

    except Exception as exc:
        error_message = (
            f"{type(exc).__name__}: {exc}"
        )

        with get_connection() as conn:
            with conn.cursor() as cur:
                mark_failed(
                    cur=cur,
                    job_id=job["id"],
                    error_message=error_message,
                )

            conn.commit()

        print(
            f"VIDEO_JOB_FAILED: "
            f"{job['id']} -> {error_message}"
        )

        raise


if __name__ == "__main__":
    job_id = os.environ.get("VIDEO_JOB_ID")

    if not job_id:
        raise SystemExit(
            "VIDEO_JOB_ID environment variable is required"
        )

    process_video_job(job_id)
