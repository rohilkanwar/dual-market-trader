    assert len(orders) == 1
    assert orders[0].outcome is Outcome.YES
    assert orders[0].price == Decimal("0.51")
    assert Decimal(orders[0].metadata["fair_value"]) == Decimal("0.65")


async def test_cost_adjusted_edge_at_threshold_does_not_trade() -> None:
