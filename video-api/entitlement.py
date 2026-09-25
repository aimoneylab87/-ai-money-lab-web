from dataclasses import dataclass


VIDEO_MONTHLY_LIMITS = {
    "free": 0,
    "starter": 10,
    "growth": 50,
    "pro": 200,
}


@dataclass(frozen=True)
class Entitlement:
    allowed: bool
    plan: str
    subscription_status: str
    monthly_limit: int
    used: int
    remaining: int
    reason: str | None = None


def monthly_limit_for_plan(plan: str) -> int:
    return VIDEO_MONTHLY_LIMITS.get(
        (plan or "free").lower(),
        VIDEO_MONTHLY_LIMITS["free"],
    )


def check_video_entitlement(cur, customer_id: str) -> Entitlement:
    cur.execute(
        """
        SELECT plan, subscription_status
        FROM customers
        WHERE id = %s
        """,
        (customer_id,),
    )

    customer = cur.fetchone()

    if not customer:
        return Entitlement(
            allowed=False,
            plan="unknown",
            subscription_status="inactive",
            monthly_limit=0,
            used=0,
            remaining=0,
            reason="Customer not found",
        )

    plan = (customer[0] or "free").lower()
    subscription_status = (customer[1] or "inactive").lower()
    monthly_limit = monthly_limit_for_plan(plan)

    if plan != "free" and subscription_status != "active":
        return Entitlement(
            allowed=False,
            plan=plan,
            subscription_status=subscription_status,
            monthly_limit=monthly_limit,
            used=0,
            remaining=0,
            reason="An active subscription is required",
        )

    cur.execute(
        """
        SELECT COUNT(*)
        FROM video_usage
        WHERE customer_id = %s
          AND timestamp >= date_trunc('month', CURRENT_TIMESTAMP)
          AND timestamp < date_trunc('month', CURRENT_TIMESTAMP) + INTERVAL '1 month'
        """,
        (customer_id,),
    )

    used = int(cur.fetchone()[0] or 0)
    remaining = max(monthly_limit - used, 0)

    if monthly_limit == 0:
        return Entitlement(
            allowed=False,
            plan=plan,
            subscription_status=subscription_status,
            monthly_limit=monthly_limit,
            used=used,
            remaining=remaining,
            reason="Video generation is not included in the current plan",
        )

    if remaining <= 0:
        return Entitlement(
            allowed=False,
            plan=plan,
            subscription_status=subscription_status,
            monthly_limit=monthly_limit,
            used=used,
            remaining=0,
            reason="Monthly video generation limit reached",
        )

    return Entitlement(
        allowed=True,
        plan=plan,
        subscription_status=subscription_status,
        monthly_limit=monthly_limit,
        used=used,
        remaining=remaining,
    )
