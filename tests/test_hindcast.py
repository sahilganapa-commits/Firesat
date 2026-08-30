"""Hindcast scoring: recall, lead time, miss naming, and the exclusion rule."""

from __future__ import annotations

import datetime as dt

import pytest

from src.hindcast import (
    GroundTruthEvent,
    build_report,
    parse_ground_truth,
    render_report,
    score,
)
from src.model import ReplayResult, RoadState, Status, Warning_

PST = dt.timezone(dt.timedelta(hours=-8), "PST")


def at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2018, 11, 8, hour, minute, tzinfo=PST)


def make_result(
    warnings: dict[str, Warning_] | None = None,
    pass_times: list[dt.datetime] | None = None,
) -> ReplayResult:
    return ReplayResult(
        warnings=warnings or {},
        confirmed_crossings={},
        pass_times=pass_times or [at(1, 42), at(13, 30)],
        final_states={},
        n_detections_used=10,
        n_detections_dropped_low_confidence=0,
    )


def point_event(road_id: str, hour: int, minute: int = 0, tier: str = "HIGH") -> GroundTruthEvent:
    when = at(hour, minute)
    return GroundTruthEvent(
        fire_id="camp_fire", road_id=road_id, closed_from=when, closed_to=when,
        tier=tier, source="S2", note="test",
    )


def warning_at(road_id: str, hour: int, minute: int = 0, kind: str = "confirmed") -> Warning_:
    when = at(hour, minute)
    return Warning_(road_id=road_id, issued_at=when, kind=kind, predicted_closure_at=when)


# --------------------------------------------------------------------------
# Hits and misses
# --------------------------------------------------------------------------


def test_warning_before_closure_is_a_hit_with_positive_lead() -> None:
    result = make_result({"pentz_rd": warning_at("pentz_rd", 7, 0)})
    [s] = score(result, [point_event("pentz_rd", 9, 0)])
    assert s.hit
    assert s.lead_time_h == pytest.approx(2.0)


def test_warning_after_closure_is_a_miss_and_says_how_late() -> None:
    result = make_result({"pentz_rd": warning_at("pentz_rd", 13, 30)})
    [s] = score(result, [point_event("pentz_rd", 9, 0)])
    assert not s.hit
    assert s.lead_time_h == pytest.approx(-4.5)
    assert "4.5 h too late" in s.miss_reason


def test_no_warning_at_all_is_a_named_miss() -> None:
    [s] = score(make_result(), [point_event("skyway", 8, 30)])
    assert not s.hit
    assert s.warning is None
    assert "never flagged" in s.miss_reason


def test_miss_reports_the_observation_gap_that_caused_it() -> None:
    """The gap is the explanation for the miss, so it must be in the record."""
    [s] = score(make_result(), [point_event("skyway", 8, 30)])
    # Last pass before 08:30 is 01:42 -> 6.8 h.
    assert s.last_pass_before_closure == at(1, 42)
    assert s.observation_gap_h == pytest.approx(6.8, abs=0.05)


def test_closure_before_any_pass_reports_no_prior_look() -> None:
    result = make_result(pass_times=[at(13, 30)])
    [s] = score(result, [point_event("pentz_rd", 9, 0)])
    assert s.last_pass_before_closure is None
    assert s.observation_gap_h is None


def test_warning_exactly_at_closure_counts_as_a_hit() -> None:
    result = make_result({"pentz_rd": warning_at("pentz_rd", 9, 0)})
    [s] = score(result, [point_event("pentz_rd", 9, 0)])
    assert s.hit
    assert s.lead_time_h == pytest.approx(0.0)


# --------------------------------------------------------------------------
# The window / cardinality event
# --------------------------------------------------------------------------


def window_event() -> GroundTruthEvent:
    return GroundTruthEvent(
        fire_id="camp_fire", road_id="southbound_3of4",
        closed_from=at(11, 30), closed_to=at(13, 0),
        tier="WINDOW", source="S2", note="three of four southbound",
    )


def test_window_event_needs_three_of_four_southbound() -> None:
    two = {
        "skyway": warning_at("skyway", 10, 0),
        "clark_rd": warning_at("clark_rd", 10, 0),
    }
    [s] = score(make_result(two), [window_event()])
    assert not s.hit
    assert "2/3" in s.miss_reason


def test_window_event_hit_when_three_flagged_in_time() -> None:
    three = {
        "skyway": warning_at("skyway", 10, 0),
        "clark_rd": warning_at("clark_rd", 10, 30),
        "neal_rd": warning_at("neal_rd", 11, 0),
    }
    [s] = score(make_result(three), [window_event()])
    assert s.hit
    # Lead is measured from the earliest of the qualifying warnings.
    assert s.lead_time_h == pytest.approx(1.5)


def test_window_event_ignores_warnings_after_the_window_closes() -> None:
    late = {
        "skyway": warning_at("skyway", 14, 0),
        "clark_rd": warning_at("clark_rd", 14, 0),
        "neal_rd": warning_at("neal_rd", 14, 0),
    }
    # The warnings are legitimate observations — they just land after the
    # window closed at 13:00, so they must not count toward the event.
    result = make_result(late, pass_times=[at(1, 42), at(14, 0)])
    [s] = score(result, [window_event()])
    assert not s.hit


def test_window_event_only_counts_southbound_roads() -> None:
    """Honey Run is westbound and must not count toward the southbound claim."""
    mixed = {
        "skyway": warning_at("skyway", 10, 0),
        "clark_rd": warning_at("clark_rd", 10, 0),
        "honey_run_rd": warning_at("honey_run_rd", 10, 0),
    }
    [s] = score(make_result(mixed), [window_event()])
    assert not s.hit
    assert "honey_run_rd" not in s.miss_reason


# --------------------------------------------------------------------------
# The exclusion rule
# --------------------------------------------------------------------------


def test_unverified_events_are_not_scored() -> None:
    unverified = GroundTruthEvent(
        fire_id="camp_fire", road_id="clark_rd", closed_from=None, closed_to=None,
        tier="UNVERIFIED", source="S1", note="not readable at build time",
    )
    assert score(make_result(), [unverified]) == []


def test_excluding_unverified_cannot_inflate_recall() -> None:
    """Exclusion must never turn a miss into a better number."""
    events = [point_event("pentz_rd", 9, 0)]
    unverified = GroundTruthEvent(
        fire_id="camp_fire", road_id="clark_rd", closed_from=None, closed_to=None,
        tier="UNVERIFIED", source="S1", note="",
    )
    result = make_result({"pentz_rd": warning_at("pentz_rd", 7, 0)})

    without = build_report(score(result, events), result, events)
    with_excluded = build_report(
        score(result, events + [unverified]), result, events + [unverified]
    )
    assert without["recall"] == with_excluded["recall"]
    assert with_excluded["n_excluded_unverified"] == 1


# --------------------------------------------------------------------------
# Report shape
# --------------------------------------------------------------------------


def test_report_carries_the_params_fingerprint() -> None:
    events = [point_event("pentz_rd", 9, 0)]
    result = make_result()
    report = build_report(score(result, events), result, events)
    assert len(report["params_fingerprint"]) == 12
    assert report["params"]["ROAD_BUFFER_M"] == 187.5


def test_recall_and_median_lead_time() -> None:
    events = [point_event("pentz_rd", 9, 0), point_event("skyway", 8, 30)]
    result = make_result({"pentz_rd": warning_at("pentz_rd", 7, 0)})
    report = build_report(score(result, events), result, events)
    assert report["recall"] == pytest.approx(0.5)
    assert report["median_lead_time_h"] == pytest.approx(2.0)
    assert report["n_misses"] == 1


def test_every_miss_appears_in_the_rendered_report() -> None:
    events = [point_event("pentz_rd", 9, 0), point_event("skyway", 8, 30)]
    result = make_result()
    text = render_report(build_report(score(result, events), result, events))
    assert "MISSES — every one, named" in text
    assert "Pentz Road" in text
    assert "Skyway" in text


def test_lookahead_is_rejected() -> None:
    """A warning issued after the last observation means ground truth leaked."""
    result = make_result(
        {"pentz_rd": warning_at("pentz_rd", 23, 0)}, pass_times=[at(1, 42)]
    )
    with pytest.raises(AssertionError, match="saw the future"):
        score(result, [point_event("pentz_rd", 9, 0)])


# --------------------------------------------------------------------------
# The real ground-truth file
# --------------------------------------------------------------------------


def test_ground_truth_file_parses() -> None:
    events = parse_ground_truth()
    assert len(events) == 6
    scored = [e for e in events if e.is_scored]
    assert len(scored) == 3


def test_ground_truth_scored_rows_all_cite_a_source() -> None:
    for event in parse_ground_truth():
        assert event.source.startswith("S"), f"{event.road_id} has no citation"


def test_pentz_road_is_the_documented_first_closure() -> None:
    """The one closure time NIST states outright. If this drifts, stop."""
    events = {e.road_id: e for e in parse_ground_truth()}
    assert events["pentz_rd"].closed_from == at(9, 0)
    assert events["pentz_rd"].tier == "HIGH"
