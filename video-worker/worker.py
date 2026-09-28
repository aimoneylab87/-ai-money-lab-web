import json
import os

import psycopg
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient

from provider_client import (
    VideoProviderError,
    get_video_provider,
)
from storage import VideoStorage


SERVICE_BUS_NAMESPACE = os.environ.get(
    "SERVICE_BUS_NAMESPACE",
    "sb-ai-money-lab.servicebus.windows.net",
)

SERVICE_BUS_QUEUE = os.environ.get(
    "SERVICE_BUS_QUEUE",
    "video-generation",
)

MAX_DELIVERY_ATTEMPTS = int(
    os.environ.get("MAX_DELIVERY_ATTEMPTS", "5")
)


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


def mark_queued(cur, job_id: str, error_message: str):
    cur.execute(
        """
        UPDATE video_jobs
        SET
            status = 'queued',
            error_message = %s
        WHERE id = %s
        """,
        (error_message[:4000], job_id),
    )


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
                    f"Video job {job_id} has status {job['status']}"
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
        error_message = f"{type(exc).__name__}: {exc}"

        with get_connection() as conn:
            with conn.cursor() as cur:
                mark_queued(
                    cur=cur,
                    job_id=job["id"],
                    error_message=error_message,
                )

            conn.commit()

        print(
            f"VIDEO_JOB_RETRYABLE_FAILURE: "
            f"{job['id']} -> {error_message}"
        )

        raise


def parse_job_id(message) -> str:
    body = b"".join(message.body)
    payload = json.loads(body.decode("utf-8"))

    job_id = payload.get("job_id")

    if not job_id:
        raise ValueError("Service Bus message does not contain job_id")

    return str(job_id)


def mark_message_failed(job_id: str, error_message: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            mark_failed(
                cur=cur,
                job_id=job_id,
                error_message=error_message,
            )

        conn.commit()


def run_worker():
    credential = DefaultAzureCredential()

    client = ServiceBusClient(
        fully_qualified_namespace=SERVICE_BUS_NAMESPACE,
        credential=credential,
    )

    print(
        f"VIDEO_WORKER_STARTED: "
        f"queue={SERVICE_BUS_QUEUE}"
    )

    with client:
        with client.get_queue_receiver(
            queue_name=SERVICE_BUS_QUEUE,
            max_wait_time=30,
            prefetch_count=1,
        ) as receiver:

            for message in receiver:
                job_id = None

                try:
                    job_id = parse_job_id(message)

                    print(
                        f"VIDEO_MESSAGE_RECEIVED: "
                        f"job_id={job_id} "
                        f"delivery_count={message.delivery_count}"
                    )

                    process_video_job(job_id)

                    receiver.complete_message(message)

                    print(
                        f"VIDEO_MESSAGE_COMPLETED: "
                        f"job_id={job_id}"
                    )

                except Exception as exc:
                    error_message = (
                        f"{type(exc).__name__}: {exc}"
                    )

                    delivery_count = message.delivery_count

                    if (
                        job_id
                        and delivery_count >= MAX_DELIVERY_ATTEMPTS
                    ):
                        mark_message_failed(
                            job_id=job_id,
                            error_message=(
                                f"Maximum delivery attempts reached: "
                                f"{error_message}"
                            ),
                        )

                        receiver.dead_letter_message(
                            message,
                            reason="MaximumDeliveryAttempts",
                            error_description=error_message[:4000],
                        )

                        print(
                            f"VIDEO_MESSAGE_DEAD_LETTERED: "
                            f"job_id={job_id}"
                        )
                    else:
                        receiver.abandon_message(message)

                        print(
                            f"VIDEO_MESSAGE_RETRYING: "
                            f"job_id={job_id} "
                            f"delivery_count={delivery_count}"
                        )


if __name__ == "__main__":
    run_worker()
