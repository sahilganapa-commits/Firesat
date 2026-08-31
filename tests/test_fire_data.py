"""The FIRMS pipeline: detections in, cumulative fire polygons out.

The reference easting/northing values below were produced by PROJ (pyproj,
EPSG:4326 -> EPSG:32610). PROJ is not a dependency of this project; it was used
once to generate ground truth for the hand-rolled projection so that the
pipeline does not need it at runtime.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.fire_data import (
    CLOSE_RADIUS_M,
    HISTORICAL_PRODUCTS,
    RECENT_PRODUCTS,
    FireFrame,
    build_frames,
    detection_footprint,
    products_for,
    ten_day_windows,
    to_utm10,
    to_wgs84,
)
from src.firms import Detection
from src.params import PARAMS

UTC = dt.timezone.utc

# lat, lon, easting, northing  (EPSG:32610, from PROJ)
PROJ_REFERENCE = [
    ("paradise", 39.7596, -121.6219, 618047.7768, 4401983.7933),
    ("chico", 39.7285, -121.8375, 599623.7965, 4398270.0920),
    ("oroville", 39.5138, -121.5564, 624097.2850, 4374790.8627),
    ("zone_meridian", 40.0, -123.0, 500000.0000, 4427757.2187),
    ("equator", 0.0, -123.0, 500000.0000, 0.0000),
]


def det(
    lat: float,
    lon: float,
    when: dt.datetime,
    *,
    confidence: str = "h",
    scan: float = 0.375,
    track: float = 0.375,
) -> Detection:
    return Detection(
        lat=lat, lon=lon, acq=when, confidence=confidence,
        bright_ti4=340.0, bright_ti5=300.0, frp=20.0,
        satellite="N", daynight="D", scan=scan, track=track,
    )


# --------------------------------------------------------------------------
# Projection to EPSG:32610
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,lat,lon,easting,northing", PROJ_REFERENCE)
def test_projection_matches_proj(name, lat, lon, easting, northing) -> None:
    x, y = to_utm10(lat, lon)
    assert x == pytest.approx(easting, abs=0.001)
    assert y == pytest.approx(northing, abs=0.001)


@pytest.mark.parametrize("name,lat,lon,easting,northing", PROJ_REFERENCE)
def test_projection_round_trips(name, lat, lon, easting, northing) -> None:
    back_lat, back_lon = to_wgs84(*to_utm10(lat, lon))
    assert back_lat == pytest.approx(lat, abs=1e-9)
    assert back_lon == pytest.approx(lon, abs=1e-9)


def test_projected_units_are_metres() -> None:
    """A known ground distance comes back in metres, not degrees."""
    a = to_utm10(39.7596, -121.6219)   # Paradise
    b = to_utm10(39.7285, -121.8375)   # Chico
    metres = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    assert 18_000 < metres < 20_000


# --------------------------------------------------------------------------
# Per-detection footprints
# --------------------------------------------------------------------------


def test_footprint_area_is_the_detections_own_scan_by_track() -> None:
    when = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    footprint = detection_footprint(det(39.76, -121.62, when, scan=0.5, track=0.4))
    # 500 m by 400 m.
    assert footprint.area == pytest.approx(500.0 * 400.0, rel=1e-6)


def test_a_wider_scan_makes_a_wider_footprint() -> None:
    """Off-nadir pixels are bigger, and the polygon must reflect that."""
    when = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    nadir = detection_footprint(det(39.76, -121.62, when, scan=0.375, track=0.375))
    edge = detection_footprint(det(39.76, -121.62, when, scan=0.8, track=0.75))
    assert edge.area > nadir.area * 4


def test_footprint_falls_back_to_the_nominal_pixel_when_scan_is_missing() -> None:
    when = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    footprint = detection_footprint(det(39.76, -121.62, when, scan=0.0, track=0.0))
    assert footprint.area == pytest.approx(PARAMS.PIXEL_SIZE_M**2, rel=1e-6)


# --------------------------------------------------------------------------
# Frames are cumulative
# --------------------------------------------------------------------------


def test_one_frame_per_satellite_pass() -> None:
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    detections = [
        det(39.76, -121.62, t0),
        det(39.76, -121.61, t0 + dt.timedelta(minutes=1)),   # same pass
        det(39.76, -121.60, t0 + dt.timedelta(hours=12)),    # next pass
    ]
    frames = build_frames(detections)
    assert len(frames) == 2
    assert frames[0].n_new_detections == 2
    assert frames[1].n_new_detections == 1


def test_frame_time_is_the_pass_acquisition_time() -> None:
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    frames = build_frames([det(39.76, -121.62, t0)])
    assert frames[0].time == t0


def test_frame_n_contains_everything_through_n() -> None:
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    detections = [
        det(39.76, -121.62, t0),
        det(39.80, -121.55, t0 + dt.timedelta(hours=12)),
    ]
    frames = build_frames(detections)

    assert frames[1].polygon.covers(frames[0].polygon)
    assert frames[1].n_detections == 2
    assert frames[1].area_km2 > frames[0].area_km2


def test_burned_area_never_shrinks() -> None:
    """The cumulative polygon is monotonic — ground does not un-burn."""
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    detections = [
        det(39.76, -121.62, t0),
        det(39.80, -121.55, t0 + dt.timedelta(hours=12)),
        det(39.76, -121.62, t0 + dt.timedelta(hours=24)),   # back where it started
    ]
    frames = build_frames(detections)
    areas = [f.area_km2 for f in frames]
    assert areas == sorted(areas)
    assert frames[2].polygon.covers(frames[0].polygon)


def test_no_detections_makes_no_frames() -> None:
    assert build_frames([]) == []


def test_low_confidence_detections_are_dropped() -> None:
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    frames = build_frames([det(39.76, -121.62, t0, confidence="l")])
    assert frames == []


# --------------------------------------------------------------------------
# Morphological closing. "Did the fire really connect there?"
# --------------------------------------------------------------------------


def test_close_radius_is_below_half_a_pixel() -> None:
    """The bound that makes the next two tests provable rather than lucky."""
    assert CLOSE_RADIUS_M < PARAMS.PIXEL_SIZE_M / 2.0


def test_closing_bridges_a_sliver_between_touching_footprints() -> None:
    """Two footprints a few tens of metres apart are one fire, not two."""
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    east = to_utm10(39.76, -121.62)
    # 50 m gap: centres 425 m apart, footprints 375 m wide.
    near_lat, near_lon = to_wgs84(east[0] + 425.0, east[1])

    frames = build_frames([det(39.76, -121.62, t0), det(near_lat, near_lon, t0)])
    assert frames[0].polygon.geom_type == "Polygon", "the sliver should have closed"


def test_closing_never_bridges_a_full_pixel_gap() -> None:
    """A missing pixel is evidence the sensor saw no fire there.

    Closing with radius r can only bridge gaps up to 2r wide. With r below half
    a pixel, a 375 m gap — the smallest a missed detection can leave — is never
    invented across. This is the property a judge asking "did the fire really
    connect there?" is entitled to.
    """
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    origin = to_utm10(39.76, -121.62)
    # Centres 750 m apart: two 375 m footprints with a clear 375 m gap between.
    far_lat, far_lon = to_wgs84(origin[0] + 750.0, origin[1])

    frames = build_frames([det(39.76, -121.62, t0), det(far_lat, far_lon, t0)])
    assert frames[0].polygon.geom_type == "MultiPolygon"
    assert len(frames[0].polygon.geoms) == 2, "a real gap must stay a gap"


def test_area_added_by_closing_is_reported() -> None:
    """Closing is auditable: the frame says how much fire it invented."""
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    origin = to_utm10(39.76, -121.62)
    near_lat, near_lon = to_wgs84(origin[0] + 425.0, origin[1])

    frames = build_frames([det(39.76, -121.62, t0), det(near_lat, near_lon, t0)])
    assert frames[0].area_added_by_closing_km2 > 0.0
    assert frames[0].close_radius_m == CLOSE_RADIUS_M
    # A sliver, not a landscape.
    assert frames[0].area_added_by_closing_km2 < 0.05


def test_closing_adds_nothing_to_a_single_detection() -> None:
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    frames = build_frames([det(39.76, -121.62, t0)])
    assert frames[0].area_added_by_closing_km2 == pytest.approx(0.0, abs=1e-9)


def test_closing_can_be_switched_off_entirely() -> None:
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    origin = to_utm10(39.76, -121.62)
    near_lat, near_lon = to_wgs84(origin[0] + 425.0, origin[1])
    detections = [det(39.76, -121.62, t0), det(near_lat, near_lon, t0)]

    frames = build_frames(detections, close_radius_m=0.0)
    assert frames[0].polygon.geom_type == "MultiPolygon"
    assert frames[0].area_added_by_closing_km2 == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------
# The frame is a FireMask the pure solver can consume
# --------------------------------------------------------------------------


def test_frame_answers_crosses_and_contains() -> None:
    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    frame = build_frames([det(39.76, -121.62, t0)])[0]
    centre = to_utm10(39.76, -121.62)

    assert frame.contains(centre)
    assert not frame.contains((centre[0] + 10_000.0, centre[1]))
    assert frame.crosses([(centre[0] - 1000.0, centre[1]), (centre[0] + 1000.0, centre[1])])
    assert not frame.crosses(
        [(centre[0] - 10_000.0, centre[1]), (centre[0] - 9_000.0, centre[1])]
    )


def test_a_real_frame_drives_the_pure_solver() -> None:
    """The duck-typed boundary actually fits: FIRMS in, trapped nodes out."""
    import networkx as nx

    from src.solver import EgressSolver, Frame

    t0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    home = to_utm10(39.7700, -121.6219)
    middle = to_utm10(39.7600, -121.6219)
    exit_node = to_utm10(39.7500, -121.6219)

    graph = nx.DiGraph()
    for node, pos in enumerate([home, middle, exit_node]):
        graph.add_node(node, pos=pos)
    graph.add_edge(0, 1); graph.add_edge(1, 0)
    graph.add_edge(1, 2); graph.add_edge(2, 1)

    # A detection sitting on the midpoint of the lower segment.
    burn_lat, burn_lon = to_wgs84(*((middle[0] + exit_node[0]) / 2, (middle[1] + exit_node[1]) / 2))
    frame = build_frames([det(burn_lat, burn_lon, t0)])[0]

    result = EgressSolver(graph, exits=[2]).step(Frame(frame.time, frame))
    assert result.trapped == frozenset({0, 1})


# --------------------------------------------------------------------------
# Request shaping
# --------------------------------------------------------------------------


def test_bbox_is_west_south_east_north() -> None:
    from src.fire_data import bbox_string

    assert bbox_string((-121.9, 39.5, -121.4, 39.9)) == "-121.9000,39.5000,-121.4000,39.9000"


def test_ten_day_windows_cover_the_range_without_gaps() -> None:
    windows = ten_day_windows(dt.date(2018, 11, 1), dt.date(2018, 11, 25))
    assert windows == [("2018-11-01", 10), ("2018-11-11", 10), ("2018-11-21", 5)]


def test_a_single_day_is_one_window() -> None:
    assert ten_day_windows(dt.date(2018, 11, 8), dt.date(2018, 11, 8)) == [("2018-11-08", 1)]


def test_no_window_ever_exceeds_the_api_limit() -> None:
    windows = ten_day_windows(dt.date(2018, 1, 1), dt.date(2018, 12, 31))
    assert all(1 <= day_range <= 10 for _, day_range in windows)
    assert sum(day_range for _, day_range in windows) == 365


def test_end_before_start_is_rejected() -> None:
    with pytest.raises(ValueError, match="before"):
        ten_day_windows(dt.date(2018, 11, 25), dt.date(2018, 11, 1))


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------


def test_a_2018_fire_uses_the_standard_processing_archive() -> None:
    products = products_for(dt.date(2018, 11, 8), today=dt.date(2026, 8, 30))
    assert products == HISTORICAL_PRODUCTS
    assert products == ("VIIRS_SNPP_SP",)


def test_a_recent_fire_uses_both_near_real_time_products() -> None:
    products = products_for(dt.date(2026, 8, 25), today=dt.date(2026, 8, 30))
    assert products == RECENT_PRODUCTS
    assert products == ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT")


def test_the_nrt_boundary_is_explicit() -> None:
    """NRT covers roughly the last two months; older than that must use SP."""
    today = dt.date(2026, 8, 30)
    assert products_for(today - dt.timedelta(days=30), today=today) == RECENT_PRODUCTS
    assert products_for(today - dt.timedelta(days=120), today=today) == HISTORICAL_PRODUCTS


# --------------------------------------------------------------------------
# Fetch orchestration (no network: the fetcher is injected)
# --------------------------------------------------------------------------


class RecordingFetcher:
    """Stands in for `firms.fetch_area`, recording what was asked for."""

    def __init__(self, rows: dict[str, list[Detection]] | None = None) -> None:
        self.calls: list[dict] = []
        self.rows = rows or {}

    def __call__(self, *, area, date, day_range, source, map_key, use_cache):
        self.calls.append(
            {"area": area, "date": date, "day_range": day_range, "source": source}
        )
        return list(self.rows.get(source, []))


BBOX = (-121.9, 39.5, -121.4, 39.9)


def test_every_window_and_product_is_requested() -> None:
    from src.fire_data import fetch_detections

    fetcher = RecordingFetcher()
    fetch_detections(
        bbox=BBOX,
        start=dt.date(2018, 11, 1),
        end=dt.date(2018, 11, 25),
        today=dt.date(2026, 8, 30),
        fetch=fetcher,
    )

    assert [c["date"] for c in fetcher.calls] == ["2018-11-01", "2018-11-11", "2018-11-21"]
    assert {c["source"] for c in fetcher.calls} == {"VIIRS_SNPP_SP"}
    assert all(c["area"] == BBOX for c in fetcher.calls)


def test_a_range_spanning_the_nrt_boundary_asks_each_window_for_its_own_product() -> None:
    """A 2018 window needs the archive; a window from last week needs NRT.

    Choosing one product for the whole range silently drops half the fire.
    """
    from src.fire_data import fetch_detections

    today = dt.date(2026, 8, 30)
    fetcher = RecordingFetcher()
    fetch_detections(
        bbox=BBOX,
        start=today - dt.timedelta(days=75),
        end=today,
        today=today,
        fetch=fetcher,
    )

    sources = {c["source"] for c in fetcher.calls}
    assert "VIIRS_SNPP_SP" in sources, "the older window needs the archive product"
    assert "VIIRS_SNPP_NRT" in sources, "the recent window needs near-real-time"


def test_the_same_pixel_from_two_products_is_kept_once() -> None:
    from src.fire_data import fetch_detections

    when = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
    duplicate = det(39.76, -121.62, when)
    fetcher = RecordingFetcher({"VIIRS_SNPP_SP": [duplicate, duplicate]})

    out = fetch_detections(
        bbox=BBOX,
        start=dt.date(2018, 11, 8),
        end=dt.date(2018, 11, 8),
        today=dt.date(2026, 8, 30),
        fetch=fetcher,
    )
    assert len(out) == 1


def test_both_platforms_seeing_the_same_fire_are_both_kept() -> None:
    """SNPP and NOAA-20 are two looks, not a duplicate."""
    from src.fire_data import fetch_detections

    when = dt.datetime(2026, 8, 25, 14, 0, tzinfo=UTC)
    snpp = dataclasses_replace(det(39.76, -121.62, when), satellite="N")
    noaa = dataclasses_replace(det(39.76, -121.62, when), satellite="1")
    fetcher = RecordingFetcher(
        {"VIIRS_SNPP_NRT": [snpp], "VIIRS_NOAA20_NRT": [noaa]}
    )

    out = fetch_detections(
        bbox=BBOX,
        start=dt.date(2026, 8, 25),
        end=dt.date(2026, 8, 25),
        today=dt.date(2026, 8, 30),
        fetch=fetcher,
    )
    assert len(out) == 2


def test_an_inside_out_bbox_is_refused_before_any_request() -> None:
    from src.fire_data import fetch_detections

    fetcher = RecordingFetcher()
    with pytest.raises(ValueError, match="west"):
        fetch_detections(
            bbox=(-121.4, 39.5, -121.9, 39.9),   # east and west swapped
            start=dt.date(2018, 11, 8),
            end=dt.date(2018, 11, 8),
            fetch=fetcher,
        )
    assert fetcher.calls == []


def dataclasses_replace(detection: Detection, **changes) -> Detection:
    import dataclasses

    return dataclasses.replace(detection, **changes)
