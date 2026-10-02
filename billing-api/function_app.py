import json
import os
import uuid
from datetime import datetime, timedelta, timezone
import azure.functions as func
from intelligence import calculate_performance, calculate_trend, detect_anomalies, calculate_forecast, calculate_ltv_cac, evaluate_decision_policy
import psycopg
import stripe
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, generate_blob_sas
from azure.servicebus import ServiceBusClient, ServiceBusMessage

from entitlement import check_video_entitlement
from video_service import create_video_job

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


VIDEO_STORAGE_ACCOUNT = os.environ.get("VIDEO_STORAGE_ACCOUNT", "").strip()
VIDEO_CONTAINER = os.environ.get("VIDEO_CONTAINER", "videos").strip()
VIDEO_SAS_MINUTES = int(os.environ.get("VIDEO_SAS_MINUTES", "30"))


def create_video_sas_url(video_url: str) -> str | None:
    if not video_url or not VIDEO_STORAGE_ACCOUNT:
        return video_url

    try:
        blob_name = video_url.rstrip("/").split("/")[-1]

        credential = DefaultAzureCredential()

        account_url = (
            f"https://{VIDEO_STORAGE_ACCOUNT}.blob.core.windows.net"
        )

        client = BlobServiceClient(
            account_url=account_url,
            credential=credential,
        )

        now = datetime.now(timezone.utc)
        account_key = client.get_user_delegation_key(
            key_start_time=now - timedelta(minutes=1),
            key_expiry_time=now + timedelta(minutes=VIDEO_SAS_MINUTES),
        )
        expiry = now + timedelta(minutes=VIDEO_SAS_MINUTES)

        sas = generate_blob_sas(
            account_name=VIDEO_STORAGE_ACCOUNT,
            container_name=VIDEO_CONTAINER,
            blob_name=blob_name,
            user_delegation_key=account_key,
            permission=BlobSasPermissions(read=True),
            start=now - timedelta(minutes=1),
            expiry=expiry,
        )

        return (
            f"{account_url}/{VIDEO_CONTAINER}/{blob_name}?{sas}"
        )

    except Exception as exc:
        print(
            f"VIDEO_SAS_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )
        return None

PLAN_FEATURES = {
    "free": {
        "max_campaigns": 1,
        "max_content_items": 10,
        "analytics": False,
        "ai_generation": False,
    },
    "starter": {
        "max_campaigns": 3,
        "max_content_items": 50,
        "analytics": True,
        "ai_generation": True,
    },
    "growth": {
        "max_campaigns": 10,
        "max_content_items": 250,
        "analytics": True,
        "ai_generation": True,
    },
    "pro": {
        "max_campaigns": 50,
        "max_content_items": 1000,
        "analytics": True,
        "ai_generation": True,
    },
}

ALLOWED_PLANS = set(PLAN_FEATURES)

SERVICE_BUS_NAMESPACE = os.environ.get(
    "SERVICE_BUS_NAMESPACE",
    "sb-ai-money-lab.servicebus.windows.net",
)
SERVICE_BUS_QUEUE = os.environ.get(
    "SERVICE_BUS_QUEUE",
    "video-generation",
)


def get_connection():
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        sslmode=os.environ.get("POSTGRES_SSLMODE", "require"),
    )


def parse_financial_value(value, field_name):
    try:
        amount = float(value if value not in (None, "") else 0)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number")

    if amount < 0:
        raise ValueError(f"{field_name} cannot be negative")

    return amount


AUTOMATION_ACTIONS = {
    "pause",
    "launch",
}


def execute_automation_action(
    customer_id: str,
    campaign_id: str | None,
    decision_type: str,
    action: str,
    reason: str | None = None,
    metadata: dict | None = None,
) -> dict:
    action = str(action or "").strip().lower()

    if action not in AUTOMATION_ACTIONS:
        raise ValueError("Unsupported automation action")

    execution_id = str(uuid.uuid4())
    metadata = metadata or {}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO automation_executions (
                    id,
                    customer_id,
                    campaign_id,
                    decision_type,
                    action,
                    status,
                    reason,
                    metadata
                )
                VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)
                """,
                (
                    execution_id,
                    customer_id,
                    campaign_id,
                    decision_type,
                    action,
                    reason,
                    json.dumps(metadata),
                ),
            )

            if action == "pause":
                if not campaign_id:
                    raise ValueError(
                        "campaign_id is required for pause"
                    )

                cur.execute(
                    """
                    UPDATE campaigns
                    SET status = 'paused',
                        updated_at = NOW()
                    WHERE id = %s
                      AND customer_id = %s
                    RETURNING id
                    """,
                    (campaign_id, customer_id),
                )

                if not cur.fetchone():
                    raise ValueError("Campaign not found")

            elif action == "launch":
                if not campaign_id:
                    raise ValueError(
                        "campaign_id is required for launch"
                    )

                cur.execute(
                    """
                    UPDATE campaigns
                    SET status = 'active',
                        updated_at = NOW()
                    WHERE id = %s
                      AND customer_id = %s
                    RETURNING id
                    """,
                    (campaign_id, customer_id),
                )

                if not cur.fetchone():
                    raise ValueError("Campaign not found")

            cur.execute(
                """
                UPDATE automation_executions
                SET status = 'executed',
                    executed_at = NOW()
                WHERE id = %s
                """,
                (execution_id,),
            )

    return {
        "execution_id": execution_id,
        "action": action,
        "status": "executed",
    }


@app.route(
    route="automation/execute",
    methods=["POST", "OPTIONS"],
)
def automation_execute(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

    try:
        body = req.get_json()
    except ValueError:
        return json_response({
            "success": False,
            "error": "Invalid JSON",
        }, 400)

    customer_id = str(body.get("customer_id", "")).strip()
    campaign_id = str(body.get("campaign_id", "")).strip() or None
    decision_type = str(body.get("decision_type", "")).strip().lower()
    action = str(body.get("action", "")).strip().lower()
    reason = str(body.get("reason", "")).strip() or None
    metadata = body.get("metadata") or {}

    if not customer_id:
        return json_response({
            "success": False,
            "error": "customer_id is required",
        }, 400)

    if not decision_type:
        return json_response({
            "success": False,
            "error": "decision_type is required",
        }, 400)

    if action not in AUTOMATION_ACTIONS:
        return json_response({
            "success": False,
            "error": "Unsupported automation action",
        }, 400)

    if not isinstance(metadata, dict):
        return json_response({
            "success": False,
            "error": "metadata must be an object",
        }, 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM customers WHERE id = %s",
                    (customer_id,),
                )

                if not cur.fetchone():
                    return json_response({
                        "success": False,
                        "error": "Customer not found",
                    }, 404)

        result = execute_automation_action(
            customer_id=customer_id,
            campaign_id=campaign_id,
            decision_type=decision_type,
            action=action,
            reason=reason,
            metadata=metadata,
        )

        return json_response({
            "success": True,
            "execution": result,
        }, 200)

    except ValueError as exc:
        return json_response({
            "success": False,
            "error": str(exc),
        }, 400)

    except Exception:
        return json_response({
            "success": False,
            "error": "Automation execution failed",
        }, 500)


@app.route(route="billing/customer", methods=["POST", "OPTIONS"])
def billing_customer(req: func.HttpRequest) -> func.HttpResponse:
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
        return func.HttpResponse(
            json.dumps({"success": False, "error": "Invalid JSON"}),
            status_code=400,
            mimetype="application/json",
        )

    email = str(body.get("email", "")).strip().lower()
    name = str(body.get("name", "")).strip()
    if not name:
        name = email.split("@")[0]
    if not email:
        return func.HttpResponse(
            json.dumps({"success": False, "error": "Email is required"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO customers (id, email, name, plan)
                    VALUES (%s, %s, %s, 'free')
                    ON CONFLICT (email)
                    DO UPDATE SET
                        name = EXCLUDED.name
                    RETURNING id, email, name, plan, subscription_status, created_at
                    """,
                    (str(uuid.uuid4()), email, name),
                )

                row = cur.fetchone()

        customer = {
            "id": str(row[0]),
            "email": row[1],
            "name": row[2],
            "plan": row[3],
            "subscription_status": row[4],
            "created_at": row[5].isoformat(),
            "features": PLAN_FEATURES.get(row[3], PLAN_FEATURES["free"]),
        }

        return func.HttpResponse(
            json.dumps({"success": True, "customer": customer}),
            status_code=200,
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    except Exception:
        return func.HttpResponse(
            json.dumps({
                "success": False,
                "error": "Billing customer operation failed"
            }),
            status_code=500,
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


STRIPE_PLAN_PRICES = {
    "starter": "STRIPE_PRICE_STARTER",
    "growth": "STRIPE_PRICE_GROWTH",
    "pro": "STRIPE_PRICE_PRO",
}


def stripe_price_for_plan(plan):
    setting_name = STRIPE_PLAN_PRICES.get(plan)
    if not setting_name:
        return None
    return os.environ.get(setting_name)


def customer_response(row):
    return {
        "id": str(row[0]),
        "email": row[1],
        "name": row[2],
        "plan": row[3],
        "subscription_status": row[4],
        "created_at": row[5].isoformat() if row[5] else None,
        "features": PLAN_FEATURES.get(row[3], PLAN_FEATURES["free"]),
    }


@app.route(route="billing/checkout", methods=["POST", "OPTIONS"])
def billing_checkout(req: func.HttpRequest) -> func.HttpResponse:
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
        return json_response({"success": False, "error": "Invalid JSON"}, 400)

    email = str(body.get("email", "")).strip().lower()
    name = str(body.get("name", "")).strip() or None
    customer_id = str(body.get("customer_id", "")).strip()
    plan = str(body.get("plan", "")).strip().lower()

    if not email or not customer_id:
        return json_response({
            "success": False,
            "error": "Customer ID and email are required"
        }, 400)

    if plan not in {"starter", "growth", "pro"}:
        return json_response({
            "success": False,
            "error": "Paid plan required"
        }, 400)

    price_id = stripe_price_for_plan(plan)

    if not price_id:
        return json_response({
            "success": False,
            "error": "Stripe price is not configured for this plan"
        }, 500)

    secret_key = os.environ.get("STRIPE_SECRET_KEY")

    if not secret_key:
        return json_response({
            "success": False,
            "error": "Stripe is not configured"
        }, 500)

    stripe.api_key = secret_key

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, name, plan, subscription_status, created_at,
                           stripe_customer_id, stripe_subscription_id
                    FROM customers
                    WHERE email = %s
                    """,
                    (email,),
                )
                row = cur.fetchone()

                if row:
                    db_customer_id = str(row[0])

                    cur.execute(
                        """
                        UPDATE customers
                        SET name = COALESCE(%s, name)
                        WHERE email = %s
                        """,
                        (name, email),
                    )
                else:
                    db_customer_id = customer_id

                    cur.execute(
                        """
                        INSERT INTO customers (id, email, name, plan)
                        VALUES (%s, %s, %s, 'free')
                        """,
                        (db_customer_id, email, name),
                    )

                conn.commit()

        stripe_customer_id = row[6] if row else None

        if not stripe_customer_id:
            stripe_customer = stripe.Customer.create(
                email=email,
                name=name,
                metadata={
                    "ai_money_lab_customer_id": db_customer_id
                },
            )
            stripe_customer_id = stripe_customer.id

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE customers
                        SET stripe_customer_id = %s
                        WHERE id = %s
                        """,
                        (stripe_customer_id, db_customer_id),
                    )
                conn.commit()

        success_url = os.environ.get(
            "STRIPE_SUCCESS_URL",
            "https://aimoneyslab.com/dashboard.html?checkout=success"
        )

        cancel_url = os.environ.get(
            "STRIPE_CANCEL_URL",
            "https://aimoneyslab.com/dashboard.html?checkout=cancelled"
        )

        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=stripe_customer_id,
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "customer_id": db_customer_id,
                "plan": plan,
            },
            subscription_data={
                "metadata": {
                    "customer_id": db_customer_id,
                    "plan": plan,
                }
            },
        )

        return json_response({
            "success": True,
            "checkout_url": session.url,
            "session_id": session.id,
        })

    except Exception as exc:
        print(f"STRIPE_CHECKOUT_ERROR: {type(exc).__name__}: {exc}")
        return json_response({
            "success": False,
            "error": "Unable to create Stripe Checkout session"
        }, 500)


@app.route(route="stripe/webhook", methods=["POST"])
def stripe_webhook(req: func.HttpRequest) -> func.HttpResponse:
    payload = req.get_body()
    signature = req.headers.get("Stripe-Signature")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        return func.HttpResponse("Webhook secret not configured", status_code=500)

    try:
        event = stripe.Webhook.construct_event(
            payload,
            signature,
            webhook_secret,
        )
    except ValueError:
        return func.HttpResponse("Invalid payload", status_code=400)
    except stripe.error.SignatureVerificationError:
        return func.HttpResponse("Invalid signature", status_code=400)

    event_type = event["type"]
    data = event["data"]["object"].to_dict()

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:

                if event_type == "checkout.session.completed":
                    customer_id = data.get("metadata", {}).get("customer_id")
                    plan = data.get("metadata", {}).get("plan")
                    stripe_customer_id = data.get("customer")
                    stripe_subscription_id = data.get("subscription")

                    if (
                        not customer_id
                        or plan not in ALLOWED_PLANS
                        or data.get("mode") != "subscription"
                    ):
                        conn.commit()
                        return func.HttpResponse("ok", status_code=200)

                    cur.execute(
                        """
                        UPDATE customers
                        SET stripe_customer_id = %s,
                            stripe_subscription_id = %s,
                            stripe_price_id = %s,
                            plan = %s,
                            subscription_status = 'active'
                        WHERE id = %s
                        """,
                        (
                            stripe_customer_id,
                            stripe_subscription_id,
                            stripe_price_for_plan(plan),
                            plan,
                            customer_id,
                        ),
                    )

                    if cur.rowcount != 1:
                        raise RuntimeError(
                            f"Customer update failed: {customer_id}"
                        )

                elif event_type in (
                    "customer.subscription.updated",
                    "customer.subscription.deleted",
                ):
                    stripe_customer_id = data.get("customer")
                    stripe_subscription_id = data.get("id")
                    subscription_status = data.get("status", "canceled")

                    items = data.get("items", {}).get("data", [])
                    price_id = items[0].get("price", {}).get("id") if items else None

                    plan = "free"

                    for candidate, env_name in STRIPE_PLAN_PRICES.items():
                        if price_id == os.environ.get(env_name):
                            plan = candidate
                            break

                    if event_type == "customer.subscription.deleted":
                        plan = "free"
                        subscription_status = "canceled"

                    cur.execute(
                        """
                        UPDATE customers
                        SET stripe_customer_id = %s,
                            stripe_subscription_id = %s,
                            stripe_price_id = %s,
                            plan = %s,
                            subscription_status = %s
                        WHERE stripe_customer_id = %s
                        """,
                        (
                            stripe_customer_id,
                            stripe_subscription_id,
                            price_id,
                            plan,
                            subscription_status,
                            stripe_customer_id,
                        ),
                    )

                    if cur.rowcount > 1:
                        raise RuntimeError(
                            f"Multiple customers matched {stripe_customer_id}"
                        )

            conn.commit()

        return func.HttpResponse("ok", status_code=200)

    except Exception as exc:
        print(f"STRIPE_WEBHOOK_ERROR: {type(exc).__name__}: {exc}")
        return func.HttpResponse("Webhook processing failed", status_code=500)

@app.route(route="billing/plan", methods=["GET", "OPTIONS"])
def billing_plan(req: func.HttpRequest) -> func.HttpResponse:
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

    plan = str(req.params.get("plan", "free")).strip().lower()

    if plan not in PLAN_FEATURES:
        return func.HttpResponse(
            json.dumps({
                "success": False,
                "error": "Invalid plan"
            }),
            status_code=400,
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    return func.HttpResponse(
        json.dumps({
            "success": True,
            "plan": plan,
            "features": PLAN_FEATURES[plan],
        }),
        status_code=200,
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def json_response(payload, status_code=200):
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


def campaign_customer(customer_id, cur):
    cur.execute(
        """
        SELECT id, email, name, plan
        FROM customers
        WHERE id = %s
        """,
        (customer_id,),
    )
    return cur.fetchone()


def serialize_campaign(row):
    return {
        "id": str(row[0]),
        "customer_id": str(row[1]),
        "name": row[2],
        "destination_url": row[3],
        "status": row[4],
        "objective": row[5],
        "budget": float(row[6]) if row[6] is not None else None,
        "channel": row[7],
        "audience": row[8],
        "offer": row[9],
        "start_date": row[10].isoformat() if row[10] else None,
        "end_date": row[11].isoformat() if row[11] else None,
        "created_at": row[12].isoformat(),
        "updated_at": row[13].isoformat(),
    }


@app.route(route="campaigns", methods=["GET", "POST", "OPTIONS"])
def campaigns(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

    customer_id = str(req.params.get("customer_id", "")).strip()

    if not customer_id:
        return json_response({
            "success": False,
            "error": "customer_id is required",
        }, 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                customer = campaign_customer(customer_id, cur)

                if not customer:
                    return json_response({
                        "success": False,
                        "error": "Customer not found",
                    }, 404)

                if req.method == "GET":
                    cur.execute(
                        """
                        SELECT id, customer_id, name, destination_url,
                               status, objective, budget, channel, audience, offer,
                               start_date, end_date, created_at, updated_at
                        FROM campaigns
                        WHERE customer_id = %s
                        ORDER BY created_at DESC
                        """,
                        (customer_id,),
                    )

                    rows = cur.fetchall()

                    return json_response({
                        "success": True,
                        "campaigns": [serialize_campaign(row) for row in rows],
                    })

                try:
                    body = req.get_json()
                except ValueError:
                    return json_response({
                        "success": False,
                        "error": "Invalid JSON",
                    }, 400)

                name = str(body.get("name", "")).strip()
                destination_url = str(
                    body.get("destination_url", "")
                ).strip()
                objective = str(body.get("objective", "")).strip() or None
                channel = str(body.get("channel", "")).strip() or None
                audience = str(body.get("audience", "")).strip() or None
                offer = str(body.get("offer", "")).strip() or None

                budget_value = body.get("budget")
                budget = None if budget_value in (None, "") else float(budget_value)

                start_date = str(body.get("start_date", "")).strip() or None
                end_date = str(body.get("end_date", "")).strip() or None

                if not name:
                    return json_response({
                        "success": False,
                        "error": "Campaign name is required",
                    }, 400)

                if not destination_url:
                    return json_response({
                        "success": False,
                        "error": "destination_url is required",
                    }, 400)

                plan = customer[3] or "free"
                max_campaigns = PLAN_FEATURES.get(
                    plan,
                    PLAN_FEATURES["free"],
                )["max_campaigns"]

                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM campaigns
                    WHERE customer_id = %s
                    """,
                    (customer_id,),
                )

                campaign_count = cur.fetchone()[0]

                if campaign_count >= max_campaigns:
                    return json_response({
                        "success": False,
                        "error": "Campaign limit reached",
                        "plan": plan,
                        "max_campaigns": max_campaigns,
                    }, 403)

                campaign_id = str(uuid.uuid4())

                cur.execute(
                    """
                    INSERT INTO campaigns (
                        id,
                        customer_id,
                        name,
                        destination_url,
                        objective,
                        budget,
                        channel,
                        audience,
                        offer,
                        start_date,
                        end_date
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING
                        id,
                        customer_id,
                        name,
                        destination_url,
                        status,
                        objective,
                        budget,
                        channel,
                        audience,
                        offer,
                        start_date,
                        end_date,
                        created_at,
                        updated_at
                    """,
                    (
                        campaign_id,
                        customer_id,
                        name,
                        destination_url,
                        objective,
                        budget,
                        channel,
                        audience,
                        offer,
                        start_date,
                        end_date,
                    ),
                )

                row = cur.fetchone()

                return json_response({
                    "success": True,
                    "campaign": serialize_campaign(row),
                }, 201)

    except Exception:
        return json_response({
            "success": False,
            "error": "Campaign operation failed",
        }, 500)


@app.route(
    route="campaigns/{campaign_id}",
    methods=["GET", "PUT", "DELETE", "OPTIONS"],
)
def campaign_detail(
    req: func.HttpRequest,
) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

    campaign_id = str(
        req.route_params.get("campaign_id", "")
    ).strip()

    customer_id = str(
        req.params.get("customer_id", "")
    ).strip()

    if not campaign_id:
        return json_response({
            "success": False,
            "error": "campaign_id is required",
        }, 400)

    if not customer_id:
        return json_response({
            "success": False,
            "error": "customer_id is required",
        }, 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if req.method == "GET":
                    cur.execute(
                        """
                        SELECT id, customer_id, name, destination_url,
                               status, objective, budget, channel, audience, offer,
                               start_date, end_date, created_at, updated_at
                        FROM campaigns
                        WHERE id = %s
                          AND customer_id = %s
                        """,
                        (campaign_id, customer_id),
                    )

                    row = cur.fetchone()

                    if not row:
                        return json_response({
                            "success": False,
                            "error": "Campaign not found",
                        }, 404)

                    return json_response({
                        "success": True,
                        "campaign": serialize_campaign(row),
                    })

                if req.method == "DELETE":
                    cur.execute(
                        """
                        DELETE FROM campaigns
                        WHERE id = %s
                          AND customer_id = %s
                        RETURNING id
                        """,
                        (campaign_id, customer_id),
                    )

                    row = cur.fetchone()

                    if not row:
                        return json_response({
                            "success": False,
                            "error": "Campaign not found",
                        }, 404)

                    return json_response({
                        "success": True,
                        "deleted": str(row[0]),
                    })

                try:
                    body = req.get_json()
                except ValueError:
                    return json_response({
                        "success": False,
                        "error": "Invalid JSON",
                    }, 400)

                name = str(body.get("name", "")).strip()
                destination_url = str(
                    body.get("destination_url", "")
                ).strip()
                status = str(
                    body.get("status", "draft")
                ).strip().lower()
                objective = str(body.get("objective", "")).strip() or None
                channel = str(body.get("channel", "")).strip() or None
                audience = str(body.get("audience", "")).strip() or None
                offer = str(body.get("offer", "")).strip() or None

                budget_value = body.get("budget")
                budget = None if budget_value in (None, "") else float(budget_value)

                start_date = str(body.get("start_date", "")).strip() or None
                end_date = str(body.get("end_date", "")).strip() or None

                if not name:
                    return json_response({
                        "success": False,
                        "error": "Campaign name is required",
                    }, 400)

                if not destination_url:
                    return json_response({
                        "success": False,
                        "error": "destination_url is required",
                    }, 400)

                allowed_statuses = {
                    "draft",
                    "active",
                    "paused",
                    "completed",
                }

                if status not in allowed_statuses:
                    return json_response({
                        "success": False,
                        "error": "Invalid campaign status",
                    }, 400)

                cur.execute(
                    """
                    UPDATE campaigns
                    SET
                        name = %s,
                        destination_url = %s,
                        status = %s,
                        objective = %s,
                        budget = %s,
                        channel = %s,
                        audience = %s,
                        offer = %s,
                        start_date = %s,
                        end_date = %s,
                        updated_at = NOW()
                    WHERE id = %s
                      AND customer_id = %s
                    RETURNING
                        id,
                        customer_id,
                        name,
                        destination_url,
                        status,
                        objective,
                        budget,
                        channel,
                        audience,
                        offer,
                        start_date,
                        end_date,
                        created_at,
                        updated_at
                    """,
                    (
                        name,
                        destination_url,
                        status,
                        objective,
                        budget,
                        channel,
                        audience,
                        offer,
                        start_date,
                        end_date,
                        campaign_id,
                        customer_id,
                    ),
                )

                row = cur.fetchone()

                if not row:
                    return json_response({
                        "success": False,
                        "error": "Campaign not found",
                    }, 404)

                return json_response({
                    "success": True,
                    "campaign": serialize_campaign(row),
                })

    except Exception:
        return json_response({
            "success": False,
            "error": "Campaign operation failed",
        }, 500)


@app.route(route="track", methods=["POST", "OPTIONS"])
def track(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

    try:
        body = req.get_json()
    except ValueError:
        return json_response({
            "success": False,
            "error": "Invalid JSON",
        }, 400)

    event = str(body.get("event", "")).strip()

    if not event:
        return json_response({
            "success": False,
            "error": "event is required",
        }, 400)

    customer_id = str(body.get("customer_id", "")).strip() or None

    try:
        revenue = parse_financial_value(body.get("revenue", 0), "revenue")
        cost = parse_financial_value(body.get("cost", 0), "cost")
    except ValueError as exc:
        return json_response({
            "success": False,
            "error": str(exc),
        }, 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if customer_id:
                    cur.execute(
                        """
                        SELECT id
                        FROM customers
                        WHERE id = %s
                        """,
                        (customer_id,),
                    )

                    if not cur.fetchone():
                        return json_response({
                            "success": False,
                            "error": "Customer not found",
                        }, 404)

                cur.execute(
                    """
                    INSERT INTO traffic_events (
                        event,
                        source,
                        medium,
                        campaign,
                        content,
                        landing_page,
                        page_url,
                        session_id,
                        destination,
                        revenue,
                        currency,
                        cost,
                        customer_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    RETURNING id, created_at
                    """,
                    (
                        event,
                        body.get("source"),
                        body.get("medium"),
                        body.get("campaign"),
                        body.get("content"),
                        body.get("landing_page"),
                        body.get("page_url"),
                        body.get("session_id"),
                        body.get("destination"),
                        revenue,
                        body.get("currency", "USD"),
                        cost,
                        customer_id,
                    ),
                )

                row = cur.fetchone()

        return json_response({
            "success": True,
            "id": str(row[0]),
            "created_at": row[1].isoformat(),
        }, 200)

    except Exception:
        return json_response({
            "success": False,
            "error": "Tracking operation failed",
        }, 500)



@app.route(
    route="campaigns/{campaign_id}/tracking-url",
    methods=["GET", "OPTIONS"],
)
def campaign_tracking_url(
    req: func.HttpRequest,
) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

    campaign_id = str(
        req.route_params.get("campaign_id", "")
    ).strip()

    customer_id = str(
        req.params.get("customer_id", "")
    ).strip()

    if not campaign_id:
        return json_response({
            "success": False,
            "error": "campaign_id is required",
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
                    SELECT id, customer_id, name, destination_url
                    FROM campaigns
                    WHERE id = %s
                      AND customer_id = %s
                    """,
                    (campaign_id, customer_id),
                )

                row = cur.fetchone()

                if not row:
                    return json_response({
                        "success": False,
                        "error": "Campaign not found",
                    }, 404)

                tracking_base = (
                    os.getenv(
                        "TRACKING_BASE_URL",
                        "https://func-ai-money-lab-billing.azurewebsites.net",
                    )
                    .rstrip("/")
                )

                from urllib.parse import urlencode

                query = urlencode({
                    "campaign_id": str(row[0]),
                    "utm_source": "ai_money_lab",
                    "utm_medium": "campaign",
                    "utm_campaign": str(row[0]),
                })

                tracking_url = (
                    f"{tracking_base}/api/click?{query}"
                )

                return json_response({
                    "success": True,
                    "campaign_id": str(row[0]),
                    "campaign_name": row[2],
                    "destination_url": row[3],
                    "tracking_url": tracking_url,
                })

    except Exception:
        return json_response({
            "success": False,
            "error": "Tracking URL operation failed",
        }, 500)


@app.route(
    route="click",
    methods=["GET", "OPTIONS"],
)
def campaign_click(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

    campaign_id = str(
        req.params.get("campaign_id", "")
    ).strip()

    if not campaign_id:
        return json_response({
            "success": False,
            "error": "campaign_id is required",
        }, 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, customer_id, destination_url, status
                    FROM campaigns
                    WHERE id = %s
                    """,
                    (campaign_id,),
                )

                campaign = cur.fetchone()

                if not campaign:
                    return json_response({
                        "success": False,
                        "error": "Campaign not found",
                    }, 404)

                if campaign[3] != "active":
                    return json_response({
                        "success": False,
                        "error": "Campaign is not active",
                    }, 403)

                source = (
                    str(req.params.get("utm_source", "")).strip()
                    or "ai_money_lab"
                )
                medium = (
                    str(req.params.get("utm_medium", "")).strip()
                    or "campaign"
                )
                campaign_name = (
                    str(req.params.get("utm_campaign", "")).strip()
                    or campaign_id
                )
                content = (
                    str(req.params.get("utm_content", "")).strip()
                    or None
                )

                session_id = (
                    str(req.params.get("session_id", "")).strip()
                    or None
                )

                cur.execute(
                    """
                    INSERT INTO traffic_events (
                        event,
                        source,
                        medium,
                        campaign,
                        content,
                        landing_page,
                        page_url,
                        session_id,
                        destination,
                        customer_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    RETURNING id, created_at
                    """,
                    (
                        "click",
                        source,
                        medium,
                        campaign_name,
                        content,
                        "/api/click",
                        req.url,
                        session_id,
                        campaign[2],
                        str(campaign[1]),
                    ),
                )

                event = cur.fetchone()

        return func.HttpResponse(
            status_code=302,
            headers={
                "Location": campaign[2],
                "Cache-Control": "no-store",
            },
        )

    except Exception:
        return json_response({
            "success": False,
            "error": "Click tracking operation failed",
        }, 500)


@app.route(
    route="conversions",
    methods=["POST", "OPTIONS"],
)
def conversion(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

    try:
        body = req.get_json()
    except ValueError:
        return json_response({
            "success": False,
            "error": "Invalid JSON",
        }, 400)

    campaign_id = str(
        body.get("campaign_id", "")
    ).strip()

    if not campaign_id:
        return json_response({
            "success": False,
            "error": "campaign_id is required",
        }, 400)

    session_id = (
        str(body.get("session_id", "")).strip()
        or None
    )

    try:
        revenue = float(body.get("revenue", 0))
    except (TypeError, ValueError):
        return json_response({
            "success": False,
            "error": "revenue must be a number",
        }, 400)

    if revenue < 0:
        return json_response({
            "success": False,
            "error": "revenue cannot be negative",
        }, 400)

    currency = (
        str(body.get("currency", "USD")).strip().upper()
        or "USD"
    )

    if len(currency) != 3:
        return json_response({
            "success": False,
            "error": "currency must be a 3-letter code",
        }, 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, customer_id, name, status
                    FROM campaigns
                    WHERE id = %s
                    """,
                    (campaign_id,),
                )

                campaign = cur.fetchone()

                if not campaign:
                    return json_response({
                        "success": False,
                        "error": "Campaign not found",
                    }, 404)

                if campaign[3] != "active":
                    return json_response({
                        "success": False,
                        "error": "Campaign is not active",
                    }, 403)

                attribution = None

                if session_id:
                    cur.execute(
                        """
                        SELECT
                            source,
                            medium,
                            campaign,
                            landing_page,
                            destination
                        FROM traffic_events
                        WHERE event = 'click'
                          AND campaign = %s
                          AND session_id = %s
                          AND customer_id = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (
                            str(campaign[0]),
                            session_id,
                            str(campaign[1]),
                        ),
                    )
                    attribution = cur.fetchone()

                source = (
                    attribution[0]
                    if attribution and attribution[0]
                    else "ai_money_lab"
                )

                medium = (
                    attribution[1]
                    if attribution and attribution[1]
                    else "campaign"
                )

                attributed_campaign = (
                    attribution[2]
                    if attribution and attribution[2]
                    else str(campaign[0])
                )

                landing_page = (
                    attribution[3]
                    if attribution and attribution[3]
                    else "/api/conversions"
                )

                destination = (
                    attribution[4]
                    if attribution and attribution[4]
                    else campaign[2]
                )

                cur.execute(
                    """
                    INSERT INTO traffic_events (
                        event,
                        source,
                        medium,
                        campaign,
                        landing_page,
                        page_url,
                        session_id,
                        destination,
                        revenue,
                        currency,
                        customer_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    RETURNING id, created_at
                    """,
                    (
                        "conversion",
                        source,
                        medium,
                        attributed_campaign,
                        landing_page,
                        req.url,
                        session_id,
                        destination,
                        revenue,
                        currency,
                        str(campaign[1]),
                    ),
                )

                row = cur.fetchone()

        return json_response({
            "success": True,
            "id": str(row[0]),
            "campaign_id": str(campaign[0]),
            "customer_id": str(campaign[1]),
            "revenue": revenue,
            "currency": currency,
            "created_at": row[1].isoformat(),
        }, 200)

    except Exception:
        return json_response({
            "success": False,
            "error": "Conversion operation failed",
        }, 500)


@app.route(
    route="analytics-dashboard",
    methods=["GET", "OPTIONS"],
)
def analytics_dashboard(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

    customer_id = str(
        req.params.get("customer_id", "")
    ).strip()

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if customer_id:
                    cur.execute(
                        """
                        SELECT id
                        FROM customers
                        WHERE id = %s
                        """,
                        (customer_id,),
                    )

                    if not cur.fetchone():
                        return json_response({
                            "success": False,
                            "error": "Customer not found",
                        }, 404)

                scope = "WHERE customer_id = %s" if customer_id else ""
                params = (customer_id,) if customer_id else ()

                cur.execute(
                    f"""
                    SELECT event, COUNT(*)
                    FROM traffic_events
                    {scope}
                    GROUP BY event
                    ORDER BY COUNT(*) DESC
                    """,
                    params,
                )

                events = [
                    {
                        "event": row[0],
                        "count": row[1],
                    }
                    for row in cur.fetchall()
                ]

                cur.execute(
                    f"""
                    SELECT COALESCE(source, '(direct)'), COUNT(*)
                    FROM traffic_events
                    {scope}
                    GROUP BY source
                    ORDER BY COUNT(*) DESC
                    """,
                    params,
                )

                sources = [
                    {
                        "source": row[0],
                        "count": row[1],
                    }
                    for row in cur.fetchall()
                ]

                cur.execute(
                    f"""
                    SELECT COALESCE(campaign, '(none)'), COUNT(*)
                    FROM traffic_events
                    {scope}
                    GROUP BY campaign
                    ORDER BY COUNT(*) DESC
                    """,
                    params,
                )

                campaigns = [
                    {
                        "campaign": row[0],
                        "count": row[1],
                    }
                    for row in cur.fetchall()
                ]

                cur.execute(
                    f"""
                    SELECT COALESCE(medium, '(none)'), COUNT(*)
                    FROM traffic_events
                    {scope}
                    GROUP BY medium
                    ORDER BY COUNT(*) DESC
                    """,
                    params,
                )

                mediums = [
                    {
                        "medium": row[0],
                        "count": row[1],
                    }
                    for row in cur.fetchall()
                ]

                cur.execute(
                    f"""
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(revenue), 0),
                        COALESCE(SUM(cost), 0)
                    FROM traffic_events
                    {scope}
                    """,
                    params,
                )

                totals = cur.fetchone()

                total_events = int(totals[0] or 0)
                total_revenue = float(totals[1] or 0)
                total_cost = float(totals[2] or 0)
                total_profit = total_revenue - total_cost

                total_roi = (
                    (total_profit / total_cost) * 100
                    if total_cost > 0
                    else None
                )

                total_roas = (
                    total_revenue / total_cost
                    if total_cost > 0
                    else None
                )

                cur.execute(
                    f"""
                    SELECT
                        COALESCE(campaign, '(none)') AS campaign,
                        COUNT(*) FILTER (
                            WHERE event = 'click'
                        ) AS clicks,
                        COUNT(*) FILTER (
                            WHERE event = 'conversion'
                        ) AS conversions,
                        COALESCE(SUM(revenue), 0) AS revenue,
                        COALESCE(SUM(cost), 0) AS cost
                    FROM traffic_events
                    {scope}
                    GROUP BY campaign
                    ORDER BY revenue DESC, clicks DESC
                    """,
                    params,
                )

                campaign_rows = cur.fetchall()

                campaign_metrics = []

                for row in campaign_rows:
                    campaign = row[0]
                    clicks = int(row[1] or 0)
                    conversions = int(row[2] or 0)
                    revenue = float(row[3] or 0)
                    cost = float(row[4] or 0)

                    conversion_rate = (
                        (conversions / clicks) * 100
                        if clicks > 0
                        else 0
                    )

                    profit = revenue - cost

                    roi = (
                        (profit / cost) * 100
                        if cost > 0
                        else None
                    )

                    roas = (
                        revenue / cost
                        if cost > 0
                        else None
                    )

                    campaign_metrics.append({
                        "campaign": campaign,
                        "clicks": clicks,
                        "conversions": conversions,
                        "conversion_rate": round(
                            conversion_rate,
                            2,
                        ),
                        "revenue": round(revenue, 2),
                        "cost": round(cost, 2),
                        "profit": round(profit, 2),
                        "roi": (
                            round(roi, 2)
                            if roi is not None
                            else None
                        ),
                        "roas": (
                            round(roas, 2)
                            if roas is not None
                            else None
                        ),
                    })

        return json_response({
            "success": True,
            "customer_id": customer_id or None,
            "total_events": total_events,
            "total_revenue": round(total_revenue, 2),
            "total_cost": round(total_cost, 2),
            "total_profit": round(total_profit, 2),
            "total_roi": (
                round(total_roi, 2)
                if total_roi is not None
                else None
            ),
            "total_roas": (
                round(total_roas, 2)
                if total_roas is not None
                else None
            ),
            "events": events,
            "sources": sources,
            "campaigns": campaigns,
            "campaign_metrics": campaign_metrics,
            "mediums": mediums,
        })

    except Exception:
        return json_response({
            "success": False,
            "error": "Analytics operation failed",
        }, 500)



@app.route(
    route="optimization",
    methods=["GET", "OPTIONS"],
)
def optimization(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

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
                    SELECT id
                    FROM customers
                    WHERE id::text = %s
                    """,
                    (customer_id,),
                )

                if not cur.fetchone():
                    return json_response({
                        "success": False,
                        "error": "Customer not found",
                    }, 404)

                cur.execute(
                    """
                    SELECT
                        c.id,
                        c.name,
                        c.objective,
                        c.budget,
                        c.channel,
                        c.audience,
                        c.offer,
                        COUNT(te.id) FILTER (
                            WHERE te.event = 'click'
                        ) AS clicks,
                        COUNT(te.id) FILTER (
                            WHERE te.event = 'conversion'
                        ) AS conversions,
                        COALESCE(
                            SUM(te.revenue),
                            0
                        ) AS revenue,
                        COALESCE(
                            SUM(te.cost),
                            0
                        ) AS cost
                    FROM campaigns c
                    LEFT JOIN traffic_events te
                        ON te.campaign = c.id::text
                        AND te.customer_id = c.customer_id
                    WHERE c.customer_id = %s
                    GROUP BY
                        c.id,
                        c.name,
                        c.objective,
                        c.budget,
                        c.channel,
                        c.audience,
                        c.offer
                    ORDER BY revenue DESC, clicks DESC
                    """,
                    (customer_id,),
                )

                rows = cur.fetchall()

        recommendations = []
        opportunities = []
        campaign_analysis = []

        for row in rows:
            (
                campaign_id,
                name,
                objective,
                budget,
                channel,
                audience,
                offer,
                clicks,
                conversions,
                revenue,
                cost,
            ) = row

            clicks = int(clicks or 0)
            conversions = int(conversions or 0)
            revenue = float(revenue or 0)
            cost = float(cost or 0)
            budget = float(budget) if budget is not None else None

            conversion_rate = (
                (conversions / clicks) * 100
                if clicks > 0
                else 0
            )

            profit = revenue - cost

            roi = (
                (profit / cost) * 100
                if cost > 0
                else None
            )

            roas = (
                revenue / cost
                if cost > 0
                else None
            )

            analysis = {
                "campaign_id": str(campaign_id),
                "name": name,
                "objective": objective,
                "channel": channel,
                "audience": audience,
                "offer": offer,
                "clicks": clicks,
                "conversions": conversions,
                "conversion_rate": round(
                    conversion_rate,
                    2,
                ),
                "revenue": round(revenue, 2),
                "cost": round(cost, 2),
                "profit": round(profit, 2),
                "roi": (
                    round(roi, 2)
                    if roi is not None
                    else None
                ),
                "roas": (
                    round(roas, 2)
                    if roas is not None
                    else None
                ),
            }

            campaign_analysis.append(analysis)

            policy_result = evaluate_decision_policy(
                clicks=clicks,
                conversions=conversions,
                revenue=revenue,
                cost=cost,
                budget=budget,
            )

            for decision in policy_result["decisions"]:
                recommendations.append({
                    **decision,
                    "campaign_id": str(campaign_id),
                    "campaign": name,
                })

            if revenue == 0 and clicks == 0:
                opportunities.append({
                    "type": "data_collection",
                    "campaign_id": str(campaign_id),
                    "campaign": name,
                    "reason": "No attributed traffic or revenue has been recorded yet.",
                    "action": "Drive qualified traffic and collect enough performance data before making optimization decisions.",
                })

        if campaign_analysis:
            profitable = [
                item for item in campaign_analysis
                if item["profit"] > 0
            ]

            if profitable:
                best_campaign = max(
                    profitable,
                    key=lambda item: (
                        item["roi"]
                        if item["roi"] is not None
                        else 0
                    ),
                )

                opportunities.append({
                    "type": "best_performer",
                    "campaign_id": best_campaign["campaign_id"],
                    "campaign": best_campaign["name"],
                    "reason": "This campaign currently has the strongest positive ROI among campaigns with recorded profit.",
                    "action": "Use its audience, offer, channel, and creative characteristics as candidates for controlled experiments.",
                })

        priority_order = {
            "high": 0,
            "medium": 1,
            "low": 2,
        }

        recommendations.sort(
            key=lambda item: priority_order.get(
                item.get("priority"),
                9,
            )
        )

        next_best_action = None

        if recommendations:
            next_best_action = recommendations[0]
        elif opportunities:
            next_best_action = opportunities[0]

        return json_response({
            "success": True,
            "customer_id": customer_id,
            "campaign_count": len(campaign_analysis),
            "campaigns": campaign_analysis,
            "recommendations": recommendations,
            "opportunities": opportunities,
            "next_best_action": next_best_action,
            "engine": {
                "version": "1.0",
                "mode": "recommendation",
                "automatic_execution": False,
            },
        })

    except Exception as exc:
        print(
            f"OPTIMIZATION_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return json_response({
            "success": False,
            "error": "Optimization operation failed",
        }, 500)


@app.route(
    route="intelligence-performance",
    methods=["GET", "OPTIONS"],
)
def intelligence_performance(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

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
                        COUNT(*) FILTER (
                            WHERE event = 'click'
                        ) AS clicks,
                        COUNT(*) FILTER (
                            WHERE event = 'conversion'
                        ) AS conversions,
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN event = 'conversion'
                                    THEN revenue
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS revenue,
                        COALESCE(
                            SUM(cost),
                            0
                        ) AS cost
                    FROM traffic_events
                    WHERE customer_id = %s
                    """,
                    (customer_id,),
                )

                row = cur.fetchone()

        metrics = calculate_performance(
            clicks=row[0],
            conversions=row[1],
            revenue=row[2],
            cost=row[3],
        )

        return json_response({
            "success": True,
            "customer_id": customer_id,
            "intelligence": metrics,
            "engine": {
                "version": "1.0",
                "mode": "performance",
            },
        })

    except Exception as exc:
        print(
            f"INTELLIGENCE_PERFORMANCE_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return json_response({
            "success": False,
            "error": "Intelligence operation failed",
        }, 500)



@app.route(
    route="intelligence-ltv-cac",
    methods=["GET", "OPTIONS"],
)
def intelligence_ltv_cac(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

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
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN event = 'conversion'
                                    THEN revenue
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS revenue,
                        COUNT(*) FILTER (
                            WHERE event = 'conversion'
                        ) AS conversions,
                        COUNT(
                            DISTINCT CASE
                                WHEN event = 'conversion'
                                     AND session_id IS NOT NULL
                                THEN session_id
                            END
                        ) AS unique_customers,
                        COALESCE(
                            SUM(cost),
                            0
                        ) AS acquisition_cost
                    FROM traffic_events
                    WHERE customer_id = %s
                      AND created_at >= NOW() - INTERVAL '30 days'
                    """,
                    (customer_id,),
                )

                row = cur.fetchone()

        revenue = float(row[0] or 0)
        conversions = int(row[1] or 0)
        unique_customers = int(row[2] or 0)
        acquisition_cost = float(row[3] or 0)

        customers = (
            unique_customers
            if unique_customers > 0
            else conversions
        )

        result = calculate_ltv_cac(
            revenue=revenue,
            customers=customers,
            acquisition_cost=acquisition_cost,
            lifespan_periods=1,
        )

        return json_response({
            "success": True,
            "customer_id": customer_id,
            "period": "last_30_days",
            "metrics": result,
            "engine": {
                "version": "1.0",
                "mode": "ltv_cac",
                "lifespan_periods": 1,
            },
        })

    except Exception as exc:
        print(
            f"INTELLIGENCE_LTV_CAC_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return json_response({
            "success": False,
            "error": "Intelligence LTV/CAC operation failed",
        }, 500)


@app.route(
    route="intelligence-forecast",
    methods=["GET", "OPTIONS"],
)
def intelligence_forecast(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

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
                        COUNT(*) FILTER (
                            WHERE event = 'click'
                        ) AS clicks,
                        COUNT(*) FILTER (
                            WHERE event = 'conversion'
                        ) AS conversions,
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN event = 'conversion'
                                    THEN revenue
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS revenue,
                        COALESCE(
                            SUM(cost),
                            0
                        ) AS cost
                    FROM traffic_events
                    WHERE customer_id = %s
                      AND created_at >= NOW() - INTERVAL '7 days'
                    """,
                    (customer_id,),
                )

                row = cur.fetchone()

        current = calculate_performance(
            clicks=row[0],
            conversions=row[1],
            revenue=row[2],
            cost=row[3],
        )

        forecast = calculate_forecast(
            current=current,
            forecast_days=7,
        )

        return json_response({
            "success": True,
            "customer_id": customer_id,
            "period": "next_7_days",
            "baseline": current,
            "forecast": forecast,
            "engine": {
                "version": "1.0",
                "mode": "forecast",
                "forecast_days": 7,
            },
        })

    except Exception as exc:
        print(
            f"INTELLIGENCE_FORECAST_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return json_response({
            "success": False,
            "error": "Intelligence forecast operation failed",
        }, 500)


@app.route(
    route="intelligence-anomaly",
    methods=["GET", "OPTIONS"],
)
def intelligence_anomaly(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

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
                        COUNT(*) FILTER (
                            WHERE event = 'click'
                        ) AS clicks,
                        COUNT(*) FILTER (
                            WHERE event = 'conversion'
                        ) AS conversions,
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN event = 'conversion'
                                    THEN revenue
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS revenue,
                        COALESCE(
                            SUM(cost),
                            0
                        ) AS cost
                    FROM traffic_events
                    WHERE customer_id = %s
                      AND created_at >= NOW() - INTERVAL '7 days'
                    """,
                    (customer_id,),
                )

                current_row = cur.fetchone()

                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE event = 'click'
                        ) AS clicks,
                        COUNT(*) FILTER (
                            WHERE event = 'conversion'
                        ) AS conversions,
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN event = 'conversion'
                                    THEN revenue
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS revenue,
                        COALESCE(
                            SUM(cost),
                            0
                        ) AS cost
                    FROM traffic_events
                    WHERE customer_id = %s
                      AND created_at >= NOW() - INTERVAL '14 days'
                      AND created_at < NOW() - INTERVAL '7 days'
                    """,
                    (customer_id,),
                )

                previous_row = cur.fetchone()

        current = calculate_performance(
            clicks=current_row[0],
            conversions=current_row[1],
            revenue=current_row[2],
            cost=current_row[3],
        )

        previous = calculate_performance(
            clicks=previous_row[0],
            conversions=previous_row[1],
            revenue=previous_row[2],
            cost=previous_row[3],
        )

        anomalies = detect_anomalies(
            current=current,
            previous=previous,
        )

        return json_response({
            "success": True,
            "customer_id": customer_id,
            "periods": {
                "current": "last_7_days",
                "previous": "7_to_14_days_ago",
            },
            "current": current,
            "previous": previous,
            "anomalies": anomalies,
            "engine": {
                "version": "1.0",
                "mode": "anomaly",
                "threshold_percent": 50.0,
            },
        })

    except Exception as exc:
        print(
            f"INTELLIGENCE_ANOMALY_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return json_response({
            "success": False,
            "error": "Intelligence anomaly operation failed",
        }, 500)


@app.route(
    route="intelligence-trend",
    methods=["GET", "OPTIONS"],
)
def intelligence_trend(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return json_response({}, 204)

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
                        COUNT(*) FILTER (
                            WHERE event = 'click'
                        ) AS clicks,
                        COUNT(*) FILTER (
                            WHERE event = 'conversion'
                        ) AS conversions,
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN event = 'conversion'
                                    THEN revenue
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS revenue,
                        COALESCE(
                            SUM(cost),
                            0
                        ) AS cost
                    FROM traffic_events
                    WHERE customer_id = %s
                      AND created_at >= NOW() - INTERVAL '7 days'
                    """,
                    (customer_id,),
                )

                current_row = cur.fetchone()

                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE event = 'click'
                        ) AS clicks,
                        COUNT(*) FILTER (
                            WHERE event = 'conversion'
                        ) AS conversions,
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN event = 'conversion'
                                    THEN revenue
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS revenue,
                        COALESCE(
                            SUM(cost),
                            0
                        ) AS cost
                    FROM traffic_events
                    WHERE customer_id = %s
                      AND created_at >= NOW() - INTERVAL '14 days'
                      AND created_at < NOW() - INTERVAL '7 days'
                    """,
                    (customer_id,),
                )

                previous_row = cur.fetchone()

        current = calculate_performance(
            clicks=current_row[0],
            conversions=current_row[1],
            revenue=current_row[2],
            cost=current_row[3],
        )

        previous = calculate_performance(
            clicks=previous_row[0],
            conversions=previous_row[1],
            revenue=previous_row[2],
            cost=previous_row[3],
        )

        trend = calculate_trend(
            current=current,
            previous=previous,
        )

        return json_response({
            "success": True,
            "customer_id": customer_id,
            "periods": {
                "current": "last_7_days",
                "previous": "7_to_14_days_ago",
            },
            "current": current,
            "previous": previous,
            "trend": trend,
            "engine": {
                "version": "1.0",
                "mode": "trend",
            },
        })

    except Exception as exc:
        print(
            f"INTELLIGENCE_TREND_ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return json_response({
            "success": False,
            "error": "Intelligence trend operation failed",
        }, 500)


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

@app.route(route="videos", methods=["GET", "POST", "OPTIONS"])
def create_video(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "GET":
        return list_videos(req)

    if req.method == "OPTIONS":
        return func.HttpResponse(
            "",
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
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
                "video_url": create_video_sas_url(row[7]) if row[7] else None,
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
            "video_url": create_video_sas_url(row[7]) if row[7] else None,
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