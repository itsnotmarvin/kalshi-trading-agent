import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_script(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def test_end_to_end_parse_split_score_and_unlocked_refusal(tmp_path):
    csv_path = tmp_path / "history.csv"
    history_json = tmp_path / "history.json"
    splits_json = tmp_path / "splits.json"
    locked_json = tmp_path / "locked.json"
    scores_json = tmp_path / "scores.json"
    csv_path.write_text(
        "timestamp,Target\n"
        "2026-01-01T00:00:00Z,40\n"
        "2026-01-02T00:00:00Z,45\n"
        "2026-01-03T00:00:00Z,60\n"
        "2026-01-04T00:00:00Z,70\n"
    )

    run_script("scripts/skills/parse_kalshi_history.py", str(csv_path), "--column", "Target", "--ticker", "TICKER", "--output", str(history_json))
    history = json.loads(history_json.read_text())
    assert history["points"][0]["yes_dollars"] == 0.4
    assert "summary" in history["explanatory_only"]

    run_script("scripts/skills/make_walk_forward_splits.py", str(history_json), "--auto-cutoffs", "1", "--output", str(splits_json))
    splits = json.loads(splits_json.read_text())
    prediction = splits["splits"][0]["prediction_lock"]
    prediction.update(
        {
            "id": "p1",
            "predicted_probability": 0.68,
            "confidence": "medium",
            "rationale": "locked synthetic test",
            "predicted_catalysts": [{"name": "news", "realized": False}],
            "locked": True,
            "locked_at": "2026-01-02T00:00:00+00:00",
            "outcome": 1,
            "outcome_known_at": "2026-01-05T00:00:00+00:00",
            "closing_price": 0.72,
        }
    )
    locked_json.write_text(json.dumps({"predictions": [prediction]}, indent=2))
    run_script("scripts/skills/score_predictions.py", str(locked_json), "--output", str(scores_json))
    scores = json.loads(scores_json.read_text())
    assert scores["n"] == 1
    assert scores["aggregate"]["false_positive_catalyst_rate"]["rate"] == 1.0

    prediction["locked"] = False
    locked_json.write_text(json.dumps({"predictions": [prediction]}, indent=2))
    refused = run_script("scripts/skills/score_predictions.py", str(locked_json), check=False)
    assert refused.returncode == 2
    assert "refusing to score unlocked prediction" in refused.stderr


def test_renderer_golden_headings(tmp_path):
    payload = tmp_path / "payload.json"
    report = tmp_path / "report.md"
    payload.write_text(json.dumps({"market": "Example market", "side": "YES"}))
    run_script("scripts/skills/render_market_move_report.py", str(payload), "--output", str(report))
    markdown = report.read_text()
    for heading in (
        "# Market Move Report",
        "## Quick Read",
        "## What Changed",
        "## Price History Signals",
        "## Likely Catalysts",
        "## Fair Probability Check",
        "## Hold-Sell-Watch Plan",
        "## Risks",
    ):
        assert heading in markdown
    assert "not assessed" in markdown
