"""Door-to-safe-zone planning.

Answers one question: *from this address, in this scenario, which way out was
still open, and how long did it have left?*

Past tense is not an accident. This is a planning tool. Every answer it gives
is anchored to a satellite observation that is already hours old, and the
interface is required to say so in the same breath as the recommendation. A
route is never presented as a live instruction.

Three rules are enforced here rather than left to the UI:

  1. A CROSSED exit is never recommended. Not ranked last — excluded.
  2. An UNKNOWN exit is reported as UNKNOWN. It is never upgraded to "probably
     open" because it was open at the last look.
  3. Every recommendation carries `as_of` and `observation_age_h`. A caller
     cannot render the advice without also having the staleness.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from .firms import PACIFIC_STANDARD, Detection
from .geo import Point, haversine_m, point_to_polyline_m
from .model import RoadState, Status, assess_at
from .params import PARAMS, FrozenParams
from .roads import KNOWN_PLACES, KnownPlace, ROADS, Road, ROADS_BY_ID

# Ranking: lower sorts first. CROSSED is excluded before ranking, never ranked.
_STATUS_RANK = {Status.OPEN: 0, Status.UNKNOWN: 1, Status.CROSSED: 2}


@dataclasses.dataclass(frozen=True)
class ExitOption:
    road_id: str
    road_name: str
    terminus_name: str
    status: Status

    # Distance from the origin to where you'd join this artery, then out.
    distance_to_road_m: float
    distance_along_road_m: float

    as_of: dt.datetime | None
    observation_age_h: float | None

    confirmed_crossing_at: dt.datetime | None
    projected_closure_at: dt.datetime | None
    minutes_until_closure: float | None
    measured_closing_rate_kmh: float | None

    @property
    def is_usable(self) -> bool:
        return self.status is not Status.CROSSED

    def headline(self) -> str:
        """One line a stranger can read in three seconds."""
        if self.status is Status.CROSSED:
            when = self.confirmed_crossing_at
            return f"CUT — fire crossed this road at {_hhmm(when)}"
        if self.status is Status.UNKNOWN:
            return f"UNKNOWN — no satellite look since {_hhmm(self.as_of)}"
        if self.minutes_until_closure is not None:
            return f"OPEN at last look — measured closing, ~{self.minutes_until_closure:.0f} min left"
        return f"OPEN at last look ({_hhmm(self.as_of)})"

    def detail(self) -> str:
        if self.status is Status.UNKNOWN:
            age = self.observation_age_h
            return (
                f"The last usable observation of this area was "
                f"{_hhmm(self.as_of)}"
                + (f", {age:.1f} h before this scenario time. " if age is not None else ". ")
                + "Firebreak does not guess what happened since. Treat this road "
                "as unknown, not as open."
            )
        if self.status is Status.CROSSED:
            return (
                "A detection pixel overlapped this road. Firebreak treats a "
                "crossed road as impassable and has no way to tell whether a "
                "lane or shoulder remained usable."
            )
        if self.minutes_until_closure is not None:
            return (
                f"Between the last two satellite passes the fire closed on this "
                f"road at a measured {self.measured_closing_rate_kmh:.1f} km/h. "
                f"Extrapolating that rate — not simulating fire — puts it at the "
                f"road around {_hhmm(self.projected_closure_at)}. This is an "
                f"extrapolation from two observations, nothing more."
            )
        return (
            f"No fire was detected on or measurably closing on this road as of "
            f"{_hhmm(self.as_of)}."
        )


@dataclasses.dataclass(frozen=True)
class EgressPlan:
    origin_label: str
    origin: Point
    scenario_time: dt.datetime

    recommended: ExitOption | None
    options: tuple[ExitOption, ...]

    as_of: dt.datetime | None
    observation_age_h: float | None

    @property
    def has_any_usable_exit(self) -> bool:
        """True only when some exit was observed OPEN.

        UNKNOWN does not count. An unobserved road is not a usable exit — it is
        an absence of information, and reporting it as usable is precisely the
        failure this tool exists to avoid.
        """
        return any(o.status is Status.OPEN for o in self.options)

    def summary(self) -> str:
        if self.recommended is None:
            return (
                "NO OPEN EXIT FOUND in the observations available at this "
                "scenario time. Every artery was either crossed or unobserved."
            )
        rec = self.recommended
        return (
            f"Nearest exit still open at the last satellite look: "
            f"{rec.road_name} toward {rec.terminus_name}. {rec.headline()}"
        )


def _hhmm(when: dt.datetime | None) -> str:
    """Local (PST) clock time.

    FIRMS timestamps are UTC. Every time a user sees must be local, or an
    01:42 overpass reads as 09:42 and the staleness story silently inverts.
    """
    if when is None:
        return "—"
    return f"{when.astimezone(PACIFIC_STANDARD):%H:%M}"


def snap_to_known_place(query: str) -> KnownPlace:
    """Map free text to the nearest known Paradise location.

    Firebreak deliberately does not geocode to a street address. The model's
    resolution is a 375 m pixel; presenting a house-level pin would imply a
    precision the data does not have. Snapping to a named neighbourhood keeps
    the output honest about its own resolution.
    """
    q = (query or "").strip().lower()
    if not q:
        return KNOWN_PLACES[0]

    for place in KNOWN_PLACES:
        if place.id == q:
            return place

    scored = [
        (sum(1 for tok in q.split() if tok in place.label.lower()), place)
        for place in KNOWN_PLACES
    ]
    best_score, best = max(scored, key=lambda pair: pair[0])
    return best if best_score > 0 else KNOWN_PLACES[0]


def _join_point_distance(origin: Point, road: Road) -> tuple[float, float]:
    """(metres to reach the road, metres along the road to its terminus)."""
    to_road = point_to_polyline_m(origin, road.segment_points())
    # Distance from the join to the terminus, approximated from the nearest
    # vertex onward. Adequate for ranking; not turn-by-turn.
    verts = road.segment_points()
    nearest_i = min(
        range(len(verts)), key=lambda i: haversine_m(origin, verts[i])
    )
    along = sum(
        haversine_m(verts[i], verts[i + 1]) for i in range(nearest_i, len(verts) - 1)
    )
    return to_road, along


def build_plan(
    detections: list[Detection],
    origin_query: str,
    scenario_time: dt.datetime,
    roads: tuple[Road, ...] = ROADS,
    params: FrozenParams = PARAMS,
) -> EgressPlan:
    """The whole planning answer for one address at one scenario instant."""
    place = snap_to_known_place(origin_query)
    states: dict[str, RoadState] = assess_at(detections, scenario_time, roads, params)

    options: list[ExitOption] = []
    for road in roads:
        state = states[road.id]
        to_road, along = _join_point_distance(place.point, road)
        options.append(
            ExitOption(
                road_id=road.id,
                road_name=road.name,
                terminus_name=road.terminus_name,
                status=state.status,
                distance_to_road_m=to_road,
                distance_along_road_m=along,
                as_of=state.as_of,
                observation_age_h=state.observation_age_h,
                confirmed_crossing_at=state.confirmed_crossing_at,
                projected_closure_at=state.projected_closure_at,
                minutes_until_closure=state.minutes_until_closure(scenario_time),
                measured_closing_rate_kmh=state.measured_closing_rate_kmh,
            )
        )

    # Rank: open before unknown; then nearest. A projected closure pushes an
    # option down but does not disqualify it — it may still be the best one.
    def sort_key(o: ExitOption) -> tuple[int, int, float]:
        has_deadline = 1 if o.minutes_until_closure is not None else 0
        return (_STATUS_RANK[o.status], has_deadline, o.distance_to_road_m)

    options.sort(key=sort_key)

    usable = [o for o in options if o.is_usable and o.status is Status.OPEN]
    recommended = usable[0] if usable else None

    any_state = next(iter(states.values()), None)
    return EgressPlan(
        origin_label=place.label,
        origin=place.point,
        scenario_time=scenario_time,
        recommended=recommended,
        options=tuple(options),
        as_of=any_state.as_of if any_state else None,
        observation_age_h=any_state.observation_age_h if any_state else None,
    )


def plan_to_dict(plan: EgressPlan) -> dict:
    """JSON-serialisable form for the interface."""
    return {
        "origin_label": plan.origin_label,
        "origin": {"lat": plan.origin[0], "lon": plan.origin[1]},
        "scenario_time": plan.scenario_time.isoformat(),
        # Local time: the UI reads the clock out of this string.
        "as_of": plan.as_of.astimezone(PACIFIC_STANDARD).isoformat() if plan.as_of else None,
        "observation_age_h": plan.observation_age_h,
        "summary": plan.summary(),
        "has_any_usable_exit": plan.has_any_usable_exit,
        "recommended_road_id": plan.recommended.road_id if plan.recommended else None,
        "options": [
            {
                "road_id": o.road_id,
                "road_name": o.road_name,
                "terminus_name": o.terminus_name,
                "status": o.status.value,
                "headline": o.headline(),
                "detail": o.detail(),
                "distance_to_road_km": round(o.distance_to_road_m / 1000.0, 2),
                "distance_along_road_km": round(o.distance_along_road_m / 1000.0, 2),
                "minutes_until_closure": (
                    round(o.minutes_until_closure) if o.minutes_until_closure else None
                ),
                "as_of": o.as_of.astimezone(PACIFIC_STANDARD).isoformat() if o.as_of else None,
                "observation_age_h": (
                    round(o.observation_age_h, 2) if o.observation_age_h is not None else None
                ),
                "polyline": [
                    {"lat": lat, "lon": lon}
                    for lat, lon in ROADS_BY_ID[o.road_id].polyline
                ],
            }
            for o in plan.options
        ],
    }
