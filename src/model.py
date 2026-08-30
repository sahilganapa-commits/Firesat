"""THE FIXED MODEL.

One algorithm, one set of frozen parameters, applied identically to every fire.
There is no `if fire_id == ...` in this file and there never will be —
`tests/test_no_per_fire_tuning.py` greps for it.

What the model does
-------------------
1. CONFIRMED CROSSING (no free parameters at all). A road is crossed when a
   qualifying VIIRS detection centroid falls within half a pixel edge of its
   centreline. Half a pixel is where footprint overlap becomes certain. This is
   sensor geometry, not a threshold anyone chose.

2. MEASURED APPROACH (no free parameters either). Between two consecutive
   overpasses, the minimum distance from the fire to a road either shrinks or
   it doesn't. The rate it shrinks at is measured, not assumed — no fuel model,
   no terrain, no wind. If it is shrinking, time-to-road is distance divided by
   that measured rate. The literature clamp in params.py can only ever slow
   this down.

3. REFUSAL. If the newest observation is older than MAX_PROJECTION_H, the model
   returns UNKNOWN. It does not carry a stale front forward and call it a
   forecast. The unobserved gap between satellite passes is the single largest
   source of error in this system, and the design decision is to surface it
   rather than paper over it.

Point 3 is the one that makes the tool honest, and it is also why the tool is
for planning and not navigation.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum

from .firms import Detection, filter_confidence, group_into_passes
from .geo import centroid, point_to_polyline_m
from .params import PARAMS, FrozenParams, assert_no_per_fire_tuning
from .roads import Road, ROADS


class Status(str, enum.Enum):
    OPEN = "open"
    CROSSED = "crossed"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class RoadState:
    """What the model is willing to say about one road at one instant."""

    road_id: str
    status: Status
    # The observation this statement rests on. None when nothing has been seen.
    as_of: dt.datetime | None
    observation_age_h: float | None

    # Set once a detection has actually landed on the road.
    confirmed_crossing_at: dt.datetime | None = None

    # Extrapolated, never simulated. Both None unless the fire is measurably
    # closing on this road AND the projection lands inside MAX_PROJECTION_H.
    projected_closure_at: dt.datetime | None = None
    projection_issued_at: dt.datetime | None = None

    nearest_detection_m: float | None = None
    measured_closing_rate_kmh: float | None = None

    @property
    def is_extrapolated(self) -> bool:
        return self.projected_closure_at is not None

    def minutes_until_closure(self, now: dt.datetime) -> float | None:
        """Minutes until projected closure, or None if there is no projection."""
        if self.projected_closure_at is None:
            return None
        return (self.projected_closure_at - now).total_seconds() / 60.0


@dataclasses.dataclass(frozen=True)
class Warning_:
    """The first moment the model said anything actionable about a road.

    `kind` is "confirmed" when a detection landed on the road, "projected" when
    the model extrapolated a closing front onto it. The hindcast scores against
    whichever came first.
    """

    road_id: str
    issued_at: dt.datetime
    kind: str  # "confirmed" | "projected"
    predicted_closure_at: dt.datetime


@dataclasses.dataclass
class ReplayResult:
    """Everything the hindcast needs from one replay."""

    warnings: dict[str, Warning_]
    confirmed_crossings: dict[str, dt.datetime]
    pass_times: list[dt.datetime]
    final_states: dict[str, RoadState]
    n_detections_used: int
    n_detections_dropped_low_confidence: int


# --------------------------------------------------------------------------
# Core primitives
# --------------------------------------------------------------------------


def min_distance_to_road(detections: list[Detection], road: Road) -> float:
    """Smallest distance in metres from any detection to the road centreline."""
    if not detections:
        return float("inf")
    poly = road.segment_points()
    return min(point_to_polyline_m(d.point, poly) for d in detections)


def road_is_crossed(
    detections: list[Detection], road: Road, params: FrozenParams = PARAMS
) -> bool:
    """True when a detection pixel footprint overlaps the road centreline."""
    return min_distance_to_road(detections, road) <= params.ROAD_BUFFER_M


def measured_closing_rate_kmh(
    d_prev_m: float,
    d_now_m: float,
    dt_hours: float,
    params: FrozenParams = PARAMS,
) -> float | None:
    """Rate the fire is closing on a road, km/h, or None if it isn't.

    Purely a measurement between two observations. Clamped from above by the
    literature sanity cap, which can only ever reduce it.
    """
    if dt_hours <= 0:
        return None
    if not (d_prev_m < float("inf") and d_now_m < float("inf")):
        return None
    closing_m_per_h = (d_prev_m - d_now_m) / dt_hours
    if closing_m_per_h <= 0:
        return None  # not approaching; no projection
    return min(closing_m_per_h / 1000.0, params.ROS_SANITY_CAP_KMH)


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def replay(
    detections: list[Detection],
    roads: tuple[Road, ...] = ROADS,
    params: FrozenParams = PARAMS,
) -> ReplayResult:
    """Run the model forward through the detection record, pass by pass.

    Strictly causal: at every step only detections already acquired are used.
    Nothing downstream of this function may look at ground truth.
    """
    assert_no_per_fire_tuning(params)

    n_raw = len(detections)
    usable = filter_confidence(detections, params.MIN_CONFIDENCE)
    passes = group_into_passes(usable, params.PASS_GAP_MINUTES)

    warnings: dict[str, Warning_] = {}
    confirmed: dict[str, dt.datetime] = {}
    pass_times: list[dt.datetime] = []
    states: dict[str, RoadState] = {}

    # Distance from each road to the fire at the previous pass, for the rate.
    prev_distance: dict[str, float] = {}
    prev_pass_time: dt.datetime | None = None

    for pass_dets in passes:
        pass_time = max(d.acq for d in pass_dets)
        pass_times.append(pass_time)

        dt_hours = (
            (pass_time - prev_pass_time).total_seconds() / 3600.0
            if prev_pass_time is not None
            else 0.0
        )

        for road in roads:
            d_now = min_distance_to_road(pass_dets, road)

            # --- 1. Confirmed crossing -------------------------------------
            if d_now <= params.ROAD_BUFFER_M and road.id not in confirmed:
                confirmed[road.id] = pass_time
                warnings.setdefault(
                    road.id,
                    Warning_(
                        road_id=road.id,
                        issued_at=pass_time,
                        kind="confirmed",
                        predicted_closure_at=pass_time,
                    ),
                )

            # --- 2. Measured approach --------------------------------------
            rate_kmh = None
            projected_at = None
            if road.id not in confirmed and prev_pass_time is not None:
                rate_kmh = measured_closing_rate_kmh(
                    prev_distance.get(road.id, float("inf")), d_now, dt_hours, params
                )
                if rate_kmh is not None and rate_kmh > 0:
                    hours_to_road = (d_now / 1000.0) / rate_kmh
                    # Refuse to project beyond the honesty horizon.
                    if hours_to_road <= params.MAX_PROJECTION_H:
                        projected_at = pass_time + dt.timedelta(hours=hours_to_road)
                        warnings.setdefault(
                            road.id,
                            Warning_(
                                road_id=road.id,
                                issued_at=pass_time,
                                kind="projected",
                                predicted_closure_at=projected_at,
                            ),
                        )

            states[road.id] = RoadState(
                road_id=road.id,
                status=Status.CROSSED if road.id in confirmed else Status.OPEN,
                as_of=pass_time,
                observation_age_h=0.0,
                confirmed_crossing_at=confirmed.get(road.id),
                projected_closure_at=projected_at,
                projection_issued_at=pass_time if projected_at else None,
                nearest_detection_m=None if d_now == float("inf") else d_now,
                measured_closing_rate_kmh=rate_kmh,
            )
            prev_distance[road.id] = d_now

        prev_pass_time = pass_time

    return ReplayResult(
        warnings=warnings,
        confirmed_crossings=confirmed,
        pass_times=pass_times,
        final_states=states,
        n_detections_used=len(usable),
        n_detections_dropped_low_confidence=n_raw - len(usable),
    )


def assess_at(
    detections: list[Detection],
    now: dt.datetime,
    roads: tuple[Road, ...] = ROADS,
    params: FrozenParams = PARAMS,
) -> dict[str, RoadState]:
    """State of every road as the model sees it at `now`.

    This is what the planning interface calls. Detections after `now` are
    discarded, so the interface can be pointed at any instant in a scenario and
    gets exactly what the model would have known then — no hindsight leak.

    When the newest usable observation is older than MAX_PROJECTION_H, every
    not-yet-crossed road comes back UNKNOWN. That is the correct answer, and it
    is the answer this tool will give most of the time.
    """
    assert_no_per_fire_tuning(params)

    visible = [d for d in detections if d.acq <= now]
    result = replay(visible, roads, params)

    if not result.pass_times:
        return {
            road.id: RoadState(
                road_id=road.id,
                status=Status.UNKNOWN,
                as_of=None,
                observation_age_h=None,
            )
            for road in roads
        }

    last_pass = max(result.pass_times)
    age_h = (now - last_pass).total_seconds() / 3600.0
    stale = age_h > params.MAX_PROJECTION_H

    out: dict[str, RoadState] = {}
    for road in roads:
        state = result.final_states.get(road.id)
        crossing = result.confirmed_crossings.get(road.id)

        if crossing is not None:
            # A confirmed crossing is a fact about the past. It does not expire
            # — the road does not un-burn because the satellite looked away.
            out[road.id] = dataclasses.replace(
                state,
                status=Status.CROSSED,
                observation_age_h=age_h,
                confirmed_crossing_at=crossing,
            )
            continue

        if stale:
            out[road.id] = RoadState(
                road_id=road.id,
                status=Status.UNKNOWN,
                as_of=last_pass,
                observation_age_h=age_h,
                nearest_detection_m=state.nearest_detection_m if state else None,
            )
            continue

        out[road.id] = dataclasses.replace(state, observation_age_h=age_h)

    return out
