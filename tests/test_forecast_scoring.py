import pytest

from core.forecast_scoring import (
    baseline_comparison,
    brier_score,
    calibration_buckets,
    log_loss,
    make_walk_forward_splits,
)


def test_brier_score_hand_computed():
    assert abs(brier_score([0.2, 0.8], [0, 1]) - 0.04) < 1e-9


def test_log_loss_gate_returns_none_reason_under_30_samples():
    result = log_loss([0.6, 0.4], [1, 0])
    assert result["value"] is None
    assert "at least 30" in result["reason"]
    assert result["n"] == 2


def test_calibration_bucket_counts():
    buckets = calibration_buckets([0.05, 0.15, 0.85, 0.95], [0, 0, 1, 1])
    counts = {bucket["range"]: bucket["n"] for bucket in buckets}
    assert counts["0.0-0.1"] == 1
    assert counts["0.1-0.2"] == 1
    assert counts["0.8-0.9"] == 1
    assert counts["0.9-1.0"] == 1


def test_baseline_deltas_model_beats_naive_but_loses_to_closing():
    records = [
        {"model_p": 0.6, "market_mid_at_cutoff": 0.55, "closing_price": 0.9, "outcome": 1},
        {"model_p": 0.6, "market_mid_at_cutoff": 0.55, "closing_price": 0.8, "outcome": 1},
        {"model_p": 0.4, "market_mid_at_cutoff": 0.45, "closing_price": 0.1, "outcome": 0},
        {"model_p": 0.4, "market_mid_at_cutoff": 0.45, "closing_price": 0.2, "outcome": 0},
    ]
    table = {row["baseline"]: row for row in baseline_comparison(records)["baselines"]}
    assert table["naive_0_5"]["result"] == "beat"
    assert table["closing_price"]["result"] == "lost"


def test_walk_forward_split_leak_raises():
    timestamps = ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"]
    with pytest.raises(ValueError, match="strictly increasing"):
        make_walk_forward_splits(timestamps, ["2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"])
