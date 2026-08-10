"""Tests for the soccer bracket path model."""
import json
from pathlib import Path

from core.soccer_path import (
    cross_section_report,
    devig_shares,
    elimination_beta,
    match_win_probability,
    path_win_probabilities,
)


def toy_bracket_4():
    return {
        "halves": [
            {"quadrants": [{"pairs": [["A", "B"]]}]},
            {"quadrants": [{"pairs": [["C", "D"]]}]},
        ]
    }


def bracket_8():
    return {
        "halves": [
            {"quadrants": [{"pairs": [["A", "B"]]}, {"pairs": [["C", "D"]]}]},
            {"quadrants": [{"pairs": [["E", "F"]]}, {"pairs": [["G", "H"]]}]},
        ]
    }


def test_devig_shares_normalizes():
    shares = devig_shares({"A": 0.30, "B": 0.20, "C": 0.10, "Z": 0.0})
    assert abs(sum(shares.values()) - 1.0) < 1e-9
    assert "Z" not in shares
    assert abs(shares["A"] - 0.5) < 1e-9


def test_match_win_probability_bradley_terry():
    assert abs(match_win_probability(0.4, 0.3) - (0.4 / 0.7)) < 1e-9
    assert abs(match_win_probability(0.2, 0.2) - 0.5) < 1e-9


def test_four_team_closed_form():
    strengths = {"A": 0.4, "B": 0.3, "C": 0.2, "D": 0.1}
    paths = path_win_probabilities(toy_bracket_4(), strengths)
    # Hand computation: P(A>B)=4/7; final vs C (w.p. 2/3, P=2/3) or D (1/3, P=4/5).
    expected_a = (4 / 7) * ((2 / 3) * (2 / 3) + (1 / 3) * (4 / 5))
    assert abs(paths["A"] - expected_a) < 1e-9
    assert abs(sum(paths.values()) - 1.0) < 1e-6


def test_probabilities_sum_near_one_with_pooled_singletons():
    bracket = {
        "halves": [
            {"quadrants": [{"pairs": [["A", "B"], ["C", "D"]]}]},
            {"quadrants": [{"pairs": [["E"], ["F"], ["G"]]}]},
        ]
    }
    strengths = {"A": 0.3, "B": 0.15, "C": 0.15, "D": 0.1, "E": 0.15, "F": 0.1, "G": 0.05}
    paths = path_win_probabilities(bracket, strengths)
    assert 0.97 <= sum(paths.values()) <= 1.03


def test_elimination_beta_same_half_beats_cross_half():
    strengths = {"A": 0.25, "B": 0.05, "C": 0.20, "D": 0.05, "E": 0.20, "F": 0.05, "G": 0.15, "H": 0.05}
    # For A: C is the same-half rival, E is an equal-strength cross-half rival.
    beta_same = elimination_beta(bracket_8(), strengths, "A", "C")
    beta_cross = elimination_beta(bracket_8(), strengths, "A", "E")
    assert beta_same > beta_cross > 0


def test_eliminated_teams_ignored():
    strengths = {"A": 0.5, "B": 0.3, "C": 0.2}
    paths = path_win_probabilities(bracket_8(), strengths)
    assert set(paths) == {"A", "B", "C"}
    assert abs(sum(paths.values()) - 1.0) < 1e-6


def test_cross_section_report_shape():
    report = cross_section_report(toy_bracket_4(), {"A": 0.4, "B": 0.3, "C": 0.2, "D": 0.1})
    assert report["teams"][0]["team"] == "A"
    row = report["teams"][0]
    assert {"market_share", "path_implied", "consistency_gap", "top_elimination_betas"} <= set(row)
    assert len(row["top_elimination_betas"]) <= 2


def test_wc2026_bracket_file_is_coherent():
    bracket = json.loads(Path("data/wc2026_bracket.json").read_text())
    teams = []
    for half in bracket["halves"]:
        for quadrant in half["quadrants"]:
            for pair in quadrant["pairs"]:
                teams.extend(pair)
    assert len(teams) == len(set(teams)), "team listed twice in bracket"
    assert "Argentina" in teams and "France" in teams
    strengths = {team: 1.0 for team in teams}
    paths = path_win_probabilities(bracket, strengths)
    assert 0.95 <= sum(paths.values()) <= 1.05
