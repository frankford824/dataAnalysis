from ledger.engine.calculate import _apply, totals_by_metric
from ledger.money import money_float, sum_amounts

import polars as pl


def test_decimal_sum_is_order_independent_and_cent_rounded():
    values = [0.1, 0.2, 1.005, -0.305]
    assert sum_amounts(values) == sum_amounts(reversed(values))
    assert sum_amounts(values) == sum_amounts([1.0])
    assert money_float(1.005) == 1.01
    assert money_float(-1.005) == -1.01


def test_statement_and_metric_totals_use_decimal_aggregation():
    assert _apply("add", [0.1, 0.2, 1.005]) == 1.31
    facts = pl.DataFrame({
        "metric_id": ["m", "m", "m"],
        "amount": [0.1, 0.2, 1.005],
        "linked": [True, True, True],
    })
    assert totals_by_metric(facts) == {"m": 1.31}
