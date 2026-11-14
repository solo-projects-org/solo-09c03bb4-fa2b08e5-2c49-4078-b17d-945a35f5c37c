"""Rule-level tests for the pure track evaluator."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ais


def cand(row_id, utc, lat, lon, station="SZ01", batch_id="b1"):
    return ais.Candidate(
        row_id=row_id, batch_id=batch_id, station=station, utc=utc, lat=lat, lon=lon
    )


def statuses(evaluation, rows):
    return {r.row_id: evaluation.candidate_status[r.row_id][0] for r in rows}


def test_identical_replay_keeps_one_candidate():
    rows = [cand(1, 100, 22.5, 114.2), cand(2, 100, 22.5, 114.2), cand(3, 160, 22.51, 114.21)]
    ev = ais.evaluate_track(rows)
    st = statuses(ev, rows)
    assert st == {1: "trusted", 2: "isolated", 3: "trusted"}
    assert len(ev.trusted_points) == 1 + 1
    assert ev.trusted_points[0].row_id == 1  # earliest arrival wins


def test_same_second_different_position_isolates_whole_second():
    rows = [
        cand(1, 100, 22.5, 114.2, "SZ01"),
        cand(2, 100, 22.5001, 114.2, "HK02"),
        cand(3, 160, 22.51, 114.21),
    ]
    ev = ais.evaluate_track(rows)
    st = statuses(ev, rows)
    assert st[1] == st[2] == "isolated"
    assert ev.candidate_status[1][1] == "conflict"
    assert st[3] == "trusted"


def test_speed_over_60_isolated_and_next_point_uses_last_trusted():
    # 1000 kn jump, then a small move from the *trusted* point.
    rows = [
        cand(1, 0, 22.0, 114.0),
        cand(2, 60, 23.0, 114.0),  # ~60 nm / 60 s = 3600 kn -> isolated
        cand(3, 120, 22.001, 114.0),  # tiny move from row 1 over 120 s
    ]
    ev = ais.evaluate_track(rows)
    st = statuses(ev, rows)
    assert st == {1: "trusted", 2: "isolated", 3: "trusted"}
    assert ev.candidate_status[2][1] == "speed_exceeded"
    assert ev.trusted_points[-1].speed_knots is not None
    assert ev.trusted_points[-1].speed_knots < ais.MAX_SPEED_KNOTS


def test_exactly_60_knots_is_trusted():
    # ~1 nm per minute is about 60 kn; use a slightly slower step to stay valid.
    nm_per_deg = 60.0
    deg = 59.0 / 60.0 / nm_per_deg  # 59 kn over 60 s
    rows = [cand(1, 0, 22.0, 114.0), cand(2, 60, 22.0 + deg, 114.0)]
    ev = ais.evaluate_track(rows)
    assert statuses(ev, rows) == {1: "trusted", 2: "trusted"}
    assert ev.trusted_points[1].speed_knots <= 59.5


def test_just_over_60_knots_is_isolated():
    nm_per_deg = 60.0
    deg = 61.0 / 60.0 / nm_per_deg
    rows = [cand(1, 0, 22.0, 114.0), cand(2, 60, 22.0 + deg, 114.0)]
    ev = ais.evaluate_track(rows)
    assert statuses(ev, rows) == {1: "trusted", 2: "isolated"}


def test_same_second_same_position_but_different_stations_is_duplicate_not_conflict():
    rows = [
        cand(1, 100, 22.5, 114.2, "SZ01"),
        cand(2, 100, 22.5, 114.2, "HK02"),
        cand(3, 200, 22.501, 114.201, "SZ01"),
    ]
    ev = ais.evaluate_track(rows)
    st = statuses(ev, rows)
    assert st[1] == "trusted" and st[2] == "isolated"
    assert ev.candidate_status[2][1] == "duplicate"
    assert st[3] == "trusted"


def test_late_row_recomputes_earlier_verdicts():
    """A late telegram can turn a previously trusted point into a conflict."""

    early = [
        cand(1, 0, 22.0, 114.0),
        cand(2, 60, 22.01, 114.01),
        cand(3, 120, 22.02, 114.02),
    ]
    before = ais.evaluate_track(early)
    assert all(s == "trusted" for s, _, _ in before.candidate_status.values())

    # Row 4 arrives late and disagrees at t=60; it must invalidate row 2 and
    # force row 3 to be re-evaluated against row 1.
    late = early + [cand(4, 60, 22.9, 114.9, "HK02")]
    after = ais.evaluate_track(late)
    assert after.candidate_status[2][0] == "isolated"
    assert after.candidate_status[2][1] == "conflict"
    assert after.candidate_status[4][0] == "isolated"
    assert after.candidate_status[1][0] == "trusted"
    assert after.candidate_status[3][0] == "trusted"
    assert [p.row_id for p in after.trusted_points] == [1, 3]


def test_conflict_isolation_is_stable_under_replay():
    """Re-sending one side of a conflict does not resurrect the contested second."""
    # Two stations disagree at t=60, so both are isolated and t=120 is measured
    # from t=0 instead.  Replay does not change that verdict.
    rows = [
        cand(1, 0, 22.0, 114.0),
        cand(2, 60, 22.1, 114.1, "SZ01"),
        cand(3, 60, 22.9, 114.9, "HK02"),
        cand(4, 120, 22.001, 114.001),
    ]
    ev = ais.evaluate_track(rows)
    assert ev.candidate_status[2][0] == ev.candidate_status[3][0] == "isolated"
    assert ev.candidate_status[4][0] == "trusted"
    assert [p.utc for p in ev.trusted_points] == [0, 120]


def test_haversine_known_distance():
    # 1 degree of latitude is 60 nautical miles by definition of the NM.
    assert math.isclose(ais.haversine_nm(0, 0, 1, 0), 60.04, rel_tol=0.01)
