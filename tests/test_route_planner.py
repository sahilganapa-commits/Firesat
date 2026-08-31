"""The routing layer: not "are you trapped" but "which way, and how long left".

Same hand-built graphs, same purity rule, same millisecond runtime.
"""

from __future__ import annotations

import datetime as dt

import networkx as nx
import pytest

from src.route_planner import RoutePlanner, add_straight_line_lengths
from src.solver import Frame
from tests.fire_stubs import Box, NoFire
from tests.test_solver import BANNED_IMPORTS, imported_modules

UTC = dt.timezone.utc

T0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
T1 = dt.datetime(2018, 11, 8, 15, 0, tzinfo=UTC)
T2 = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)

# A town with two ways out, one much shorter than the other.
#
#        1  (home)
#       / \
#  100 /   \ 500
#     2     3
# 100 |     | 100
#     4     5      4 and 5 are exits
#   (west) (east)
FORK_NODES = {
    1: (0.0, 0.0),
    2: (-1000.0, -1000.0),
    3: (1000.0, -1000.0),
    4: (-1000.0, -2000.0),
    5: (1000.0, -2000.0),
}
FORK_EDGES = [((1, 2), 100.0), ((2, 4), 100.0), ((1, 3), 500.0), ((3, 5), 100.0)]


def fork() -> nx.DiGraph:
    g = nx.DiGraph()
    for node, pos in FORK_NODES.items():
        g.add_node(node, pos=pos)
    for (u, v), length in FORK_EDGES:
        g.add_edge(u, v, length_m=length)
        g.add_edge(v, u, length_m=length)
    return g


def planner_for_fork() -> RoutePlanner:
    p = RoutePlanner(fork(), exits=[4, 5])
    p.snap_buildings({"home": (10.0, -10.0)})
    return p


# --------------------------------------------------------------------------
# Nearest open exit
# --------------------------------------------------------------------------


def test_route_is_the_shortest_path_to_the_nearest_exit() -> None:
    plan = planner_for_fork().run([Frame(T0, NoFire())])
    route = plan.frames[0].routes["home"]

    assert route.exit_node == 4
    assert route.nodes == (1, 2, 4)
    assert route.length_m == pytest.approx(200.0)


def test_nearest_open_exit_is_chosen_over_a_closer_burned_one() -> None:
    """When the short way out burns, the plan switches to the long way out."""
    planner = planner_for_fork()
    # A box across the 2 -> 4 segment on the west side only.
    plan = planner.run([Frame(T0, Box(-1100.0, -1600.0, -900.0, -1400.0))])
    route = plan.frames[0].routes["home"]

    assert route.exit_node == 5, "should fall back to the eastern exit"
    assert route.nodes == (1, 3, 5)
    assert route.length_m == pytest.approx(600.0)


def test_an_exit_inside_the_fire_is_not_an_open_exit() -> None:
    """Arriving at a burning exit is not escaping, so it cannot be recommended."""
    planner = planner_for_fork()
    plan = planner.run([Frame(T0, Box(-1200.0, -2200.0, -800.0, -1800.0))])
    route = plan.frames[0].routes["home"]

    assert 4 in plan.frames[0].closed_exits
    assert route.exit_node == 5


def test_no_route_when_every_exit_is_cut_off() -> None:
    planner = planner_for_fork()
    plan = planner.run(
        [
            Frame(
                T0,
                Box(-1100.0, -1600.0, -900.0, -1400.0),
            ),
            Frame(T1, Box(-1100.0, -1600.0, 1100.0, -1400.0)),
        ]
    )
    assert plan.frames[0].routes["home"] is not None
    assert plan.frames[1].routes["home"] is None


# --------------------------------------------------------------------------
# Direction
# --------------------------------------------------------------------------


def test_route_direction_matters_on_one_way_streets() -> None:
    """Routing walks backward from the exits — the route is the way OUT.

    One-way loop 1 -> 2 -> 3 -> 1 with 3 the exit. Burning 2 -> 3 leaves the
    resident at 1 with no way out, even though the exit can still reach them
    by driving 3 -> 1. A router that searched forward from the exit would hand
    node 1 a route it cannot legally drive.
    """
    nodes = {1: (0.0, 1000.0), 2: (1000.0, 1000.0), 3: (500.0, 0.0)}
    loop = nx.DiGraph()
    for n, p in nodes.items():
        loop.add_node(n, pos=p)
    for u, v in [(1, 2), (2, 3), (3, 1)]:
        loop.add_edge(u, v, length_m=100.0)

    planner = RoutePlanner(loop, exits=[3])
    planner.snap_buildings({"home": (0.0, 1000.0)})

    clean = planner.run([Frame(T0, NoFire())])
    assert clean.frames[0].routes["home"].nodes == (1, 2, 3)

    planner = RoutePlanner(loop, exits=[3])
    planner.snap_buildings({"home": (0.0, 1000.0)})
    burned = planner.run([Frame(T0, Box(700.0, 450.0, 800.0, 550.0))])

    assert burned.frames[0].routes["home"] is None
    # The exit can still drive to the home — which is exactly why forward search
    # is the wrong question to ask.
    forward = loop.copy()
    forward.remove_edge(2, 3)
    assert nx.has_path(forward, 3, 1)


def test_route_is_reported_from_the_building_outward() -> None:
    """Not from the exit inward. The first node is home; the last is the exit."""
    plan = planner_for_fork().run([Frame(T0, NoFire())])
    route = plan.frames[0].routes["home"]

    assert route.nodes[0] == route.origin_node
    assert route.nodes[-1] == route.exit_node


# --------------------------------------------------------------------------
# Route-closing time
# --------------------------------------------------------------------------


def test_route_closing_time_is_the_frame_the_last_path_disappeared() -> None:
    planner = planner_for_fork()
    plan = planner.run(
        [
            Frame(T0, NoFire()),
            Frame(T1, Box(-1100.0, -1600.0, -900.0, -1400.0)),   # west cut
            Frame(T2, Box(-1100.0, -1600.0, 1100.0, -1400.0)),   # east cut too
        ]
    )

    assert plan.route_closing_time["home"] == T2


def test_a_building_that_never_loses_its_route_has_no_closing_time() -> None:
    planner = planner_for_fork()
    plan = planner.run([Frame(T0, NoFire()), Frame(T1, NoFire())])
    assert plan.route_closing_time.get("home") is None


def test_route_closing_time_is_monotonic_and_never_reopens() -> None:
    """Once the last route is gone it stays gone, even if the fire shrinks."""
    planner = planner_for_fork()
    plan = planner.run(
        [
            Frame(T0, Box(-1100.0, -1600.0, 1100.0, -1400.0)),
            Frame(T1, NoFire()),
            Frame(T2, NoFire()),
        ]
    )

    assert plan.route_closing_time["home"] == T0
    assert [f.routes["home"] for f in plan.frames] == [None, None, None]


def test_minutes_until_close_counts_down_from_each_observation() -> None:
    planner = planner_for_fork()
    plan = planner.run(
        [
            Frame(T0, NoFire()),
            Frame(T1, Box(-1100.0, -1600.0, -900.0, -1400.0)),
            Frame(T2, Box(-1100.0, -1600.0, 1100.0, -1400.0)),
        ]
    )

    # T0 -> T2 is two hours; T1 -> T2 is one.
    assert plan.frames[0].routes["home"].minutes_until_close == pytest.approx(120.0)
    assert plan.frames[1].routes["home"].minutes_until_close == pytest.approx(60.0)
    assert plan.frames[2].routes["home"] is None


def test_minutes_until_close_is_none_when_the_route_never_closes() -> None:
    planner = planner_for_fork()
    plan = planner.run([Frame(T0, NoFire()), Frame(T1, NoFire())])
    assert plan.frames[0].routes["home"].minutes_until_close is None


# --------------------------------------------------------------------------
# Causality. The deadline is hindsight; the live answer must not use it.
# --------------------------------------------------------------------------


def test_live_plan_never_looks_at_a_frame_after_the_scenario_time() -> None:
    """`plan_at` is what an interface may show. It cannot see the future."""
    planner = planner_for_fork()
    plan = planner.run(
        [
            Frame(T0, NoFire()),
            Frame(T1, Box(-1100.0, -1600.0, -900.0, -1400.0)),
            Frame(T2, Box(-1100.0, -1600.0, 1100.0, -1400.0)),
        ]
    )

    live = plan.plan_at(T1)
    assert live.as_of == T1
    assert live.routes["home"].exit_node == 5
    # The route does close, at T2 — but nothing observed by T1 says so.
    assert live.routes["home"].minutes_until_close is None
    assert live.routes["home"].closed_at is None


def test_live_plan_reports_a_closure_that_has_already_been_observed() -> None:
    planner = planner_for_fork()
    plan = planner.run(
        [
            Frame(T0, NoFire()),
            Frame(T1, Box(-1100.0, -1600.0, 1100.0, -1400.0)),
        ]
    )

    live = plan.plan_at(T2)
    assert live.routes["home"] is None
    assert live.closed_at["home"] == T1


def test_plan_at_before_any_observation_is_empty() -> None:
    planner = planner_for_fork()
    plan = planner.run([Frame(T1, NoFire())])
    live = plan.plan_at(T0)
    assert live.as_of is None
    assert live.routes == {}


# --------------------------------------------------------------------------
# Agreement with the solver
# --------------------------------------------------------------------------


def test_a_building_is_trapped_exactly_when_it_has_no_route() -> None:
    """The two layers must never disagree about who is cut off."""
    planner = planner_for_fork()
    plan = planner.run(
        [
            Frame(T0, NoFire()),
            Frame(T1, Box(-1100.0, -1600.0, -900.0, -1400.0)),
            Frame(T2, Box(-1100.0, -1600.0, 1100.0, -1400.0)),
        ]
    )

    for frame in plan.frames:
        for building_id, route in frame.routes.items():
            node = plan.building_node[building_id]
            assert (route is None) == (node in frame.trapped)


# --------------------------------------------------------------------------
# Edge lengths
# --------------------------------------------------------------------------


def test_straight_line_lengths_are_filled_from_node_positions() -> None:
    g = nx.DiGraph()
    g.add_node(1, pos=(0.0, 0.0))
    g.add_node(2, pos=(300.0, 400.0))
    g.add_edge(1, 2)

    add_straight_line_lengths(g)
    assert g[1][2]["length_m"] == pytest.approx(500.0)


def test_an_edge_with_no_length_is_refused_not_silently_costed_at_one_metre() -> None:
    """networkx defaults a missing weight to 1. On a road network that is a
    30 km highway priced at one metre, and the route it produces is nonsense."""
    g = nx.DiGraph()
    for n, p in FORK_NODES.items():
        g.add_node(n, pos=p)
    g.add_edge(1, 2, length_m=100.0)
    g.add_edge(2, 4)   # no length

    with pytest.raises(ValueError, match="length_m"):
        RoutePlanner(g, exits=[4])


def test_existing_lengths_are_not_overwritten() -> None:
    g = nx.DiGraph()
    g.add_node(1, pos=(0.0, 0.0))
    g.add_node(2, pos=(300.0, 400.0))
    g.add_edge(1, 2, length_m=12.0)

    add_straight_line_lengths(g)
    assert g[1][2]["length_m"] == pytest.approx(12.0)


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


def test_route_planner_imports_nothing_geospatial_and_nothing_networked() -> None:
    offending = imported_modules("src/route_planner.py") & BANNED_IMPORTS
    assert offending == set(), f"src/route_planner.py must not import {sorted(offending)}"


def test_route_planner_imports_only_networkx_and_the_standard_library() -> None:
    import sys

    third_party = {
        name
        for name in imported_modules("src/route_planner.py")
        if name not in sys.stdlib_module_names and not name.startswith("_")
    }
    assert third_party <= {"networkx"}, f"unexpected dependency: {sorted(third_party)}"
