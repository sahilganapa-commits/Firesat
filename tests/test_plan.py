"""Planner behaviour — especially the three rules that keep it honest.

A bug here is not a wrong pixel, it is a route recommendation onto a road the
fire has already crossed. These are the tests that matter most in the repo.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.firms import Detection
from src.model import Status
from src.plan import build_plan, plan_to_dict, snap_to_known_place
from src.roads import KNOWN_PLACES, ROADS, ROADS_BY_ID

UTC = dt.timezone.utc


def det(lat: float, lon: float, when: dt.datetime) -> Detection:
    return Detection(
        lat=lat, lon=lon, acq=when, confidence="h",
        bright_ti4=340.0, bright_ti5=300.0, frp=20.0,
        satellite="N", daynight="D", scan=0.4, track=0.4,
    )


def cross_all(when: dt.datetime) -> list[Detection]:
    """A detection sitting on every artery."""
    return [det(*road.polyline[len(road.polyline) // 2], when) for road in ROADS]


# --------------------------------------------------------------------------
# Rule 1 — a crossed exit is never recommended
# --------------------------------------------------------------------------


def test_crossed_road_is_never_recommended() -> None:
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    plan = build_plan(cross_all(when), "paradise_downtown", when)

    assert plan.recommended is None
    assert not plan.has_any_usable_exit
    assert "NO OPEN EXIT" in plan.summary()


def test_open_road_is_preferred_over_crossed_one() -> None:
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    # Cross only Pentz; everything else stays open.
    pentz = ROADS_BY_ID["pentz_rd"]
    plan = build_plan([det(*pentz.polyline[3], when)], "paradise_east", when)

    assert plan.recommended is not None
    assert plan.recommended.road_id != "pentz_rd"
    assert plan.recommended.status is Status.OPEN


def test_crossed_option_reports_when_it_was_cut() -> None:
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    pentz = ROADS_BY_ID["pentz_rd"]
    plan = build_plan([det(*pentz.polyline[3], when)], "paradise_east", when)

    cut = next(o for o in plan.options if o.road_id == "pentz_rd")
    assert cut.status is Status.CROSSED
    assert cut.confirmed_crossing_at == when
    assert "CUT" in cut.headline()
    assert not cut.is_usable


# --------------------------------------------------------------------------
# Rule 2 — UNKNOWN is never upgraded to "probably open"
# --------------------------------------------------------------------------


def test_stale_observation_is_unknown_not_recommended_as_open() -> None:
    observed = dt.datetime(2018, 11, 8, 10, 0, tzinfo=UTC)
    much_later = observed + dt.timedelta(hours=8)
    # Fire seen far from every road, then nothing for eight hours.
    plan = build_plan([det(39.60, -121.40, observed)], "paradise_downtown", much_later)

    assert all(o.status is Status.UNKNOWN for o in plan.options)
    assert plan.recommended is None, "UNKNOWN must never be recommended as open"


def test_unknown_option_says_it_does_not_guess() -> None:
    observed = dt.datetime(2018, 11, 8, 10, 0, tzinfo=UTC)
    plan = build_plan(
        [det(39.60, -121.40, observed)],
        "paradise_downtown",
        observed + dt.timedelta(hours=8),
    )
    detail = plan.options[0].detail()
    assert "does not guess" in detail
    assert "unknown, not as open" in detail


def test_unknown_exits_do_not_count_as_usable() -> None:
    """Regression: UNKNOWN once reported as usable while the summary said none.

    An unobserved road is an absence of information, not an exit.
    """
    observed = dt.datetime(2018, 11, 8, 10, 0, tzinfo=UTC)
    plan = build_plan(
        [det(39.60, -121.40, observed)],
        "paradise_downtown",
        observed + dt.timedelta(hours=8),
    )
    assert all(o.status is Status.UNKNOWN for o in plan.options)
    assert not plan.has_any_usable_exit
    assert "NO OPEN EXIT" in plan.summary()


def test_times_are_shown_in_local_pst_not_utc() -> None:
    """Regression: a 01:42 PST overpass rendered as 09:42 and inverted the story."""
    # 09:42 UTC == 01:42 PST — the real VIIRS pre-dawn pass over Paradise.
    observed = dt.datetime(2018, 11, 8, 9, 42, tzinfo=UTC)
    plan = build_plan(
        [det(39.60, -121.40, observed)],
        "paradise_downtown",
        observed + dt.timedelta(hours=8),
    )
    headline = plan.options[0].headline()
    assert "01:42" in headline, headline
    assert "09:42" not in headline

    payload = plan_to_dict(plan)
    assert payload["as_of"].startswith("2018-11-08T01:42")


def test_no_observations_at_all_yields_no_recommendation() -> None:
    plan = build_plan([], "paradise_downtown", dt.datetime(2018, 11, 8, 12, 0, tzinfo=UTC))
    assert plan.recommended is None
    assert all(o.status is Status.UNKNOWN for o in plan.options)


# --------------------------------------------------------------------------
# Rule 3 — staleness travels with every answer
# --------------------------------------------------------------------------


def test_every_option_carries_its_observation_age() -> None:
    observed = dt.datetime(2018, 11, 8, 10, 0, tzinfo=UTC)
    now = observed + dt.timedelta(minutes=45)
    plan = build_plan([det(39.60, -121.40, observed)], "paradise_downtown", now)

    for option in plan.options:
        assert option.as_of is not None
        assert option.observation_age_h == pytest.approx(0.75, abs=0.01)


def test_serialised_plan_cannot_omit_staleness() -> None:
    observed = dt.datetime(2018, 11, 8, 10, 0, tzinfo=UTC)
    payload = plan_to_dict(
        build_plan([det(39.60, -121.40, observed)], "paradise_downtown",
                   observed + dt.timedelta(minutes=30))
    )
    assert payload["as_of"] is not None
    assert payload["observation_age_h"] is not None
    for option in payload["options"]:
        assert "observation_age_h" in option
        assert "as_of" in option


def test_plan_never_leaks_future_detections() -> None:
    """Scenario replay must not see detections after the scenario time."""
    early = dt.datetime(2018, 11, 8, 10, 0, tzinfo=UTC)
    later = dt.datetime(2018, 11, 8, 20, 0, tzinfo=UTC)
    plan = build_plan(cross_all(later), "paradise_downtown", early)

    assert all(o.status is not Status.CROSSED for o in plan.options)


# --------------------------------------------------------------------------
# Address handling
# --------------------------------------------------------------------------


def test_snap_matches_known_place_by_id() -> None:
    assert snap_to_known_place("paradise_east").id == "paradise_east"


def test_snap_matches_on_words() -> None:
    assert snap_to_known_place("magalia").id == "magalia"


def test_snap_falls_back_rather_than_failing() -> None:
    """A stranger typing anything must still get an answer, not an error."""
    assert snap_to_known_place("").id == KNOWN_PLACES[0].id
    assert snap_to_known_place("qqq zzz").id == KNOWN_PLACES[0].id


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_all_five_arteries_are_always_offered() -> None:
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    plan = build_plan([], "paradise_downtown", when)
    assert len(plan.options) == 5
    assert {o.road_id for o in plan.options} == {r.id for r in ROADS}


def test_serialised_plan_includes_route_geometry() -> None:
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    payload = plan_to_dict(build_plan([], "paradise_downtown", when))
    for option in payload["options"]:
        assert len(option["polyline"]) >= 2
        assert {"lat", "lon"} <= option["polyline"][0].keys()
