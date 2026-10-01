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
