"""Cost of goods sold helpers.

Purchase Rate on a product must be per piece. If a line total was saved
by mistake (e.g. 36,000 instead of 4,500), multiplying by qty again
inflates cost. When the stored rate looks like a line total for the
sold qty, use it as the line cost instead of qty * rate.
"""

_UNIT_COST = "COALESCE(pc.UnitCost, it.PurchaseRate, 0)"


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
    sale_rate = f"{detail_alias}.Rate"
    return f"""
        CASE
            WHEN {_UNIT_COST} <= 0 THEN 0
            WHEN {qty} > 1
             AND {_UNIT_COST} >= (COALESCE({sale_rate}, 0) * {qty})
                THEN {_UNIT_COST}
            ELSE {qty} * {_UNIT_COST}
        END
    """
