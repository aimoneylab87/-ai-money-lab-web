import json
import os

import azure.functions as func
import psycopg
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage

from entitlement import check_video_entitlement
from video_service import create_video_job


app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

SERVICE_BUS_NAMESPACE = os.environ.get(
    "SERVICE_BUS_NAMESPACE",
    "sb-ai-money-lab.servicebus.windows.net",
)
SERVICE_BUS_QUEUE = os.environ.get(
    "SERVICE_BUS_QUEUE",
    "video-generation",
)


def enqueue_video_job(job_id: str):
    credential = DefaultAzureCredential()

    with ServiceBusClient(
        fully_qualified_namespace=SERVICE_BUS_NAMESPACE,
        credential=credential,
    ) as client:
        with client.get_queue_sender(
            queue_name=SERVICE_BUS_QUEUE
        ) as sender:
            sender.send_messages(
                ServiceBusMessage(
                    json.dumps({"job_id": job_id}),
                    content_type="application/json",
                    subject="video-generation",
                )
            )



def get_connection():
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        sslmode=os.environ.get("POSTGRES_SSLMODE", "require"),
    )


def json_response(payload, status_code=200):
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


@app.route(route="videos", methods=["POST", "OPTIONS"])
def create_video(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse(
            "",
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )

    try:
        body = req.get_json()
    except ValueError:
        return json_response({
            "success": False,
            "error": "Invalid JSON",
        }, 400)

    customer_id = str(body.get("customer_id", "")).strip()
    prompt = str(body.get("prompt", "")).strip()
    provider = str(body.get("provider", "sora-2")).strip().lower() or "sora-2"

    try:
        duration_seconds = int(body.get("duration_seconds", 8))
    except (TypeError, ValueError):
        return json_response({
            "success": False,
            "error": "duration_seconds must be an integer",
        }, 400)

    resolution = str(
        body.get("resolution", "vertical")
    ).strip().lower()

    if not customer_id:
        return json_response({
            "success": False,
            "error": "customer_id is required",
        }, 400)

    if not prompt:
        return json_response({
            "success": False,
            "error": "prompt is required",
        }, 400)

    if len(prompt) > 4000:
        return json_response({
            "success": False,
            "error": "prompt is too long",
        }, 400)

    if duration_seconds not in {4, 8, 12}:
        return json_response({
            "success": False,
            "error": "duration_seconds must be 4, 8, or 12",
        }, 400)

    if resolution not in {"vertical", "landscape"}:
        return json_response({
            "success": False,
            "error": "resolution must be vertical or landscape",
        }, 400)

    if provider not in {"sora-2", "sora", "azure-sora"}:
        return json_response({
            "success": False,
            "error": "provider must be sora-2, sora, or azure-sora",
        }, 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, name, plan, subscription_status
                    FROM customers
                    WHERE id = %s
                    """,
                    (customer_id,),
                )

                customer = cur.fetchone()

                if not customer:
                    return json_response({
                        "success": False,
                        "error": "Customer not found",
                    }, 404)

                entitlement = check_video_entitlement(
                    cur,
                    customer_id,
                )

                if not entitlement.allowed:
                    return json_response({
                        "success": False,
                        "error": "Video generation not available",
                        "reason": entitlement.reason,
                        "plan": entitlement.plan,
                        "subscription_status": entitlement.subscription_status,
                        "monthly_limit": entitlement.monthly_limit,
                        "used": entitlement.used,
                        "remaining": entitlement.remaining,
                        "upgrade_required": True,
                    }, 403)

                job = create_video_job(
                    cur=cur,
                    customer_id=customer_id,
                    prompt=prompt,
                    provider=provider,
                    duration_seconds=duration_seconds,
                    resolution=resolution,
                )

                conn.commit()

                try:
                    enqueue_video_job(job["id"])
                except Exception as queue_exc:
                    print(
                        f"VIDEO_QUEUE_ERROR: "
                        f"{type(queue_exc).__name__}: {queue_exc}"
                    )
                    return json_response({
                        "success": False,
                        "error": "Video job could not be queued",
                        "job": job,
                    }, 503)

                return json_response({
                    "success": True,
                    "job": job,
                    "usage": {
                        "plan": entitlement.plan,
                        "monthly_limit": entitlement.monthly_limit,
                        "used": entitlement.used,
                        "remaining": entitlement.remaining,
                    },
                }, 202)

    except Exception as exc:
        print(
            f"VIDEO_CREATE_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return json_response({
            "success": False,
            "error": "Unable to create video job",
        }, 500)

@app.route(route="videos", methods=["GET", "OPTIONS"])
def list_videos(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse(
            "",
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )

    customer_id = str(
        req.params.get("customer_id", "")
    ).strip()

    if not customer_id:
        return json_response({
            "success": False,
            "error": "customer_id is required",
        }, 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        customer_id,
                        prompt,
                        provider,
                        status,
                        duration_seconds,
                        resolution,
                        video_url,
                        thumbnail_url,
                        error_message,
                        created_at,
                        started_at,
                        completed_at
                    FROM video_jobs
                    WHERE customer_id = %s
                    ORDER BY created_at DESC
                    LIMIT 50
                    """,
                    (customer_id,),
                )

                rows = cur.fetchall()

        videos = []

        for row in rows:
            videos.append({
                "id": str(row[0]),
                "customer_id": str(row[1]),
                "prompt": row[2],
                "provider": row[3],
                "status": row[4],
                "duration_seconds": row[5],
                "resolution": row[6],
                "video_url": row[7],
                "thumbnail_url": row[8],
                "error_message": row[9],
                "created_at": row[10].isoformat() if row[10] else None,
                "started_at": row[11].isoformat() if row[11] else None,
                "completed_at": row[12].isoformat() if row[12] else None,
            })

        return json_response({
            "success": True,
            "videos": videos,
        })

    except Exception as exc:
        print(
            f"VIDEO_LIST_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return json_response({
            "success": False,
            "error": "Unable to load videos",
        }, 500)


@app.route(route="videos/{job_id}", methods=["GET", "OPTIONS"])
def get_video(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse(
            "",
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )

    job_id = str(
        req.route_params.get("job_id", "")
    ).strip()

    customer_id = str(
        req.params.get("customer_id", "")
    ).strip()

    if not job_id:
        return json_response({
            "success": False,
            "error": "job_id is required",
        }, 400)

    if not customer_id:
        return json_response({
            "success": False,
            "error": "customer_id is required",
        }, 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        customer_id,
                        prompt,
                        provider,
                        status,
                        duration_seconds,
                        resolution,
                        video_url,
                        thumbnail_url,
                        error_message,
                        created_at,
                        started_at,
                        completed_at
                    FROM video_jobs
                    WHERE id = %s
                      AND customer_id = %s
                    """,
                    (job_id, customer_id),
                )

                row = cur.fetchone()

        if not row:
            return json_response({
                "success": False,
                "error": "Video job not found",
            }, 404)

        video = {
            "id": str(row[0]),
            "customer_id": str(row[1]),
            "prompt": row[2],
            "provider": row[3],
            "status": row[4],
            "duration_seconds": row[5],
            "resolution": row[6],
            "video_url": row[7],
            "thumbnail_url": row[8],
            "error_message": row[9],
            "created_at": row[10].isoformat() if row[10] else None,
            "started_at": row[11].isoformat() if row[11] else None,
            "completed_at": row[12].isoformat() if row[12] else None,
        }

        return json_response({
            "success": True,
            "video": video,
        })

    except Exception as exc:
        print(
            f"VIDEO_GET_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return json_response({
            "success": False,
            "error": "Unable to load video job",
        }, 500)
