"""
Bracket-path probability math for soccer tournament winner markets.

Turns the winner-market cross-section plus a verified bracket structure into
calculations Early Marks can rank on:

- Bradley-Terry match probabilities using de-vigged winner shares as strength.
- Path-implied win probability: product of per-round advance probabilities
  over the team's remaining bracket path.
- Mispricing signal: path-implied probability minus market share (internal
  consistency of the cross-section, not an external truth claim).
- Elimination beta: how much a team's path probability improves when a named
  rival is knocked out, computed by re-running the path model without the
  rival. Same-half eliminations help more than cross-half ones; the model
  captures that structurally instead of by heuristic.

The bracket file (data/reference/wc2026_bracket.json) is curated from verified
sources and carries a `verified_as_of` date. Rounds are nested groups:
half -> quadrant -> pair -> team names matching Kalshi `yes_sub_title`.
All outputs are research-only signals, labeled by the caller per
probability-and-pricing rules.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

from core.probability_math import clamp_probability


def devig_shares(prices: dict[str, float]) -> dict[str, float]:
    """Normalize winner mids so shares sum to 1; drops non-positive entries."""
    clean = {team: float(p) for team, p in prices.items() if p and p > 0}
    total = sum(clean.values())
    if total <= 0:
        return {}
    return {team: p / total for team, p in clean.items()}


def match_win_probability(strength_a: float, strength_b: float) -> float:
    """Bradley-Terry single-match probability from two strengths."""
    a = max(float(strength_a), 1e-9)
    b = max(float(strength_b), 1e-9)
    return a / (a + b)


def _round_advance_probability(
    team: str,
    own_group: list[str],
    opponent_group: list[str],
    strengths: dict[str, float],
    reach: dict[str, float],
) -> float:
    """P(team wins its next round) = sum over opponents of
    P(opponent reaches this round) * P(team beats opponent)."""
    s_team = strengths.get(team, 0.0)
    if s_team <= 0:
        return 0.0
    opponents = [op for op in opponent_group if op != team and strengths.get(op, 0.0) > 0]
    if not opponents:
        return 1.0
    total_reach = sum(reach.get(op, 0.0) for op in opponents)
    if total_reach <= 0:
        return 1.0
    prob = 0.0
    for op in opponents:
        p_meet = reach.get(op, 0.0) / total_reach
        prob += p_meet * match_win_probability(s_team, strengths.get(op, 0.0))
    return clamp_probability(prob)


def path_win_probabilities(
    bracket: dict[str, Any],
    strengths: dict[str, float],
) -> dict[str, float]:
    """
    Path-implied tournament win probability per team.

    bracket: {"halves": [{"quadrants": [{"pairs": [[teamA, teamB], ...]}, ...]}, ...]}
    Teams absent from `strengths` (eliminated / unknown) are ignored.
    Rounds are played pair -> quadrant -> half -> final.
    """
    halves = bracket.get("halves") or []
    alive: set[str] = {t for t, s in strengths.items() if s > 0}

    def group_teams(node: Any) -> list[str]:
        if isinstance(node, list):
            teams: list[str] = []
            for child in node:
                teams.extend(group_teams(child))
            return teams
        return [node] if node in alive else []

    # reach[team] starts at 1 for alive teams and is multiplied down each round.
    reach = {team: 1.0 for team in alive}
    result = {team: 1.0 for team in alive}

    def play_round(matchups: dict[str, list[str]]) -> None:
        """matchups: team -> pooled opponent group for this round.
        All advances are computed from the same pre-round reach snapshot."""
        updates: dict[str, float] = {}
        for team, opponents in matchups.items():
            advance = _round_advance_probability(team, [team], opponents, strengths, reach)
            updates[team] = reach.get(team, 0.0) * advance
        for team, value in updates.items():
            reach[team] = value
            result[team] = value

    # Round 1: within verified pairs (singleton pairs advance automatically).
    matchups: dict[str, list[str]] = {}
    for half in halves:
        for quadrant in half.get("quadrants") or []:
            for pair in quadrant.get("pairs") or []:
                members = group_teams(pair)
                for team in members:
                    others = [t for t in members if t != team]
                    if others:
                        matchups[team] = others
    if matchups:
        play_round(matchups)

    # Round 2: pooled complement inside the quadrant (reach the semifinal slot).
    matchups = {}
    for half in halves:
        for quadrant in half.get("quadrants") or []:
            pairs = quadrant.get("pairs") or []
            if len(pairs) < 2:
                continue
            for pair in pairs:
                members = group_teams(pair)
                complement = [t for p in pairs if p is not pair for t in group_teams(p)]
                if not complement:
                    continue
                for team in members:
                    matchups[team] = complement
    if matchups:
        play_round(matchups)

    # Round 3: pooled complement quadrants inside the half (semifinal).
    matchups = {}
    for half in halves:
        quadrants = half.get("quadrants") or []
        if len(quadrants) < 2:
            continue
        for quadrant in quadrants:
            own = group_teams(quadrant.get("pairs") or [])
            complement = [
                t for q in quadrants if q is not quadrant for t in group_teams(q.get("pairs") or [])
            ]
            if not complement:
                continue
            for team in own:
                matchups[team] = complement
    if matchups:
        play_round(matchups)

    # Final: across halves.
    if len(halves) == 2:
        teams_a = group_teams([q.get("pairs") or [] for q in halves[0].get("quadrants") or []])
        teams_b = group_teams([q.get("pairs") or [] for q in halves[1].get("quadrants") or []])
        matchups = {team: teams_b for team in teams_a}
        matchups.update({team: teams_a for team in teams_b})
        if matchups:
            play_round(matchups)

    # Pooled rounds are an approximation when pairings are unverified, so the
    # raw total can drift slightly from 1; renormalize to keep the output a
    # coherent probability distribution over alive teams.
    total = sum(result.get(team, 0.0) for team in alive)
    if total > 0:
        return {team: result.get(team, 0.0) / total for team in alive}
    return {team: 0.0 for team in alive}


def elimination_beta(
    bracket: dict[str, Any],
    strengths: dict[str, float],
    team: str,
    eliminated_rival: str,
) -> float:
    """Path-probability gain for `team` if `eliminated_rival` drops out now."""
    if team == eliminated_rival:
        return 0.0
    base = path_win_probabilities(bracket, strengths).get(team, 0.0)
    reduced = dict(strengths)
    reduced.pop(eliminated_rival, None)
    without = path_win_probabilities(bracket, reduced).get(team, 0.0)
    return without - base


def cross_section_report(
    bracket: dict[str, Any],
    mid_prices: dict[str, float],
) -> dict[str, Any]:
    """
    Full research payload: de-vigged shares, path-implied probabilities,
    mispricing gap per team, and the two largest elimination betas per team.
    """
    shares = devig_shares(mid_prices)
    paths = path_win_probabilities(bracket, shares)
    rows = []
    rivals_by_share = sorted(shares, key=lambda t: -shares[t])[:6]
    for team in sorted(shares, key=lambda t: -shares[t]):
        betas = []
        for rival in rivals_by_share:
            if rival == team:
                continue
            betas.append({
                "rival": rival,
                "beta": round(elimination_beta(bracket, shares, team, rival), 4),
            })
        betas.sort(key=lambda row: -row["beta"])
        rows.append({
            "team": team,
            "market_share": round(shares[team], 4),
            "path_implied": round(paths.get(team, 0.0), 4),
            "consistency_gap": round(paths.get(team, 0.0) - shares[team], 4),
            "top_elimination_betas": betas[:2],
        })
    return {
        "method": (
            "Bradley-Terry path model using de-vigged winner shares as strengths; "
            "consistency_gap flags internal cross-section tension, not external truth."
        ),
        "teams": rows,
    }
