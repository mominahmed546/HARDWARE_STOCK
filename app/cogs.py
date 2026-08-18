"""Cost of goods sold helpers.

Purchase Rate must be per piece. If a line total was saved by mistake
(e.g. 36,000 instead of 4,500), qty * rate inflates cost. Prefer the
lower of purchase-history unit cost and product purchase rate, and if
the stored figure is closer to the invoice line total than to the unit
sale price, treat it as the line cost (do not multiply by qty again).
"""

_UNIT_COST = """COALESCE(
            CASE
                WHEN COALESCE(pc.UnitCost, 0) > 0 AND COALESCE(it.PurchaseRate, 0) > 0
                    THEN LEAST(pc.UnitCost, it.PurchaseRate)
                ELSE COALESCE(NULLIF(pc.UnitCost, 0), NULLIF(it.PurchaseRate, 0), 0)
            END,
            0
        )"""


def purchase_unit_cost_join(detail_alias="id"):
    return f"""
        LEFT JOIN (
            SELECT
                ItemID,
                SUM(Qty * PurchaseRate) / NULLIF(SUM(Qty), 0) AS UnitCost
            FROM PurchaseDetails
            GROUP BY ItemID
        ) pc ON pc.ItemID = {detail_alias}.ItemID
    """


def sold_line_cost_sql(detail_alias="id"):
    qty = f"{detail_alias}.Qty"
    sale_rate = f"COALESCE({detail_alias}.Rate, 0)"
    line_sale = f"({sale_rate} * {qty})"
    return f"""
        CASE
            WHEN {_UNIT_COST} <= 0 THEN 0
            WHEN COALESCE({qty}, 0) <= 1 THEN COALESCE({qty}, 0) * {_UNIT_COST}
            WHEN ABS({_UNIT_COST} - {sale_rate}) <= ABS({_UNIT_COST} - {line_sale})
                THEN {qty} * {_UNIT_COST}
            ELSE {_UNIT_COST}
        END
    """
