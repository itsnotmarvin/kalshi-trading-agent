from unittest.mock import AsyncMock

from fastapi.testclient import TestClient  # type: ignore

import server


class FakeWorldCupService:
    def route_status(self):
        return {
            "label": "LIVE DATA DEGRADED",
            "blocked_reasons": ["Calibration unavailable — probabilities are uncalibrated."],
            "provider": {
                "claude": {
                    "active": True,
                    "api_key_configured": True,
                    "model": "claude-opus-5",
                }
            },
            "calibration": {"available": False},
            "settings_summary": {"provider": "Claude", "data_status": "LIVE DATA DEGRADED"},
        }

    async def get_matchday(self, user_timezone="America/New_York"):
        return {
            "status": self.route_status(),
            "dashboard_day": "2026-06-24",
            "matches": [],
            "markets": [],
            "candidates": [],
            "basket": {"legs": []},
            "message": "Schedule data unavailable — recommendations disabled.",
        }

    async def evaluate(self, payload=None):
        return await self.get_matchday((payload or {}).get("user_timezone", "America/New_York"))

    def update_settings(self, updates):
        return {"status": "updated", "warnings": [], "settings_summary": {"provider": "Claude"}}

    def approval_lock(self, card):
        return {"status": "locked", "lock": {"lock_id": "wc-lock-test", "card_hash": "abc", "card": card}}

    def paper_approve(self, payload):
        return {"status": "paper_approved", "lock_id": payload["lock_id"], "simulated_order": {"live_order_placed": False}}


def make_client(monkeypatch, enabled: bool = True):
    # The archived route is default-off; these tests opt in explicitly.
    monkeypatch.setattr(server.settings, "world_cup_enabled", enabled)
    monkeypatch.setattr(server, "world_cup_service", FakeWorldCupService())
    monkeypatch.setattr(server, "refresh_state_from_platform", AsyncMock())
    return TestClient(server.app)


def test_world_cup_route_is_disabled_by_default(monkeypatch):
    client = make_client(monkeypatch, enabled=False)

    assert client.get("/api/world-cup/status").status_code == 410
    assert client.get("/world-cup").status_code == 410
    assert client.post("/api/world-cup/evaluate", json={}).status_code == 410


def test_world_cup_status_route(monkeypatch):
    client = make_client(monkeypatch)

    resp = client.get("/api/world-cup/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"]["claude"]["model"] == "claude-opus-5"
    assert "environment" in data


def test_world_cup_matchday_route(monkeypatch):
    client = make_client(monkeypatch)

    resp = client.get("/api/world-cup/matchday")

    assert resp.status_code == 200
    assert resp.json()["matches"] == []


def test_world_cup_post_routes_require_token(monkeypatch):
    client = make_client(monkeypatch)

    resp = client.post("/api/world-cup/evaluate", json={})

    assert resp.status_code == 403


def test_world_cup_evaluate_settings_lock_and_paper_approval(monkeypatch):
    client = make_client(monkeypatch)
    headers = {"X-Agent-Token": server.API_TOKEN}

    evaluate = client.post("/api/world-cup/evaluate", json={"user_timezone": "America/New_York"}, headers=headers)
    settings_resp = client.post("/api/world-cup/settings", json={"max_legs": 3}, headers=headers)
    lock = client.post("/api/world-cup/approval-lock", json={"card": {"market": "KXWC", "approval_eligible": True}}, headers=headers)
    approve = client.post("/api/world-cup/paper-approve", json={"lock_id": "wc-lock-test"}, headers=headers)

    assert evaluate.status_code == 200
    assert settings_resp.json()["status"] == "updated"
    assert lock.json()["status"] == "locked"
    assert approve.json()["simulated_order"]["live_order_placed"] is False
