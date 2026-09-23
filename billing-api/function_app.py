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
        "created_at": row[5].isoformat(),
        "updated_at": row[6].isoformat(),
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
                               status, created_at, updated_at
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
                        destination_url
                    )
                    VALUES (%s, %s, %s, %s)
                    RETURNING
                        id,
                        customer_id,
                        name,
                        destination_url,
                        status,
                        created_at,
                        updated_at
                    """,
                    (
                        campaign_id,
                        customer_id,
                        name,
                        destination_url,
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
                               status, created_at, updated_at
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
                        updated_at = NOW()
                    WHERE id = %s
                      AND customer_id = %s
                    RETURNING
                        id,
                        customer_id,
                        name,
                        destination_url,
                        status,
                        created_at,
                        updated_at
                    """,
                    (
                        name,
                        destination_url,
                        status,
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
