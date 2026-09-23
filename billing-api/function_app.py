import json
import os
import uuid
import azure.functions as func
import psycopg

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

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


def get_connection():
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        sslmode=os.environ.get("POSTGRES_SSLMODE", "require"),
    )


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
    name = str(body.get("name", "")).strip() or None
    plan = str(body.get("plan", "free")).strip().lower()

    if not email:
        return func.HttpResponse(
            json.dumps({"success": False, "error": "Email is required"}),
            status_code=400,
            mimetype="application/json",
        )

    if plan not in ALLOWED_PLANS:
        return func.HttpResponse(
            json.dumps({"success": False, "error": "Invalid plan"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO customers (id, email, name, plan)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (email)
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        plan = EXCLUDED.plan
                    RETURNING id, email, name, plan, subscription_status, created_at
                    """,
                    (str(uuid.uuid4()), email, name, plan),
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
