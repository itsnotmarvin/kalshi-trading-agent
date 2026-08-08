import json

import server


def test_append_early_mark_run_preserves_supplied_run_id(monkeypatch, tmp_path):
    runs_path = tmp_path / "early_marks_runs.json"
    monkeypatch.setattr(server, "EARLY_MARK_RUNS_PATH", runs_path)

    run = server._append_early_mark_run(
        {"generated_at": "2026-07-03T12:27:03+00:00", "marks": []},
        {"category": "soccer"},
        run_id="early-marks-test-run",
    )

    assert run["run_id"] == "early-marks-test-run"
    assert json.loads(runs_path.read_text())[0]["run_id"] == "early-marks-test-run"


def test_early_mark_quick_take_is_short_and_structured():
    quick = server._build_early_mark_quick_take(
        placement="Argentina",
        current_price=0.18,
        target_price=0.27,
        status_display="Watch only",
        repricing_thesis="I believe Argentina can reprice if the bracket opens up. Extra detail should stay in the research drawer.",
        caution="Do not chase if liquidity dries up or the path catalyst does not appear. This extra sentence should be trimmed.",
        market_knows_check="If price moves sharply before a public explanation appears, investigate the move. Extra text.",
        next_catalyst={
            "label": "France elimination or another bracket-path break",
            "why_it_matters": "World Cup futures can reprice when the path becomes clearer.",
        },
        soccer_path={"market_share_pct": 18.2, "path_implied_pct": 23.2},
    )

    assert set(quick) == {"takeaway", "catalyst", "why", "risk", "check", "full_thesis"}
    assert quick["takeaway"] == "Watch only: watch whether Argentina can move from 18c toward 27c."
    assert "market 18.2%" in quick["why"]
    assert len(quick["risk"]) <= 150


def test_failed_claude_enrichment_is_exposed_as_degraded(monkeypatch, tmp_path):
    probe_path = tmp_path / "early_marks_probe.json"
    probe_path.write_text(json.dumps({
        "run_id": "failed-run",
        "generated_at": "2026-08-03T12:00:00+00:00",
        "top_candidates": [],
        "candidate_count": 0,
        "claude_model": "claude-opus-5",
        "claude_status": "failed",
        "claude_results": [],
        "claude_parse_error": "structured output failed validation",
    }))
    monkeypatch.setattr(server, "EARLY_MARK_PROBE_PATH", probe_path)

    payload = server._build_early_marks_payload()

    assert payload["mode_label"] == "Research degraded"
    assert payload["enrichment"] == {
        "status": "failed",
        "model": "claude-opus-5",
        "result_count": 0,
        "error": "structured output failed validation",
    }
    assert any("not enriched research" in warning for warning in payload["warnings"])


async def test_run_endpoint_returns_502_when_probe_exits_after_claude_failure(monkeypatch, tmp_path):
    probe_path = tmp_path / "early_marks_probe.json"
    monkeypatch.setattr(server, "EARLY_MARK_PROBE_PATH", probe_path)
    monkeypatch.setattr(server.settings, "anthropic_api_key", "test-key")
    server.EARLY_MARK_RUNNING.update({"running": False, "run_id": None})

    class FailedProbe:
        returncode = 2

        def __init__(self, command):
            self.command = command

        async def communicate(self):
            run_id = self.command[self.command.index("--run-id") + 1]
            probe_path.write_text(json.dumps({
                "run_id": run_id,
                "generated_at": "2026-08-03T12:00:00+00:00",
                "top_candidates": [],
                "claude_model": "claude-opus-5",
                "claude_status": "failed",
                "claude_results": [],
                "claude_parse_error": "Claude structured output failed validation",
            }))
            return b"", b""

    async def fake_subprocess(*command, **kwargs):
        return FailedProbe(list(command))

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_subprocess)

    response = await server.run_early_marks({"category": "politics"})
    body = json.loads(response.body)

    assert response.status_code == 502
    assert body["status"] == "error"
    assert body["ui_state"] == "failed"
    assert "Claude enrichment failed" in body["message"]
