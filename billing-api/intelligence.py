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
