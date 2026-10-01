from __future__ import annotations

from typing import Any


def calculate_performance(
    clicks: int,
    conversions: int,
    revenue: float,
    cost: float,
) -> dict[str, Any]:
    clicks = int(clicks or 0)
    conversions = int(conversions or 0)
    revenue = float(revenue or 0)
    cost = float(cost or 0)

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

    return {
        "clicks": clicks,
        "conversions": conversions,
        "conversion_rate": round(conversion_rate, 2),
        "revenue": round(revenue, 2),
        "cost": round(cost, 2),
        "profit": round(profit, 2),
        "roi": round(roi, 2) if roi is not None else None,
        "roas": round(roas, 2) if roas is not None else None,
    }


def calculate_trend(
    current: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    metrics = (
        "clicks",
        "conversions",
        "conversion_rate",
        "revenue",
        "cost",
        "profit",
    )

    trend: dict[str, Any] = {}

    for metric in metrics:
        current_value = float(current.get(metric) or 0)
        previous_value = float(previous.get(metric) or 0)

        change = current_value - previous_value

        if previous_value != 0:
            change_percent = (
                (change / abs(previous_value)) * 100
            )
        elif current_value != 0:
            change_percent = 100.0
        else:
            change_percent = 0.0

        trend[metric] = {
            "current": round(current_value, 2),
            "previous": round(previous_value, 2),
            "change": round(change, 2),
            "change_percent": round(change_percent, 2),
        }

    return trend

def detect_anomalies(
    current: dict[str, Any],
    previous: dict[str, Any],
    threshold_percent: float = 50.0,
) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []

    metrics = (
        "clicks",
        "conversions",
        "conversion_rate",
        "revenue",
        "cost",
        "profit",
    )

    for metric in metrics:
        current_value = float(current.get(metric) or 0)
        previous_value = float(previous.get(metric) or 0)

        if previous_value == 0:
            if current_value == 0:
                continue

            anomalies.append({
                "metric": metric,
                "current": round(current_value, 2),
                "previous": round(previous_value, 2),
                "change_percent": 100.0,
                "direction": "increase",
                "reason": "new_activity",
            })
            continue

        change_percent = (
            (current_value - previous_value)
            / abs(previous_value)
        ) * 100

        if abs(change_percent) >= threshold_percent:
            anomalies.append({
                "metric": metric,
                "current": round(current_value, 2),
                "previous": round(previous_value, 2),
                "change_percent": round(change_percent, 2),
                "direction": (
                    "increase"
                    if change_percent > 0
                    else "decrease"
                ),
                "reason": "threshold_exceeded",
            })

    return anomalies


def calculate_forecast(
    current: dict[str, Any],
    forecast_days: int = 7,
) -> dict[str, Any]:
    forecast_days = max(int(forecast_days or 7), 1)

    metrics = (
        "clicks",
        "conversions",
        "revenue",
        "cost",
        "profit",
    )

    forecast: dict[str, Any] = {}

    for metric in metrics:
        current_value = float(current.get(metric) or 0)

        projected_value = (
            current_value / 7
        ) * forecast_days

        forecast[metric] = round(projected_value, 2)

    clicks = forecast["clicks"]
    conversions = forecast["conversions"]

    forecast["conversion_rate"] = round(
        (conversions / clicks) * 100,
        2,
    ) if clicks > 0 else 0

    return forecast


def calculate_ltv_cac(
    revenue: float,
    customers: int,
    acquisition_cost: float,
    lifespan_periods: float = 1,
) -> dict[str, Any]:
    revenue = float(revenue or 0)
    customers = int(customers or 0)
    acquisition_cost = float(acquisition_cost or 0)
    lifespan_periods = max(float(lifespan_periods or 1), 1)

    revenue_per_customer = (
        revenue / customers
        if customers > 0
        else 0
    )

    ltv = revenue_per_customer * lifespan_periods

    cac = (
        acquisition_cost / customers
        if customers > 0
        else 0
    )

    ltv_cac_ratio = (
        ltv / cac
        if cac > 0
        else None
    )

    return {
        "revenue": round(revenue, 2),
        "customers": customers,
        "acquisition_cost": round(acquisition_cost, 2),
        "lifespan_periods": round(lifespan_periods, 2),
        "revenue_per_customer": round(revenue_per_customer, 2),
        "ltv": round(ltv, 2),
        "cac": round(cac, 2),
        "ltv_cac_ratio": (
            round(ltv_cac_ratio, 2)
            if ltv_cac_ratio is not None
            else None
        ),
    }


DECISION_POLICY = {
    "minimum_clicks_for_conversion_warning": 10,
    "low_conversion_rate_percent": 2.0,
    "minimum_roi_for_scale_percent": 100.0,
    "minimum_profit_for_scale": 0.0,
    "maximum_loss_for_protection": 0.0,
    "budget_overrun_allowed": False,
}


def evaluate_decision_policy(
    clicks: int,
    conversions: int,
    revenue: float,
    cost: float,
    budget: float | None = None,
) -> dict[str, Any]:
    clicks = int(clicks or 0)
    conversions = int(conversions or 0)
    revenue = float(revenue or 0)
    cost = float(cost or 0)
    budget = (
        float(budget)
        if budget is not None
        else None
    )

    profit = revenue - cost

    conversion_rate = (
        (conversions / clicks) * 100
        if clicks > 0
        else 0
    )

    roi = (
        (profit / cost) * 100
        if cost > 0
        else None
    )

    decisions: list[dict[str, Any]] = []

    if (
        revenue > 0
        and profit > DECISION_POLICY["minimum_profit_for_scale"]
        and roi is not None
        and roi >= DECISION_POLICY["minimum_roi_for_scale_percent"]
    ):
        decisions.append({
            "type": "scale",
            "priority": "high",
            "reason": "Positive profit and ROI meet the scale policy threshold.",
            "action": "Consider increasing qualified traffic while monitoring profitability.",
        })

    if (
        clicks >= DECISION_POLICY["minimum_clicks_for_conversion_warning"]
        and conversions == 0
    ):
        decisions.append({
            "type": "conversion",
            "priority": "high",
            "reason": "Traffic threshold reached without recorded conversions.",
            "action": "Review the offer, landing experience, targeting, and conversion tracking.",
        })

    elif (
        clicks >= DECISION_POLICY["minimum_clicks_for_conversion_warning"]
        and conversion_rate < DECISION_POLICY["low_conversion_rate_percent"]
    ):
        decisions.append({
            "type": "optimize",
            "priority": "medium",
            "reason": "Conversion rate is below the policy threshold.",
            "action": "Test the offer, creative, audience, or landing experience.",
        })

    if (
        cost > 0
        and profit < DECISION_POLICY["maximum_loss_for_protection"]
    ):
        decisions.append({
            "type": "protect_profit",
            "priority": "high",
            "reason": "Campaign is operating at a loss.",
            "action": "Review spend and performance before increasing traffic.",
        })

    if (
        DECISION_POLICY["budget_overrun_allowed"] is False
        and budget is not None
        and cost > budget
    ):
        decisions.append({
            "type": "budget_alert",
            "priority": "high",
            "reason": "Campaign cost exceeds the planned budget.",
            "action": "Review spending before additional spend is authorized.",
        })

    return {
        "policy_version": "1.0",
        "decisions": decisions,
        "safety": {
            "automatic_execution_allowed": False,
            "budget_overrun_allowed": False,
        },
    }
