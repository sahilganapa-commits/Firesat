"""Model behaviour: detection, measured approach, and refusal to guess."""

from __future__ import annotations

import datetime as dt

import pytest

from src.firms import Detection, group_into_passes
from src.geo import haversine_m, point_to_polyline_m
from src.model import (
    Status,
    assess_at,
    measured_closing_rate_kmh,
    min_distance_to_road,
    replay,
    road_is_crossed,
)
from src.params import PARAMS, FrozenParams
from src.roads import ROADS_BY_ID

UTC = dt.timezone.utc


def det(lat: float, lon: float, when: dt.datetime, confidence: str = "h") -> Detection:
    return Detection(
        lat=lat, lon=lon, acq=when, confidence=confidence,
        bright_ti4=340.0, bright_ti5=300.0, frp=20.0,
        satellite="N", daynight="D", scan=0.4, track=0.4,
    )


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def test_haversine_known_distance() -> None:
    # Paradise to Chico, ~30 km by straight line.
    d = haversine_m((39.7596, -121.6219), (39.7285, -121.8375))
    assert 17_000 < d < 22_000


def test_point_on_polyline_is_zero_distance() -> None:
    poly = [(39.75, -121.60), (39.76, -121.61)]
    assert point_to_polyline_m((39.75, -121.60), poly) == pytest.approx(0.0, abs=1.0)


# --------------------------------------------------------------------------
# Confirmed crossing
# --------------------------------------------------------------------------


def test_detection_on_the_road_is_a_crossing() -> None:
    road = ROADS_BY_ID["pentz_rd"]
    on_road = road.polyline[3]
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    assert road_is_crossed([det(*on_road, when)], road)


def test_detection_far_away_is_not_a_crossing() -> None:
    road = ROADS_BY_ID["pentz_rd"]
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    # ~5 km east of the road.
    assert not road_is_crossed([det(39.7500, -121.5170, when)], road)


def test_crossing_threshold_is_half_a_pixel() -> None:
    """Just inside half a pixel counts; just outside does not."""
    road = ROADS_BY_ID["pentz_rd"]
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    lat, lon = road.polyline[3]

    # 1 deg longitude at this latitude ~= 85.5 km, so 0.001 deg ~= 85 m.
    near = det(lat, lon + 0.0015, when)   # ~128 m  -> inside 187.5 m
    far = det(lat, lon + 0.0035, when)    # ~300 m  -> outside

    assert min_distance_to_road([near], road) < PARAMS.ROAD_BUFFER_M
    assert min_distance_to_road([far], road) > PARAMS.ROAD_BUFFER_M
    assert road_is_crossed([near], road)
    assert not road_is_crossed([far], road)


def test_low_confidence_detections_are_dropped() -> None:
    road = ROADS_BY_ID["pentz_rd"]
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    result = replay([det(*road.polyline[3], when, confidence="l")])
    assert "pentz_rd" not in result.confirmed_crossings
    assert result.n_detections_dropped_low_confidence == 1


# --------------------------------------------------------------------------
# Measured approach
# --------------------------------------------------------------------------


def test_closing_rate_is_measured_not_assumed() -> None:
    # 2 km closer over 1 hour = 2 km/h.
    assert measured_closing_rate_kmh(5000.0, 3000.0, 1.0) == pytest.approx(2.0)


def test_no_projection_when_fire_is_receding() -> None:
    assert measured_closing_rate_kmh(3000.0, 5000.0, 1.0) is None


def test_closing_rate_clamped_by_literature_cap() -> None:
    """The clamp can only ever slow a projection down."""
    absurd = measured_closing_rate_kmh(500_000.0, 0.0, 1.0)
    assert absurd == pytest.approx(PARAMS.ROS_SANITY_CAP_KMH)


def test_projection_refused_beyond_horizon() -> None:
    """A fire creeping toward a distant road produces no projection at all."""
    road = ROADS_BY_ID["skyway"]
    t0 = dt.datetime(2018, 11, 8, 10, 0, tzinfo=UTC)
    t1 = t0 + dt.timedelta(hours=1)

    # Approaching at ~0.1 km/h from 20 km away -> 200 h out, far past horizon.
    far_a = det(39.9000, -121.3000, t0)
    far_b = det(39.8999, -121.3000, t1)

    result = replay([far_a, far_b])
    assert "skyway" not in result.warnings


# --------------------------------------------------------------------------
# Refusal to guess (the honesty property)
# --------------------------------------------------------------------------


def test_stale_observation_returns_unknown_not_a_guess() -> None:
    road = ROADS_BY_ID["clark_rd"]
    observed = dt.datetime(2018, 11, 8, 10, 0, tzinfo=UTC)
    # 5 km from the road, seen once, then nothing for six hours.
    dets = [det(39.7000, -121.5300, observed)]

    fresh = assess_at(dets, observed + dt.timedelta(minutes=30))
    stale = assess_at(dets, observed + dt.timedelta(hours=6))

    assert fresh[road.id].status is Status.OPEN
    assert stale[road.id].status is Status.UNKNOWN
    assert stale[road.id].observation_age_h == pytest.approx(6.0, abs=0.01)


def test_no_observations_at_all_is_unknown() -> None:
    states = assess_at([], dt.datetime(2018, 11, 8, 12, 0, tzinfo=UTC))
    assert all(s.status is Status.UNKNOWN for s in states.values())
    assert all(s.as_of is None for s in states.values())


def test_confirmed_crossing_does_not_expire() -> None:
    """A burned road does not un-burn because the satellite looked away."""
    road = ROADS_BY_ID["pentz_rd"]
    seen = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    dets = [det(*road.polyline[3], seen)]

    much_later = assess_at(dets, seen + dt.timedelta(hours=12))
    assert much_later[road.id].status is Status.CROSSED
    assert much_later[road.id].confirmed_crossing_at == seen


def test_assess_at_ignores_future_detections() -> None:
    """The interface must not leak hindsight into a scenario replay."""
    road = ROADS_BY_ID["pentz_rd"]
    early = dt.datetime(2018, 11, 8, 10, 0, tzinfo=UTC)
    later = dt.datetime(2018, 11, 8, 20, 0, tzinfo=UTC)

    dets = [det(*road.polyline[3], later)]
    states = assess_at(dets, early)
    assert states[road.id].status is Status.UNKNOWN


# --------------------------------------------------------------------------
# Pass grouping
# --------------------------------------------------------------------------


def test_passes_split_on_large_gaps_only() -> None:
    t0 = dt.datetime(2018, 11, 8, 9, 0, tzinfo=UTC)
    dets = [
        det(39.8, -121.5, t0),
        det(39.8, -121.5, t0 + dt.timedelta(minutes=2)),   # same pass
        det(39.8, -121.5, t0 + dt.timedelta(hours=12)),    # next pass
    ]
    passes = group_into_passes(dets, PARAMS.PASS_GAP_MINUTES)
    assert [len(p) for p in passes] == [2, 1]


def test_replay_is_causal() -> None:
    """No warning may ever be issued before the observation that justifies it."""
    road = ROADS_BY_ID["pentz_rd"]
    when = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)
    result = replay([det(*road.polyline[3], when)])
    for warning in result.warnings.values():
        assert warning.issued_at >= min(result.pass_times)
