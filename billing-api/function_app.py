import json
import os
import uuid
import azure.functions as func
import psycopg
import stripe

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
        return func.HttpResponse(
            "Webhook secret not configured",
            status_code=500,
        )

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

                    # Ignore Checkout Sessions that are not created by
                    # AI Money Lab subscription checkout.
                    if not customer_id or plan not in ALLOWED_PLANS:
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

                elif event_type in (
                    "customer.subscription.updated",
                    "customer.subscription.deleted",
                ):
                    stripe_customer_id = data.get("customer")
                    stripe_subscription_id = data.get("id")
                    subscription_status = data.get("status", "canceled")

                    items = data.get("items", {}).get("data", [])
                    price_id = None

                    if items:
                        price_id = items[0].get("price", {}).get("id")

                    plan = "free"

                    for candidate, env_name in STRIPE_PLAN_PRICES.items():
                        if price_id and price_id == os.environ.get(env_name):
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

            conn.commit()

        return func.HttpResponse("ok", status_code=200)

    except Exception as exc:
        print(f"STRIPE_WEBHOOK_ERROR: {type(exc).__name__}: {exc}")
        return func.HttpResponse(
            f"Webhook processing failed: {type(exc).__name__}: {exc}",
            status_code=500,
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
                        referrer,
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
                        %s, %s, %s, %s, %s, %s, %s
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
                        body.get("referrer"),
                        body.get("page_url"),
                        body.get("session_id"),
                        body.get("destination"),
                        body.get("revenue", 0),
                        body.get("currency", "USD"),
                        body.get("cost", 0),
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
                        "ai_money_lab",
                        "campaign",
                        str(campaign[0]),
                        "/api/conversions",
                        req.url,
                        session_id,
                        campaign[2],
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
                    })

        return json_response({
            "success": True,
            "customer_id": customer_id or None,
            "total_events": totals[0],
            "total_revenue": float(totals[1]),
            "total_cost": float(totals[2]),
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
