"""
Fresh installations must start with zeroed category statistics.

The risk manager previously seeded new installs with a hardcoded May 2026
performance snapshot that the repository cannot reproduce from fill records.
Operational risk state may only come from auditable local history.
"""
import json

from core.risk_manager import RiskManager


def make_fresh_risk_manager(tmp_path, monkeypatch):
    stats_file = tmp_path / "category_stats.json"
    original_init = RiskManager.__init__

    def patched_init(self):
        original_init(self)
        # Re-point at a fresh location and reload as a fresh install would.
        self.category_stats_file = stats_file
        if stats_file.exists():
            stats_file.unlink()
        self.category_stats = self._load_category_stats()

    monkeypatch.setattr(RiskManager, "__init__", patched_init)
    return RiskManager(), stats_file


def test_fresh_install_starts_with_zeroed_stats(tmp_path, monkeypatch):
    rm, stats_file = make_fresh_risk_manager(tmp_path, monkeypatch)

    assert set(rm.category_stats) == {
        "Weather", "Sports", "Tech/AI", "Politics", "Crypto", "Economics",
    }
    for stats in rm.category_stats.values():
        assert stats == {"trades": 0, "won": 0, "lost": 0, "pnl": 0.0}

    # The zeroed default is persisted, not the legacy snapshot.
    persisted = json.loads(stats_file.read_text())
    assert all(s["trades"] == 0 for s in persisted.values())


def test_no_category_blocked_before_five_recorded_outcomes(tmp_path, monkeypatch):
    rm, _ = make_fresh_risk_manager(tmp_path, monkeypatch)

    # Category discipline requires >= 5 trades of local history before it may
    # hard-block; a fresh install has no evidence against any category.
    for stats in rm.category_stats.values():
        assert stats["trades"] < 5
