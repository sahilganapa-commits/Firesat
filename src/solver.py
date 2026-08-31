"""THE EGRESS SOLVER.

Roads are a directed graph: intersections are nodes, segments are edges, a
one-way street is a single-direction edge, and a handful of nodes are marked as
exits — the places where being on the road stops meaning being in the fire.

The question this module answers is not "where is the fire" but "who can no
longer drive out". Those are different questions, and the difference is the
whole point:

    The sweep runs BACKWARD, from the exits, over a reversed graph.

Walking forward from an exit answers "where can a fire engine get to". Walking
backward answers "who can get out". On a one-way street these give opposite
answers, and it is the second one that a resident's life depends on. The whole
graph is reversed once, up front; each frame then deletes the burned edges from
that reversed graph and runs a single multi-source breadth-first sweep out of
the exits. Everything the sweep does not reach is trapped.

Frames are CUMULATIVE. A road that has burned is never restored, so the trapped
set can only ever grow and a node's cutoff time is written exactly once — the
first frame at which it lost every route out. `tests/test_solver.py` asserts
that monotonicity directly; if the trapped set ever shrinks, that is a bug.

PURITY
------
This module imports networkx and the standard library. Nothing else. It has no
geospatial dependency, reads no files and opens no sockets, which is why the
test suite runs in milliseconds with no API key and is never blocked on a
satellite download.

It never sees a fire polygon. It is handed any object that can answer two
questions — `crosses(geometry)` and `contains(point)` — and `src/fire_data.py`
is what wraps a real VIIRS-derived polygon to fit. Tests pass a rectangle.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol, runtime_checkable

import networkx as nx

Coord = tuple[float, float]
Node = int
Edge = tuple[Node, Node]


@runtime_checkable
class FireMask(Protocol):
    """The only thing the solver needs to know about a fire.

    Coordinates are projected metres — EPSG:32610 in production. Deliberately
    tiny: it keeps every geometry library on the far side of this boundary.
    """

    def crosses(self, geometry: Sequence[Coord]) -> bool:
        """True when the fire has burned over this road segment."""

    def contains(self, point: Coord) -> bool:
        """True when this point lies inside the burned area."""


@dataclasses.dataclass(frozen=True)
class Frame:
    """The fire as observed at one instant.

    `time` is the acquisition time of the satellite pass this frame was built
    from, not the time anyone looked at it.
    """

    time: dt.datetime
    fire: FireMask


@dataclasses.dataclass(frozen=True)
class FrameResult:
    """What the network looked like after one frame was applied.

    `trapped`, `burned_edges` and `closed_exits` are CUMULATIVE — everything
    that has happened up to and including this frame. The `newly_` fields are
    just what changed at this frame.
    """

    time: dt.datetime
    burned_edges: frozenset[Edge]
    newly_burned_edges: frozenset[Edge]
    reached: frozenset[Node]
    trapped: frozenset[Node]
    newly_trapped: frozenset[Node]
    closed_exits: frozenset[Node]
    open_exits: frozenset[Node]


@dataclasses.dataclass(frozen=True)
class SolveResult:
    """The whole replay: a cutoff time per intersection, and per building."""

    frames: tuple[FrameResult, ...]
    cutoff_time: Mapping[Node, dt.datetime]
    building_cutoff_time: Mapping[str, dt.datetime]
    building_node: Mapping[str, Node]

    @property
    def final(self) -> FrameResult | None:
        return self.frames[-1] if self.frames else None


class EgressSolver:
    """Replays a fire over a road network and records who lost their way out.

    Construct once per network, then feed frames in time order. State is
    cumulative across `step` calls by design; build a new solver to replay from
    a clean slate.
    """

    def __init__(
        self,
        graph: nx.DiGraph,
        exits: Iterable[Node],
        *,
        geometry_key: str = "geometry",
        pos_key: str = "pos",
    ) -> None:
        exit_nodes = frozenset(exits)
        if not exit_nodes:
            raise ValueError(
                "A road network with no exit has no egress problem to solve — "
                "every node would be trapped from the first frame. Pass at "
                "least one exit node."
            )
        missing = sorted(n for n in exit_nodes if n not in graph)
        if missing:
            raise ValueError(f"exit nodes not in the graph: {missing}")

        self.graph = graph
        self.exits = exit_nodes
        self._pos_key = pos_key

        # Reverse the WHOLE graph once. Every frame reuses this object; it is
        # only ever shrunk, never rebuilt.
        self.graph_rev: nx.DiGraph = graph.reverse(copy=True)

        self._nodes: frozenset[Node] = frozenset(graph.nodes)
        self._positions: dict[Node, Coord] = {
            n: tuple(data[pos_key]) for n, data in graph.nodes(data=True) if pos_key in data
        }
        self._geometry: dict[Edge, tuple[Coord, ...]] = {
            (u, v): self._edge_geometry(u, v, data, geometry_key)
            for u, v, data in graph.edges(data=True)
        }

        self._burned: set[Edge] = set()
        self._closed_exits: set[Node] = set()
        self._trapped: set[Node] = set()
        self._cutoff: dict[Node, dt.datetime] = {}
        self._buildings: dict[str, Node] = {}
        self._last_time: dt.datetime | None = None
        self._results: list[FrameResult] = []

    # -- setup ----------------------------------------------------------

    def _edge_geometry(
        self, u: Node, v: Node, data: Mapping[str, object], geometry_key: str
    ) -> tuple[Coord, ...]:
        """The segment's shape, or the straight line between its endpoints."""
        geometry = data.get(geometry_key)
        if geometry:
            return tuple((float(x), float(y)) for x, y in geometry)  # type: ignore[misc]
        try:
            return (self._positions[u], self._positions[v])
        except KeyError as exc:
            raise ValueError(
                f"edge ({u}, {v}) has no '{geometry_key}' and node {exc.args[0]} "
                f"has no '{self._pos_key}' — the solver cannot tell whether the "
                "fire crossed it"
            ) from None

    def snap_buildings(
        self,
        buildings: Mapping[str, Coord],
        *,
        max_snap_m: float | None = None,
    ) -> dict[str, Node]:
        """Attach each building to its nearest intersection.

        A building's cutoff is the cutoff of the intersection it sits on. That
        is a deliberate coarsening: this tool resolves to the road network, not
        to a driveway, and pretending otherwise would imply a precision the
        underlying 375 m satellite pixel does not have.
        """
        if not self._positions:
            raise ValueError(
                f"nodes carry no '{self._pos_key}' attribute; cannot snap buildings"
            )

        for building_id, point in buildings.items():
            px, py = float(point[0]), float(point[1])
            node, distance_m = min(
                (
                    (n, ((px - x) ** 2 + (py - y) ** 2) ** 0.5)
                    for n, (x, y) in self._positions.items()
                ),
                key=lambda pair: pair[1],
            )
            if max_snap_m is not None and distance_m > max_snap_m:
                raise ValueError(
                    f"building {building_id!r} is {distance_m:,.0f} m from the "
                    f"nearest intersection (limit {max_snap_m:,.0f} m); refusing "
                    "to snap it rather than invent a route for it"
                )
            self._buildings[building_id] = node

        return dict(self._buildings)

    # -- the sweep ------------------------------------------------------

    def step(self, frame: Frame) -> FrameResult:
        """Apply one frame: burn, prune, sweep backward, record cutoffs."""
        if self._last_time is not None and frame.time < self._last_time:
            raise ValueError(
                f"frames must arrive in time order: {frame.time.isoformat()} "
                f"follows {self._last_time.isoformat()}. Out-of-order frames "
                "would corrupt every cutoff time recorded so far."
            )
        self._last_time = frame.time

        # 1. Which edges has the fire crossed that had not burned already?
        newly_burned = {
            edge
            for edge, geometry in self._geometry.items()
            if edge not in self._burned and frame.fire.crosses(geometry)
        }
        self._burned |= newly_burned

        # 2. Delete them from the reversed graph — in their REVERSED
        #    orientation. A burned forward edge (u, v) is the reversed edge
        #    (v, u); removing (u, v) here would delete the wrong carriageway.
        for u, v in newly_burned:
            if self.graph_rev.has_edge(v, u):
                self.graph_rev.remove_edge(v, u)

        # 3. An exit inside the fire is not an exit. Reaching it is not escape.
        for node in self.exits:
            position = self._positions.get(node)
            if position is not None and frame.fire.contains(position):
                self._closed_exits.add(node)
        open_exits = self.exits - self._closed_exits

        # 4. ONE multi-source breadth-first sweep outward from the open exits.
        reached = self._sweep(open_exits)

        # 5. Everything the sweep missed can no longer drive out.
        trapped = self._nodes - reached
        newly_trapped = trapped - self._trapped
        self._trapped = set(trapped)
        for node in newly_trapped:
            self._cutoff.setdefault(node, frame.time)

        result = FrameResult(
            time=frame.time,
            burned_edges=frozenset(self._burned),
            newly_burned_edges=frozenset(newly_burned),
            reached=frozenset(reached),
            trapped=frozenset(trapped),
            newly_trapped=frozenset(newly_trapped),
            closed_exits=frozenset(self._closed_exits),
            open_exits=frozenset(open_exits),
        )
        self._results.append(result)
        return result

    def _sweep(self, sources: Iterable[Node]) -> set[Node]:
        """Multi-source BFS outward from the exits over the reversed graph."""
        reached: set[Node] = set(sources)
        queue = deque(reached)
        adjacency = self.graph_rev.adj
        while queue:
            for neighbour in adjacency[queue.popleft()]:
                if neighbour not in reached:
                    reached.add(neighbour)
                    queue.append(neighbour)
        return reached

    def run(self, frames: Iterable[Frame]) -> SolveResult:
        """Apply every frame in order and return the whole replay."""
        for frame in frames:
            self.step(frame)
        return self.result()

    def result(self) -> SolveResult:
        return SolveResult(
            frames=tuple(self._results),
            cutoff_time=dict(self._cutoff),
            building_cutoff_time={
                building_id: self._cutoff[node]
                for building_id, node in self._buildings.items()
                if node in self._cutoff
            },
            building_node=dict(self._buildings),
        )

    # -- inspection -----------------------------------------------------

    @property
    def trapped(self) -> frozenset[Node]:
        return frozenset(self._trapped)

    @property
    def cutoff_time(self) -> Mapping[Node, dt.datetime]:
        return dict(self._cutoff)

    @property
    def open_exits(self) -> frozenset[Node]:
        return self.exits - self._closed_exits
