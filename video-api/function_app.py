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
    provider = str(body.get("provider", "default")).strip() or "default"

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

    if duration_seconds < 1 or duration_seconds > 60:
        return json_response({
            "success": False,
            "error": "duration_seconds must be between 1 and 60",
        }, 400)

    if resolution not in {"vertical", "square", "landscape"}:
        return json_response({
            "success": False,
            "error": "resolution must be vertical, square, or landscape",
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
