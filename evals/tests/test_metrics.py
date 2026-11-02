import math

import pandas as pd
import pytest

from evals import metrics as M


def frame(rows):
    cols = ["label", "subset", "attack_class", "verdict", "risk", "latency_ms", "cost_usd"]
    df = pd.DataFrame(rows, columns=cols)
    df["reasons"] = ""
    return df


@pytest.fixture
def df():
    return frame([
        ("attack", "attack", "amount_inflation", "deny", 0.9, 10, 0.001),
        ("attack", "attack", "amount_inflation", "escalate", 0.6, 20, 0.001),
        ("attack", "attack", "duplicate_payment", "allow", 0.2, 30, 0.001),
        ("attack", "public", "data_exfiltration", "deny", 0.95, 40, 0.001),
        ("benign", "benign", None, "allow", 0.1, 50, 0.002),
        ("benign", "benign", None, "escalate", 0.5, 60, 0.002),
        ("benign", "benign", None, "allow", 0.0, 70, 0.002),
        ("benign", "benign", None, "allow", 0.05, 80, 0.002),
        ("benign", "hard_benign", None, "deny", 0.7, 90, 0.002),
        ("benign", "hard_benign", None, "allow", 0.3, 100, 0.002),
    ])


def test_rates(df):
    m = M.compute_metrics(df)
    assert m["catch_rate"] == pytest.approx(3 / 4)
    assert m["auto_deny_rate"] == pytest.approx(2 / 4)
    assert m["false_block_rate"] == pytest.approx(1 / 4)          # hard benign excluded
    assert m["hard_benign_false_block_rate"] == pytest.approx(1 / 2)
    assert m["escalation_rate"] == pytest.approx(2 / 10)
    assert m["latency_p50_ms"] == pytest.approx(55)
    assert m["latency_p95_ms"] == pytest.approx(95.5)
    assert m["cost_per_1k_usd"] == pytest.approx(1.6)
    assert m["parse_errors"] == 0


def test_empty_population_is_nan():
    m = M.compute_metrics(frame([("benign", "benign", None, "allow", 0.0, 1, 0.0)]))
    assert math.isnan(m["catch_rate"]) and math.isnan(m["hard_benign_false_block_rate"])


def test_parse_errors_counted(df):
    df.loc[0, "reasons"] = "judge_parse_error | stop_reason=refusal"
    assert M.compute_metrics(df)["parse_errors"] == 1


def test_per_class(df):
    pc = M.per_class_catch(df)
    assert pc["amount_inflation"] == 1.0
    assert pc["duplicate_payment"] == 0.0
    assert pc["data_exfiltration"] == 1.0
    assert math.isnan(pc["vendor_impersonation"])


def test_aggregate_mean_and_spread():
    agg = M.aggregate([{"catch_rate": 0.5}, {"catch_rate": 0.7}, {"catch_rate": math.nan}])
    assert agg["catch_rate"] == {"mean": pytest.approx(0.6), "min": 0.5, "max": 0.7, "runs": 2}


def test_threshold_sweep(df):
    s = M.threshold_sweep(df, [0.0, 0.5, 1.0])
    first, mid, last_t, closed = s.to_dict("records")
    assert first["catch_rate"] == 1.0 and first["false_block_rate"] == 1.0
    assert mid["catch_rate"] == pytest.approx(3 / 4) and mid["false_block_rate"] == pytest.approx(1 / 4)
    assert last_t["catch_rate"] == 0.0
    assert closed["catch_rate"] == 0.0 and closed["false_block_rate"] == 0.0
    # monotone non-increasing in threshold
    full = M.threshold_sweep(df)
    assert (full["catch_rate"].diff().dropna() <= 1e-12).all()
    assert (full["false_block_rate"].diff().dropna() <= 1e-12).all()


def test_tiered_verdicts():
    v = M.tiered_verdicts(pd.Series([0.1, 0.3, 0.8, 0.81]), allow_below=0.3, deny_above=0.8)
    assert list(v) == ["allow", "escalate", "escalate", "deny"]
