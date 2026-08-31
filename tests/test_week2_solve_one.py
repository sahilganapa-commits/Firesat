"""The week-2 answer: at one timestamp, who is cut off and what is their plan."""

from __future__ import annotations

import datetime as dt

import networkx as nx
import pytest

from src.firms import Detection
from src.roads import KNOWN_PLACES_BY_ID, ROADS_BY_ID
from scripts.week2_solve_one import (
    MAX_SNAP_M,
    build_demo_network,
    format_report,
    severe_demo_detections,
    solve_at,
)

UTC = dt.timezone.utc

EARLY = dt.datetime(2018, 11, 8, 9, 42, tzinfo=UTC)    # 01:42 PST overpass
LATE = dt.datetime(2018, 11, 8, 21, 30, tzinfo=UTC)    # 13:30 PST overpass


def band(lat: float, lon_from: float, lon_to: float, when: dt.datetime) -> list[Detection]:
    """A dense east-west line of detections — a front crossing the ridge."""
    out: list[Detection] = []
    lon = lon_from
    while lon <= lon_to:
        out.append(
            Detection(
                lat=lat, lon=lon, acq=when, confidence="h",
                bright_ti4=340.0, bright_ti5=300.0, frp=25.0,
                satellite="N", daynight="D", scan=0.4, track=0.4,
            )
        )
        lon += 0.003
    return out


def far_away(when: dt.datetime) -> list[Detection]:
    """A detection nowhere near any road, so a frame exists but cuts nothing."""
    return [
        Detection(
            lat=39.95, lon=-121.30, acq=when, confidence="h",
            bright_ti4=340.0, bright_ti5=300.0, frp=25.0,
            satellite="N", daynight="D", scan=0.4, track=0.4,
        )
    ]


# --------------------------------------------------------------------------
# The demo network
# --------------------------------------------------------------------------


def test_demo_network_is_a_single_connected_component() -> None:
    """Isolated corridors would make the graph solver pointless."""
    net = build_demo_network()
    assert nx.is_weakly_connected(net.graph)


def test_there_is_one_exit_per_artery() -> None:
    net = build_demo_network()
    assert len(net.exits) == len(ROADS_BY_ID)
    for node in net.exits:
        assert node in net.graph


def test_an_exit_is_the_far_end_of_its_artery() -> None:
    net = build_demo_network()
    for node in net.exits:
        assert net.graph.nodes[node]["is_exit"] is True
        assert net.graph.nodes[node]["terminus_name"]


def test_schematic_connectors_are_labelled_as_such() -> None:
    """Cross streets are not surveyed. The graph must admit that in the data."""
    net = build_demo_network()
    schematic = [
        (u, v) for u, v, d in net.graph.edges(data=True) if d.get("schematic")
    ]
    assert schematic, "the demo network relies on connectors; they must be flagged"
    for u, v in schematic:
        assert net.graph[u][v]["road_name"] == "schematic connector"


def test_artery_edges_are_not_flagged_schematic() -> None:
    net = build_demo_network()
    real = [
        d for _, _, d in net.graph.edges(data=True) if not d.get("schematic")
    ]
    assert real
    assert all(d["road_id"] in ROADS_BY_ID for d in real)


def test_node_positions_are_utm_zone_10_metres() -> None:
    net = build_demo_network()
    for _, data in net.graph.nodes(data=True):
        x, y = data["pos"]
        assert 400_000 < x < 800_000, "easting outside UTM zone 10"
        assert 4_300_000 < y < 4_500_000, "northing not in northern California"


def test_edges_carry_a_length_in_metres() -> None:
    net = build_demo_network()
    for _, _, data in net.graph.edges(data=True):
        assert data["length_m"] > 0


def test_every_known_place_snaps_to_the_network() -> None:
    net = build_demo_network()
    assert set(net.buildings) == set(KNOWN_PLACES_BY_ID)
    for point in net.buildings.values():
        assert len(point) == 2


def test_the_snap_limit_is_stated_not_unlimited() -> None:
    assert MAX_SNAP_M > 0


# --------------------------------------------------------------------------
# Solving at one timestamp
# --------------------------------------------------------------------------


def test_a_front_across_the_ridge_cuts_the_north_off() -> None:
    """A band south of town severs every artery below it."""
    detections = band(39.7500, -121.6600, -121.5700, EARLY)
    report = solve_at(detections, EARLY, build_demo_network())

    assert "magalia" in report.cut_off_ids
    assert "paradise_north" in report.cut_off_ids


def test_nobody_is_cut_off_by_a_fire_that_touches_no_road() -> None:
    report = solve_at(far_away(EARLY), EARLY, build_demo_network())
    assert report.cut_off_ids == ()
    assert report.n_frames == 1


def test_a_building_still_open_gets_a_route_to_a_named_exit() -> None:
    report = solve_at(far_away(EARLY), EARLY, build_demo_network())
    plan = report.plans["paradise_downtown"]

    assert plan.status == "open"
    assert plan.exit_name
    assert plan.route_nodes[0] == plan.origin_node
    assert plan.route_nodes[-1] == plan.exit_node
    assert plan.length_km > 0
    assert plan.road_names, "a route a human can read"


def test_a_cut_off_building_has_no_route_and_a_cutoff_time() -> None:
    detections = band(39.7500, -121.6600, -121.5700, EARLY)
    report = solve_at(detections, EARLY, build_demo_network())
    plan = report.plans["magalia"]

    assert plan.status == "cut_off"
    assert plan.route_nodes == ()
    assert plan.cutoff_at == EARLY


# --------------------------------------------------------------------------
# Causality — the tool may not read a satellite pass that has not happened
# --------------------------------------------------------------------------


def test_a_later_overpass_cannot_reach_backward_in_time() -> None:
    detections = far_away(EARLY) + band(39.7500, -121.6600, -121.5700, LATE)
    net = build_demo_network()

    before = solve_at(detections, LATE - dt.timedelta(hours=1), net)
    after = solve_at(detections, LATE, build_demo_network())

    assert before.cut_off_ids == ()
    assert "magalia" in after.cut_off_ids


def test_a_timestamp_before_any_observation_reports_no_observation() -> None:
    """Nothing seen is UNKNOWN, never "cut off". Absence of information is not
    a finding, and reporting it as one is the failure this tool exists to
    avoid."""
    report = solve_at(far_away(LATE), EARLY, build_demo_network())
    assert report.as_of is None
    assert report.n_frames == 0
    assert report.cut_off_ids == ()
    assert {p.status for p in report.plans.values()} == {"unknown"}


def test_observation_age_is_measured_from_the_last_pass() -> None:
    report = solve_at(far_away(EARLY), EARLY + dt.timedelta(hours=6), build_demo_network())
    assert report.observation_age_h == pytest.approx(6.0, abs=0.01)
    assert report.is_stale is True


def test_a_fresh_observation_is_not_stale() -> None:
    report = solve_at(far_away(EARLY), EARLY + dt.timedelta(minutes=5), build_demo_network())
    assert report.is_stale is False


# --------------------------------------------------------------------------
# What the operator actually reads
# --------------------------------------------------------------------------


def test_the_printed_report_names_the_cut_off_places() -> None:
    detections = band(39.7500, -121.6600, -121.5700, EARLY)
    text = format_report(solve_at(detections, EARLY, build_demo_network()))

    assert "Magalia" in text
    assert "CUT OFF" in text


def test_the_printed_report_states_the_observation_age() -> None:
    report = solve_at(far_away(EARLY), EARLY + dt.timedelta(hours=6), build_demo_network())
    text = format_report(report)

    assert "6.0" in text
    assert "hours old" in text.lower()


def test_the_printed_report_admits_the_connectors_are_schematic() -> None:
    text = format_report(solve_at(far_away(EARLY), EARLY, build_demo_network()))
    assert "schematic" in text.lower()


def test_the_printed_report_says_this_is_not_navigation() -> None:
    text = format_report(solve_at(far_away(EARLY), EARLY, build_demo_network()))
    assert "not" in text.lower() and "navigation" in text.lower()


def test_exits_sharing_a_terminus_are_still_told_apart() -> None:
    """Two arteries both end at CA-70. Printing "CA-70 south" twice is useless."""
    report = solve_at(far_away(EARLY), EARLY, build_demo_network())
    assert len(set(report.open_exits)) == len(report.open_exits)
    assert len(report.open_exits) == 5


# --------------------------------------------------------------------------
# The severe demo scenario — the one that shows the tool doing its job
# --------------------------------------------------------------------------


def test_the_severe_demo_scenario_cuts_part_of_the_town_off() -> None:
    detections = severe_demo_detections()
    at = max(d.acq for d in detections)
    report = solve_at(detections, at, build_demo_network())

    assert report.cut_off_ids, "the severe scenario must actually sever the ridge"


def test_the_severe_demo_scenario_leaves_someone_with_a_plan() -> None:
    """A report where everyone is doomed demonstrates the routing layer not at all."""
    detections = severe_demo_detections()
    at = max(d.acq for d in detections)
    report = solve_at(detections, at, build_demo_network())

    still_open = [p for p in report.plans.values() if p.status == "open"]
    assert still_open, "someone must still have a route, or there is no plan to show"


def test_the_severe_demo_scenario_is_harmless_at_its_first_pass() -> None:
    """It has to develop over time, or the cutoff time means nothing."""
    detections = severe_demo_detections()
    first = min(d.acq for d in detections)
    report = solve_at(detections, first, build_demo_network())

    assert report.cut_off_ids == ()


def test_the_cut_off_set_only_grows_across_the_whole_scenario() -> None:
    """Monotonicity, end to end through the real pipeline.

    `test_solver.py` proves this on a hand-built graph. This proves it survives
    FIRMS parsing, projection, footprint buffering, morphological closing and
    frame accumulation — every stage where a shrinking polygon could creep in
    and quietly un-trap somebody.
    """
    detections = severe_demo_detections()
    start = min(d.acq for d in detections)
    end = max(d.acq for d in detections)

    previous: set[str] = set()
    for step in range(0, 26):
        when = start + (end - start) * step / 25
        report = solve_at(detections, when, build_demo_network())
        cut = set(report.cut_off_ids)
        assert previous <= cut, f"the cut-off set shrank by {when.isoformat()}"
        previous = cut

    assert previous, "the scenario must end with somebody cut off"


def test_the_printed_report_reports_area_invented_by_closing() -> None:
    """The judge's question, answered in the output rather than in a docstring."""
    detections = band(39.7500, -121.6600, -121.5700, EARLY)
    text = format_report(solve_at(detections, EARLY, build_demo_network()))
    assert "closing" in text.lower()
