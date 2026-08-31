"""The egress solver: who can no longer drive out, and from when.

Every graph here is hand-built and tiny. There is no data file, no API key and
no geospatial import in the module under test — which is why this file runs in
milliseconds and is never blocked on a FIRMS download.
"""

from __future__ import annotations

import datetime as dt

import networkx as nx
import pytest

from src.solver import EgressSolver, Frame
from tests.fire_stubs import Box, Boxes, NoFire

UTC = dt.timezone.utc

T0 = dt.datetime(2018, 11, 8, 14, 0, tzinfo=UTC)
T1 = dt.datetime(2018, 11, 8, 15, 0, tzinfo=UTC)
T2 = dt.datetime(2018, 11, 8, 16, 0, tzinfo=UTC)


def build(nodes: dict[int, tuple[float, float]], edges, two_way: bool = True) -> nx.DiGraph:
    """A directed road graph. `edges` are (u, v) pairs in the drivable direction."""
    g = nx.DiGraph()
    for node, pos in nodes.items():
        g.add_node(node, pos=pos)
    for u, v in edges:
        g.add_edge(u, v)
        if two_way:
            g.add_edge(v, u)
    return g


# A ridge town: two neighbourhoods behind one junction, one road out.
#
#   1 (north)              4 (east)
#     \                   /
#      +----- 2 (junction)
#             |
#             3  EXIT
RIDGE_NODES = {1: (0.0, 0.0), 2: (0.0, -1000.0), 3: (0.0, -2000.0), 4: (1000.0, -1000.0)}
RIDGE_EDGES = [(1, 2), (2, 3), (4, 2)]


def ridge() -> nx.DiGraph:
    return build(RIDGE_NODES, RIDGE_EDGES)


# --------------------------------------------------------------------------
# The reversed graph
# --------------------------------------------------------------------------


def test_graph_is_reversed_once_up_front() -> None:
    """The solver walks a reversed graph, and builds it exactly once."""
    solver = EgressSolver(ridge(), exits=[3])

    # Reversed: a forward road u->v is walkable v->u when searching out of an exit.
    assert solver.graph_rev.has_edge(2, 1)
    assert solver.graph_rev.has_edge(3, 2)

    before = id(solver.graph_rev)
    solver.step(Frame(T0, NoFire()))
    solver.step(Frame(T1, NoFire()))
    assert id(solver.graph_rev) == before, "reversed graph must not be rebuilt per frame"


def test_the_source_graph_is_not_mutated() -> None:
    """Burning roads must not corrupt the caller's network."""
    graph = ridge()
    solver = EgressSolver(graph, exits=[3])
    solver.step(Frame(T0, Box(-500, -1600, 500, -1400)))
    assert graph.has_edge(2, 3)
    assert graph.number_of_edges() == 6


# --------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------


def test_no_fire_means_nobody_is_trapped() -> None:
    solver = EgressSolver(ridge(), exits=[3])
    result = solver.step(Frame(T0, NoFire()))

    assert result.trapped == frozenset()
    assert result.reached == frozenset({1, 2, 3, 4})


def test_burning_the_only_road_out_traps_everyone_behind_it() -> None:
    """A box across the 2-3 segment severs the whole town from its exit."""
    solver = EgressSolver(ridge(), exits=[3])
    result = solver.step(Frame(T0, Box(-500, -1600, 500, -1400)))

    assert result.burned_edges == frozenset({(2, 3), (3, 2)})
    assert result.trapped == frozenset({1, 2, 4})
    assert result.reached == frozenset({3})


def test_burning_a_spur_traps_only_that_spur() -> None:
    """Cutting 4-2 strands the east neighbourhood, not the north one."""
    solver = EgressSolver(ridge(), exits=[3])
    result = solver.step(Frame(T0, Box(400, -1100, 600, -900)))

    assert result.trapped == frozenset({4})


def test_a_node_with_no_exit_at_all_is_trapped_from_the_first_frame() -> None:
    graph = ridge()
    graph.add_node(9, pos=(5000.0, 5000.0))
    solver = EgressSolver(graph, exits=[3])

    result = solver.step(Frame(T0, NoFire()))
    assert 9 in result.trapped


# --------------------------------------------------------------------------
# Direction. This test must never be deleted.
# --------------------------------------------------------------------------


def test_direction_matters_on_one_way_streets() -> None:
    """A BURNED one-way edge must be judged by driving OUT, not by driving in.

    The network is a one-way loop: 1 -> 2 -> 3 -> 1, with 3 the exit. Burning
    the one-way segment 2 -> 3 strands both 1 and 2: from either of them the
    only road toward the exit is gone. But the exit can still *reach* them, by
    driving 3 -> 1 -> 2. So a forward search from the exit reports nobody
    trapped, and is wrong.

    This test therefore proves two things at once:
      * the sweep runs backward from the exits, not forward, and
      * a burned forward edge (2, 3) is removed from the reversed graph in its
        reversed orientation (3, 2) — removing (2, 3) from the reversed graph
        would delete nothing and every node would look safe.
    """
    nodes = {1: (0.0, 1000.0), 2: (1000.0, 1000.0), 3: (500.0, 0.0)}
    loop = build(nodes, [(1, 2), (2, 3), (3, 1)], two_way=False)

    # A box straddling the midpoint of the 2 -> 3 segment only.
    fire = Box(700.0, 450.0, 800.0, 550.0)
    assert fire.crosses([nodes[2], nodes[3]])
    assert not fire.crosses([nodes[1], nodes[2]])
    assert not fire.crosses([nodes[3], nodes[1]])

    result = EgressSolver(loop, exits=[3]).step(Frame(T0, fire))

    assert result.burned_edges == frozenset({(2, 3)})
    assert result.trapped == frozenset({1, 2})

    # The same fire, judged forward from the exit, gets the opposite answer.
    forward = loop.copy()
    forward.remove_edge(2, 3)
    reachable_forward = nx.descendants(forward, 3) | {3}
    assert reachable_forward == {1, 2, 3}, "forward search sees no problem — that is the bug"


def test_one_way_street_burned_on_the_inbound_side_only() -> None:
    """Losing the road INTO town does not trap anyone; losing the road out does."""
    nodes = {1: (0.0, 0.0), 2: (0.0, -1000.0)}
    pair = nx.DiGraph()
    for n, p in nodes.items():
        pair.add_node(n, pos=p)
    # Two separate one-way carriageways between the same places.
    pair.add_edge(1, 2, geometry=((0.0, 0.0), (0.0, -1000.0)))          # outbound
    pair.add_edge(2, 1, geometry=((100.0, 0.0), (100.0, -1000.0)))      # inbound

    inbound_only = Box(50.0, -600.0, 150.0, -400.0)
    result = EgressSolver(pair, exits=[2]).step(Frame(T0, inbound_only))
    assert result.burned_edges == frozenset({(2, 1)})
    assert result.trapped == frozenset()

    outbound_only = Box(-50.0, -600.0, 50.0, -400.0)
    result = EgressSolver(pair, exits=[2]).step(Frame(T0, outbound_only))
    assert result.burned_edges == frozenset({(1, 2)})
    assert result.trapped == frozenset({1})


# --------------------------------------------------------------------------
# Frames are cumulative
# --------------------------------------------------------------------------


def test_roads_never_un_burn() -> None:
    """A later frame reporting no fire cannot reopen a road."""
    solver = EgressSolver(ridge(), exits=[3])
    solver.step(Frame(T0, Box(-500, -1600, 500, -1400)))
    later = solver.step(Frame(T1, NoFire()))

    assert later.trapped == frozenset({1, 2, 4})


def test_trapped_only_grows() -> None:
    """Monotonicity. If the trapped set ever shrinks, that is a bug."""
    solver = EgressSolver(ridge(), exits=[3])
    frames = [
        Frame(T0, NoFire()),
        Frame(T1, Box(400, -1100, 600, -900)),                  # cuts the east spur
        Frame(T2, Boxes.of(Box(-500, -1600, 500, -1400))),      # then the main road
    ]
    result = solver.run(frames)

    sizes = [len(f.trapped) for f in result.frames]
    assert sizes == sorted(sizes), f"trapped set shrank across frames: {sizes}"

    previous: frozenset[int] = frozenset()
    for frame_result in result.frames:
        assert previous <= frame_result.trapped
        previous = frame_result.trapped


def test_a_shrinking_fire_still_cannot_rescue_anyone() -> None:
    """Even a fire polygon that gets smaller leaves the network cut."""
    solver = EgressSolver(ridge(), exits=[3])
    big = Box(-500, -1600, 500, -1400)
    tiny = Box(-1.0, -1000.5, 1.0, -999.5)

    result = solver.run([Frame(T0, big), Frame(T1, tiny)])
    assert result.frames[1].trapped == frozenset({1, 2, 4})


# --------------------------------------------------------------------------
# Cutoff times
# --------------------------------------------------------------------------


def test_cutoff_time_is_the_frame_that_first_cut_the_node() -> None:
    solver = EgressSolver(ridge(), exits=[3])
    result = solver.run(
        [
            Frame(T0, NoFire()),
            Frame(T1, Box(400, -1100, 600, -900)),          # east spur only
            Frame(T2, Box(-500, -1600, 500, -1400)),        # main road out
        ]
    )

    assert result.cutoff_time[4] == T1
    assert result.cutoff_time[1] == T2
    assert result.cutoff_time[2] == T2
    assert 3 not in result.cutoff_time


def test_cutoff_time_is_never_overwritten_by_a_later_frame() -> None:
    solver = EgressSolver(ridge(), exits=[3])
    result = solver.run(
        [
            Frame(T0, Box(400, -1100, 600, -900)),
            Frame(T1, Box(400, -1100, 600, -900)),
            Frame(T2, Box(-500, -1600, 500, -1400)),
        ]
    )
    assert result.cutoff_time[4] == T0


def test_newly_trapped_is_reported_once() -> None:
    solver = EgressSolver(ridge(), exits=[3])
    result = solver.run(
        [
            Frame(T0, Box(400, -1100, 600, -900)),
            Frame(T1, Box(400, -1100, 600, -900)),
        ]
    )
    assert result.frames[0].newly_trapped == frozenset({4})
    assert result.frames[1].newly_trapped == frozenset()


def test_frames_must_arrive_in_time_order() -> None:
    """Out-of-order frames would silently corrupt every cutoff time."""
    solver = EgressSolver(ridge(), exits=[3])
    solver.step(Frame(T1, NoFire()))
    with pytest.raises(ValueError, match="order"):
        solver.step(Frame(T0, NoFire()))


# --------------------------------------------------------------------------
# Exits
# --------------------------------------------------------------------------


def test_an_exit_swallowed_by_the_fire_stops_being_an_exit() -> None:
    """Reaching a burning exit is not escaping."""
    solver = EgressSolver(ridge(), exits=[3])
    result = solver.step(Frame(T0, Box(-200, -2200, 200, -1800)))

    assert 3 in result.closed_exits
    assert result.trapped == frozenset({1, 2, 3, 4})


def test_a_second_exit_keeps_the_town_connected() -> None:
    graph = ridge()
    graph.add_node(5, pos=(2000.0, -1000.0))
    graph.add_edge(4, 5)
    graph.add_edge(5, 4)

    solver = EgressSolver(graph, exits=[3, 5])
    result = solver.step(Frame(T0, Box(-500, -1600, 500, -1400)))

    assert result.trapped == frozenset()


def test_exit_must_exist_in_the_graph() -> None:
    with pytest.raises(ValueError, match="not in the graph"):
        EgressSolver(ridge(), exits=[99])


def test_solver_requires_at_least_one_exit() -> None:
    with pytest.raises(ValueError, match="exit"):
        EgressSolver(ridge(), exits=[])


# --------------------------------------------------------------------------
# Buildings
# --------------------------------------------------------------------------


def test_building_snaps_to_the_nearest_intersection() -> None:
    solver = EgressSolver(ridge(), exits=[3])
    snapped = solver.snap_buildings({"house_a": (10.0, -50.0), "house_b": (990.0, -1010.0)})

    assert snapped["house_a"] == 1
    assert snapped["house_b"] == 4


def test_building_inherits_the_cutoff_of_the_node_it_snaps_to() -> None:
    solver = EgressSolver(ridge(), exits=[3])
    solver.snap_buildings({"east_house": (990.0, -1010.0), "north_house": (10.0, -50.0)})
    result = solver.run(
        [
            Frame(T0, Box(400, -1100, 600, -900)),
            Frame(T1, Box(-500, -1600, 500, -1400)),
        ]
    )

    assert result.building_cutoff_time["east_house"] == T0
    assert result.building_cutoff_time["north_house"] == T1


def test_snapping_far_beyond_the_network_is_refused() -> None:
    """A building 40 km from any road is a data error, not a resident."""
    solver = EgressSolver(ridge(), exits=[3])
    with pytest.raises(ValueError, match="snap"):
        solver.snap_buildings({"nowhere": (400_000.0, 400_000.0)}, max_snap_m=2000.0)


# --------------------------------------------------------------------------
# Purity. This is why the suite runs in milliseconds.
# --------------------------------------------------------------------------


BANNED_IMPORTS = frozenset(
    {
        # geospatial
        "osmnx", "geopandas", "pyproj", "shapely", "rasterio", "fiona",
        "osgeo", "gdal", "cartopy", "geopy",
        # network
        "requests", "httpx", "urllib", "urllib2", "http", "socket", "aiohttp",
        "ftplib", "telnetlib", "smtplib",
    }
)


def imported_modules(path: str) -> set[str]:
    """Top-level module names actually imported by a source file."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_solver_imports_nothing_geospatial_and_nothing_networked() -> None:
    """The pure core is why this suite needs no API key and no data files."""
    offending = imported_modules("src/solver.py") & BANNED_IMPORTS
    assert offending == set(), f"src/solver.py must not import {sorted(offending)}"


def test_the_pure_core_still_imports_when_every_geo_library_is_missing() -> None:
    """The invariant, proven by doing rather than by reading source.

    The AST checks above only see DIRECT imports. A relative import added later
    — `from .fire_data import ...` — would drag shapely in behind them and both
    checks would still pass. So: block the geospatial and network packages
    outright and import the pure core anyway.

    The same subprocess then confirms `src.fire_data` DOES fail under the same
    blocker, which is what proves the blocker is really blocking and this test
    is not vacuous.
    """
    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent(
        """
        import importlib.abc, importlib.machinery, sys

        BANNED = {"shapely", "pyproj", "osmnx", "geopandas", "rasterio",
                  "fiona", "requests", "httpx", "urllib3"}

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in BANNED:
                    raise ImportError("blocked for the purity test: " + fullname)
                return None

        sys.meta_path.insert(0, Blocker())

        import src.solver, src.route_planner          # must succeed

        try:
            import src.fire_data
        except ImportError:
            print("OK")
        else:
            raise SystemExit("blocker did not block: this test proves nothing")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert "OK" in done.stdout


def test_solver_imports_only_networkx_and_the_standard_library() -> None:
    import sys

    third_party = {
        name
        for name in imported_modules("src/solver.py")
        if name not in sys.stdlib_module_names and not name.startswith("_")
    }
    assert third_party <= {"networkx"}, f"unexpected dependency: {sorted(third_party)}"
