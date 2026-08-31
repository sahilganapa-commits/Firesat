"""THE ROUTING LAYER.

The solver answers "trapped: yes or no". That is not yet a plan. This module
turns it into one: for each building, at each frame, the shortest path to the
nearest STILL-OPEN exit over the fire-pruned network, and the frame at which
that path first disappears.

Direction, again
----------------
Routing walks BACKWARD, from the exits, exactly as reachability does. One
multi-source Dijkstra sweep out of the open exits over the reversed graph gives
every node its cheapest drive-OUT cost and the path that achieves it, for all
buildings at once. The path comes back as exit -> ... -> node and is reversed
for presentation, so a route always reads from the front door outward.

Searching forward from an exit would answer "where can a fire engine get to",
and on a one-way street it hands a resident a route they cannot legally drive.
`test_route_direction_matters_on_one_way_streets` pins that down.

Deadlines, and what they honestly mean
--------------------------------------
`minutes_until_close` is the gap between one observation and the observation at
which the route was gone. It is measured across frames, so it is only knowable
once the later frame exists. Over a full historical replay that is a real,
useful retrospective number: it is how much time a household actually had.

It is NOT a forecast, and it must never be shown as one. `plan_at(t)` exists
for that reason: it rebuilds the answer from frames at or before `t` only, so a
live view mechanically cannot borrow a deadline from a satellite pass that has
not happened yet. What it reports instead is the honest thing — the route as of
the last look, and a closure only once a pass has actually observed it.

PURITY
------
networkx and the standard library. No geospatial import, no file, no socket.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable, Mapping, Sequence

import networkx as nx

from .solver import Coord, EgressSolver, Frame, FrameResult, Node

DEFAULT_WEIGHT = "length_m"


def add_straight_line_lengths(
    graph: nx.DiGraph, *, weight: str = DEFAULT_WEIGHT, pos_key: str = "pos"
) -> nx.DiGraph:
    """Fill in a straight-line `length_m` for edges that lack one.

    Existing values win: a caller who knows the true centreline length of a
    segment should not have it overwritten by the distance between its
    endpoints. Coordinates are projected metres, so this is a plain Euclidean
    distance and not a great-circle one.
    """
    for u, v, data in graph.edges(data=True):
        if data.get(weight) is not None:
            continue
        try:
            (x1, y1) = graph.nodes[u][pos_key]
            (x2, y2) = graph.nodes[v][pos_key]
        except KeyError:
            continue
        data[weight] = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return graph


@dataclasses.dataclass(frozen=True)
class Route:
    """One building's way out, as of one observation."""

    building_id: str
    origin_node: Node
    exit_node: Node
    # From the front door outward. nodes[0] is home, nodes[-1] is the exit.
    nodes: tuple[Node, ...]
    length_m: float
    as_of: dt.datetime

    # Both of these are measured ACROSS frames, so both are retrospective.
    # Within a `plan_at` window they are None until a pass has observed the
    # closure — see the module docstring.
    closed_at: dt.datetime | None = None
    minutes_until_close: float | None = None

    @property
    def hops(self) -> int:
        return len(self.nodes) - 1


@dataclasses.dataclass(frozen=True)
class FrameRoutes:
    """Every building's plan at one frame."""

    time: dt.datetime
    routes: Mapping[str, Route | None]
    trapped: frozenset[Node]
    closed_exits: frozenset[Node]
    open_exits: frozenset[Node]

    @property
    def cut_off(self) -> tuple[str, ...]:
        return tuple(sorted(b for b, r in self.routes.items() if r is None))


@dataclasses.dataclass(frozen=True)
class LivePlan:
    """What may be shown for a scenario time, using only observations by then."""

    as_of: dt.datetime | None
    routes: Mapping[str, Route | None]
    closed_at: Mapping[str, dt.datetime | None]
    trapped: frozenset[Node]
    closed_exits: frozenset[Node]
    open_exits: frozenset[Node]

    @property
    def cut_off(self) -> tuple[str, ...]:
        return tuple(sorted(b for b, r in self.routes.items() if r is None))


@dataclasses.dataclass(frozen=True)
class _RawRoute:
    """A route with no timing attached, so a window can date it itself."""

    origin_node: Node
    exit_node: Node
    nodes: tuple[Node, ...]
    length_m: float


@dataclasses.dataclass(frozen=True)
class _RawFrame:
    time: dt.datetime
    raw: Mapping[str, _RawRoute | None]
    trapped: frozenset[Node]
    closed_exits: frozenset[Node]
    open_exits: frozenset[Node]


@dataclasses.dataclass(frozen=True)
class RoutePlan:
    """A whole replay's worth of plans, plus the deadline each building had."""

    frames: tuple[FrameRoutes, ...]
    route_closing_time: Mapping[str, dt.datetime | None]
    building_node: Mapping[str, Node]
    _raw_frames: tuple[_RawFrame, ...] = dataclasses.field(default=(), repr=False)

    def plan_at(self, when: dt.datetime) -> LivePlan:
        """The plan as it could honestly have been given at `when`.

        Frames after `when` are discarded before anything is computed, so a
        deadline observed by a later satellite pass cannot leak backward into
        an answer that predates it.
        """
        visible = tuple(f for f in self._raw_frames if f.time <= when)
        if not visible:
            return LivePlan(
                as_of=None,
                routes={},
                closed_at={},
                trapped=frozenset(),
                closed_exits=frozenset(),
                open_exits=frozenset(),
            )

        closing = _closing_times(visible)
        frames = _materialise(visible, closing)
        last_raw, last = visible[-1], frames[-1]
        return LivePlan(
            as_of=last.time,
            routes=last.routes,
            closed_at={b: closing.get(b) for b in last_raw.raw},
            trapped=last.trapped,
            closed_exits=last.closed_exits,
            open_exits=last.open_exits,
        )

    @property
    def final(self) -> FrameRoutes | None:
        return self.frames[-1] if self.frames else None


def _closing_times(frames: Sequence[_RawFrame]) -> dict[str, dt.datetime]:
    """First frame at which each building's route was gone."""
    closing: dict[str, dt.datetime] = {}
    for frame in frames:
        for building_id, raw in frame.raw.items():
            if raw is None:
                closing.setdefault(building_id, frame.time)
    return closing


def _materialise(
    frames: Sequence[_RawFrame], closing: Mapping[str, dt.datetime]
) -> tuple[FrameRoutes, ...]:
    """Attach timing to raw routes, relative to one window of frames."""
    out: list[FrameRoutes] = []
    for frame in frames:
        routes: dict[str, Route | None] = {}
        for building_id, raw in frame.raw.items():
            if raw is None:
                routes[building_id] = None
                continue
            closed_at = closing.get(building_id)
            minutes = (
                (closed_at - frame.time).total_seconds() / 60.0
                if closed_at is not None
                else None
            )
            routes[building_id] = Route(
                building_id=building_id,
                origin_node=raw.origin_node,
                exit_node=raw.exit_node,
                nodes=raw.nodes,
                length_m=raw.length_m,
                as_of=frame.time,
                closed_at=closed_at,
                minutes_until_close=minutes,
            )
        out.append(
            FrameRoutes(
                time=frame.time,
                routes=routes,
                trapped=frame.trapped,
                closed_exits=frame.closed_exits,
                open_exits=frame.open_exits,
            )
        )
    return tuple(out)


class RoutePlanner:
    """Plans egress routes frame by frame over a fire-pruned road network.

    Wraps an `EgressSolver` rather than reimplementing the burn bookkeeping, so
    "this building has no route" and "this node is trapped" are guaranteed to
    be the same statement.
    """

    def __init__(
        self,
        graph: nx.DiGraph,
        exits: Iterable[Node],
        *,
        weight: str = DEFAULT_WEIGHT,
        geometry_key: str = "geometry",
        pos_key: str = "pos",
    ) -> None:
        self.solver = EgressSolver(
            graph, exits, geometry_key=geometry_key, pos_key=pos_key
        )
        self.weight = weight
        self._raw_frames: list[_RawFrame] = []

        # networkx silently treats a missing weight as 1. On a road network
        # that prices a 30 km highway at one metre and the "shortest" route out
        # becomes nonsense — quietly, and only in the cases that matter.
        unweighted = [
            (u, v) for u, v, data in graph.edges(data=True) if data.get(weight) is None
        ]
        if unweighted:
            shown = ", ".join(f"({u}, {v})" for u, v in unweighted[:5])
            more = f" and {len(unweighted) - 5} more" if len(unweighted) > 5 else ""
            raise ValueError(
                f"{len(unweighted)} edge(s) have no {weight!r}: {shown}{more}. "
                f"Call add_straight_line_lengths(graph) first, or set {weight!r} "
                "on every edge — a missing weight would silently cost 1 metre."
            )

    @property
    def buildings(self) -> Mapping[str, Node]:
        return self.solver.result().building_node

    def snap_buildings(
        self, buildings: Mapping[str, Coord], *, max_snap_m: float | None = None
    ) -> dict[str, Node]:
        return self.solver.snap_buildings(buildings, max_snap_m=max_snap_m)

    def step(self, frame: Frame) -> _RawFrame:
        """Burn one frame, then re-plan every building in a single sweep."""
        state: FrameResult = self.solver.step(frame)
        building_node = self.solver.result().building_node

        paths = self._sweep_out_of(state.open_exits)

        raw: dict[str, _RawRoute | None] = {}
        for building_id, node in building_node.items():
            found = paths.get(node)
            if found is None:
                raw[building_id] = None
                continue
            nodes, length_m = found
            raw[building_id] = _RawRoute(
                origin_node=node,
                exit_node=nodes[-1],
                nodes=nodes,
                length_m=length_m,
            )

        record = _RawFrame(
            time=frame.time,
            raw=raw,
            trapped=state.trapped,
            closed_exits=state.closed_exits,
            open_exits=state.open_exits,
        )
        self._raw_frames.append(record)
        return record

    def _sweep_out_of(
        self, open_exits: frozenset[Node]
    ) -> dict[Node, tuple[tuple[Node, ...], float]]:
        """One multi-source Dijkstra from the open exits over the reversed graph.

        Returns, per node, the drive-out route (home first, exit last) and its
        cost. Because the sweep runs over the reversed graph, the cheapest
        source reached is by construction the nearest still-open exit.
        """
        if not open_exits:
            return {}

        distance, path = nx.multi_source_dijkstra(
            self.solver.graph_rev, set(open_exits), weight=self.weight
        )
        return {
            node: (tuple(reversed(nodes)), float(distance[node]))
            for node, nodes in path.items()
        }

    def run(self, frames: Iterable[Frame]) -> RoutePlan:
        for frame in frames:
            self.step(frame)
        return self.result()

    def result(self) -> RoutePlan:
        raw_frames = tuple(self._raw_frames)
        closing = _closing_times(raw_frames)
        building_node = self.solver.result().building_node
        return RoutePlan(
            frames=_materialise(raw_frames, closing),
            route_closing_time={b: closing.get(b) for b in building_node},
            building_node=building_node,
            _raw_frames=raw_frames,
        )
