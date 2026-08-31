"""THE FIRMS PIPELINE — detections in, cumulative fire polygons out.

`src/firms.py` gets the rows (fetch, cache, parse, confidence filter, pass
grouping). This module turns those rows into the geometry the solver consumes:

  1. Project every detection to EPSG:32610 (UTM zone 10N), so everything
     downstream is in metres and a distance is a distance.
  2. Buffer each detection by ITS OWN footprint. VIIRS pixels are not all
     375 m; off-nadir they stretch, and the `scan` and `track` columns report
     how much for that specific pixel. Using the nominal size everywhere would
     understate the wide ones and overstate the narrow ones.
  3. Union the footprints into one burned area.
  4. Morphologically close it, by a deliberately small radius, to bridge
     sub-pixel slivers between footprints that ought to touch.
  5. Accumulate: frame N contains everything observed through N.

WHY THE CLOSE RADIUS IS SMALL — read this before changing it
------------------------------------------------------------
Morphological closing with radius r bridges any gap up to 2r wide. That is
useful for slivers and dangerous for everything else: close too hard and the
polygon marches across a river, a cleared field or a firebreak that the fire
never crossed, and the solver then deletes roads that were never touched.

So the radius is pinned below half a VIIRS pixel:

    CLOSE_RADIUS_M = PIXEL_SIZE_M / 4 = 93.75 m,  bridging gaps up to 187.5 m

The smallest gap a *missing detection* can leave is one whole pixel — 375 m —
because that is the ground the instrument looked at and reported no fire on.
187.5 m is comfortably under it, so closing can never span a missed pixel. It
can only pull together footprints that the sensor geometry says were already
adjacent. `tests/test_fire_data.py::test_closing_never_bridges_a_full_pixel_gap`
holds that line, and every frame reports `area_added_by_closing_km2` so the
answer to "did the fire really connect there?" is a number, not an opinion.

Set `close_radius_m=0.0` to switch closing off entirely.

This module MAY use shapely. `src/solver.py` and `src/route_planner.py` may
not, which is why a `FireFrame` is handed to them through the two-method
`FireMask` duck type rather than as a polygon.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from collections.abc import Iterable, Sequence

from shapely import prepared
from shapely.geometry import LineString, Point, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .firms import (
    CACHE_DIR,
    Detection,
    FirmsError,
    fetch_area,
    filter_confidence,
    group_into_passes,
)
from .params import PARAMS

Coord = tuple[float, float]

# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------

# Standard Processing: the quality-controlled archive. Correct for a hindcast.
HISTORICAL_PRODUCTS: tuple[str, ...] = ("VIIRS_SNPP_SP",)

# Near Real Time: only covers roughly the last two months, but covers it within
# hours. Both platforms, because two satellites means two looks per day.
RECENT_PRODUCTS: tuple[str, ...] = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT")

# How far back NRT reaches. Older than this and only the SP archive has it.
NRT_WINDOW_DAYS = 60

# The FIRMS area API caps a single request at ten days.
MAX_DAY_RANGE = 10

# See the module docstring. Changing this changes which roads burn.
CLOSE_RADIUS_M: float = PARAMS.PIXEL_SIZE_M / 4.0


def products_for(date: dt.date, *, today: dt.date | None = None) -> tuple[str, ...]:
    """Which FIRMS product actually carries data for `date`."""
    today = today or dt.date.today()
    age_days = (today - date).days
    return RECENT_PRODUCTS if age_days <= NRT_WINDOW_DAYS else HISTORICAL_PRODUCTS


# --------------------------------------------------------------------------
# EPSG:32610 — UTM zone 10N on WGS84
# --------------------------------------------------------------------------
#
# Krüger series, the same formulation PROJ uses. Sub-millimetre inside the
# zone, which is four orders of magnitude finer than a 375 m pixel. Hand-rolled
# so the pipeline needs no PROJ install; `tests/test_fire_data.py` checks every
# value against PROJ output.

_A = 6378137.0                      # WGS84 semi-major axis, metres
_F = 1.0 / 298.257223563            # WGS84 flattening
_K0 = 0.9996                        # UTM scale factor on the central meridian
_FALSE_EASTING = 500_000.0
_FALSE_NORTHING = 0.0               # northern hemisphere
_ZONE = 10
_LON0 = math.radians(-183.0 + 6 * _ZONE)   # -123°, zone 10 central meridian

_N = _F / (2.0 - _F)
_RADIUS = (_A / (1.0 + _N)) * (1.0 + _N**2 / 4.0 + _N**4 / 64.0)

_ALPHA = (
    _N / 2.0 - 2.0 * _N**2 / 3.0 + 5.0 * _N**3 / 16.0 + 41.0 * _N**4 / 180.0,
    13.0 * _N**2 / 48.0 - 3.0 * _N**3 / 5.0 + 557.0 * _N**4 / 1440.0,
    61.0 * _N**3 / 240.0 - 103.0 * _N**4 / 140.0,
    49561.0 * _N**4 / 161280.0,
)
_BETA = (
    _N / 2.0 - 2.0 * _N**2 / 3.0 + 37.0 * _N**3 / 96.0 - _N**4 / 360.0,
    _N**2 / 48.0 + _N**3 / 15.0 - 437.0 * _N**4 / 1440.0,
    17.0 * _N**3 / 480.0 - 37.0 * _N**4 / 840.0,
    4397.0 * _N**4 / 161280.0,
)
_DELTA = (
    2.0 * _N - 2.0 * _N**2 / 3.0 - 2.0 * _N**3 + 116.0 * _N**4 / 45.0,
    7.0 * _N**2 / 3.0 - 8.0 * _N**3 / 5.0 - 227.0 * _N**4 / 45.0,
    56.0 * _N**3 / 15.0 - 136.0 * _N**4 / 35.0,
    4279.0 * _N**4 / 630.0,
)

UTM_ZONE_10N_EPSG = 32610


def to_utm10(lat: float, lon: float) -> Coord:
    """WGS84 degrees -> EPSG:32610 (easting, northing) in metres."""
    phi = math.radians(lat)
    lam = math.radians(lon) - _LON0

    two_sqrt_n = 2.0 * math.sqrt(_N) / (1.0 + _N)
    t = math.sinh(math.atanh(math.sin(phi)) - two_sqrt_n * math.atanh(two_sqrt_n * math.sin(phi)))

    # Conformal coordinates on the sphere, before the series correction. Both
    # sums below must read these unmodified values, so they are not updated in
    # place.
    xi_p = math.atan2(t, math.cos(lam))
    eta_p = math.atanh(math.sin(lam) / math.hypot(1.0, t))

    xi = xi_p + sum(
        alpha * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
        for j, alpha in enumerate(_ALPHA, start=1)
    )
    eta = eta_p + sum(
        alpha * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
        for j, alpha in enumerate(_ALPHA, start=1)
    )

    return (
        _FALSE_EASTING + _K0 * _RADIUS * eta,
        _FALSE_NORTHING + _K0 * _RADIUS * xi,
    )


def to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """EPSG:32610 (easting, northing) in metres -> WGS84 (lat, lon) degrees."""
    xi = (northing - _FALSE_NORTHING) / (_K0 * _RADIUS)
    eta = (easting - _FALSE_EASTING) / (_K0 * _RADIUS)

    xi_p = xi - sum(
        beta * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        for j, beta in enumerate(_BETA, start=1)
    )
    eta_p = eta - sum(
        beta * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
        for j, beta in enumerate(_BETA, start=1)
    )

    chi = math.asin(max(-1.0, min(1.0, math.sin(xi_p) / math.cosh(eta_p))))
    phi = chi + sum(
        delta * math.sin(2 * j * chi) for j, delta in enumerate(_DELTA, start=1)
    )
    lam = _LON0 + math.atan2(math.sinh(eta_p), math.cos(xi_p))

    return (math.degrees(phi), math.degrees(lam))


# --------------------------------------------------------------------------
# Footprints
# --------------------------------------------------------------------------


def detection_footprint(detection: Detection) -> BaseGeometry:
    """The ground area one VIIRS pixel actually covered, in EPSG:32610.

    `scan` and `track` are that pixel's along-scan and along-track dimensions
    in kilometres. VIIRS flies a near-polar orbit, so at this latitude
    along-scan is close to east-west and along-track close to north-south; the
    footprint is modelled as an axis-aligned rectangle on that basis. The
    residual rotation is small next to a 375 m pixel and is not corrected for.

    Falls back to the nominal 375 m pixel when a row carries no scan/track,
    which some archive exports do.
    """
    width = detection.scan * 1000.0 if detection.scan > 0 else PARAMS.PIXEL_SIZE_M
    height = detection.track * 1000.0 if detection.track > 0 else PARAMS.PIXEL_SIZE_M

    x, y = to_utm10(detection.lat, detection.lon)
    return box(x - width / 2.0, y - height / 2.0, x + width / 2.0, y + height / 2.0)


def morphological_close(geometry: BaseGeometry, radius_m: float) -> BaseGeometry:
    """Dilate then erode, bridging gaps no wider than `2 * radius_m`.

    The result is unioned with the input so closing can only ever ADD area —
    a numerically eroded corner must never quietly un-burn ground.
    """
    if radius_m <= 0 or geometry.is_empty:
        return geometry
    closed = geometry.buffer(radius_m).buffer(-radius_m)
    return unary_union([geometry, closed])


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------


@dataclasses.dataclass
class FireFrame:
    """The cumulative burned area as of one satellite pass.

    Satisfies the solver's `FireMask` duck type — `crosses` and `contains` —
    which is the entire interface between this geometry-aware module and the
    pure graph core.
    """

    time: dt.datetime
    polygon: BaseGeometry
    n_detections: int
    n_new_detections: int
    area_km2: float
    area_added_by_closing_km2: float
    close_radius_m: float
    _prepared: prepared.PreparedGeometry = dataclasses.field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # Prepared geometry keeps the per-edge tests cheap: a town network has
        # thousands of edges and every one is tested against every frame.
        self._prepared = prepared.prep(self.polygon)

    def crosses(self, geometry: Sequence[Coord]) -> bool:
        """True when the burned area touches this road segment."""
        points = list(geometry)
        if not points:
            return False
        shape = Point(points[0]) if len(points) == 1 else LineString(points)
        return self._prepared.intersects(shape)

    def contains(self, point: Coord) -> bool:
        """True when this point lies inside the burned area."""
        return self._prepared.intersects(Point(point))

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.polygon.bounds


def build_frames(
    detections: Sequence[Detection],
    *,
    close_radius_m: float = CLOSE_RADIUS_M,
    min_confidence: str | None = None,
    pass_gap_minutes: float | None = None,
) -> list[FireFrame]:
    """Turn a detection record into one cumulative fire polygon per pass.

    Frames are CUMULATIVE by construction: each frame's polygon is unioned with
    the previous frame's, so burned ground can never be given back. That is not
    a convenience — it is the property the solver's monotonic trapped set rests
    on, and it is asserted in `test_burned_area_never_shrinks`.
    """
    min_confidence = min_confidence or PARAMS.MIN_CONFIDENCE
    pass_gap = pass_gap_minutes if pass_gap_minutes is not None else PARAMS.PASS_GAP_MINUTES

    usable = filter_confidence(list(detections), min_confidence)
    passes = group_into_passes(usable, pass_gap)

    frames: list[FireFrame] = []
    raw_union: BaseGeometry | None = None
    previous: BaseGeometry | None = None
    seen = 0

    for pass_detections in passes:
        footprints = [detection_footprint(d) for d in pass_detections]
        raw_union = unary_union(
            footprints if raw_union is None else [raw_union, *footprints]
        )
        seen += len(pass_detections)

        closed = morphological_close(raw_union, close_radius_m)
        if previous is not None:
            closed = unary_union([previous, closed])
        previous = closed

        frames.append(
            FireFrame(
                time=max(d.acq for d in pass_detections),
                polygon=closed,
                n_detections=seen,
                n_new_detections=len(pass_detections),
                area_km2=closed.area / 1e6,
                area_added_by_closing_km2=(closed.area - raw_union.area) / 1e6,
                close_radius_m=close_radius_m,
            )
        )

    return frames


# --------------------------------------------------------------------------
# Request shaping
# --------------------------------------------------------------------------


def bbox_string(bbox: tuple[float, float, float, float]) -> str:
    """FIRMS wants west,south,east,north. Getting this order wrong returns an
    empty CSV rather than an error, so it is spelled out in one place."""
    west, south, east, north = bbox
    if west > east or south > north:
        raise ValueError(
            f"bbox must be (west, south, east, north); got {bbox!r}, which is "
            "inside out"
        )
    return ",".join(f"{v:.4f}" for v in (west, south, east, north))


def ten_day_windows(start: dt.date, end: dt.date) -> list[tuple[str, int]]:
    """Split an inclusive date range into (start_date, day_range) requests.

    The FIRMS area endpoint accepts at most ten days per call, so any range
    longer than that has to be walked. Windows are contiguous and never
    overlap, so a detection is fetched exactly once.
    """
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")

    windows: list[tuple[str, int]] = []
    cursor = start
    while cursor <= end:
        remaining = (end - cursor).days + 1
        span = min(MAX_DAY_RANGE, remaining)
        windows.append((cursor.isoformat(), span))
        cursor += dt.timedelta(days=span)
    return windows


def fetch_detections(
    *,
    bbox: tuple[float, float, float, float],
    start: dt.date,
    end: dt.date,
    products: Iterable[str] | None = None,
    map_key: str | None = None,
    use_cache: bool = True,
    today: dt.date | None = None,
    fetch=fetch_area,
) -> list[Detection]:
    """Fetch every detection in a bbox and date range, across products.

    Walks the range in ten-day windows — the API's per-request cap — and asks
    each window for the product that actually holds data for ITS dates, not for
    the range's start date. A range that straddles the NRT boundary needs the
    archive for its old half and near-real-time for its recent half; picking
    one product for the whole range silently drops the other half of the fire.

    Responses are cached to `data/cache/` by `src.firms`, so a second run is
    offline and byte-identical. `fetch` is injectable so this orchestration can
    be tested without a network or a key.

    A product returning nothing is not an error — NOAA-20 has no data before
    2020, and asking for it is harmless.
    """
    bbox_string(bbox)  # validate the ordering before spending a request

    forced = tuple(products) if products is not None else None
    if forced is not None and not forced:
        raise ValueError("no FIRMS products selected")

    collected: dict[tuple, Detection] = {}
    failures: list[str] = []
    attempted = 0

    for date_s, day_range in ten_day_windows(start, end):
        window_start = dt.date.fromisoformat(date_s)
        for product in forced or products_for(window_start, today=today):
            attempted += 1
            try:
                batch = fetch(
                    area=bbox,
                    date=date_s,
                    day_range=day_range,
                    source=product,
                    map_key=map_key,
                    use_cache=use_cache,
                )
            except FirmsError as exc:
                failures.append(f"{product} {date_s}+{day_range}d: {exc}")
                continue
            for d in batch:
                # Two platforms legitimately see the same fire from different
                # orbits; keep both looks, but never the same pixel twice.
                collected[(d.lat, d.lon, d.acq, d.satellite)] = d

    if failures and len(failures) == attempted:
        raise FirmsError("every FIRMS request failed:\n  " + "\n  ".join(failures))

    return sorted(collected.values(), key=lambda d: d.acq)


def cache_location() -> str:
    return str(CACHE_DIR)
