"""Stdlib fire masks for tests.

The solver and the router accept any object that can answer two questions:
"has this road segment been burned over?" and "is this point inside the fire?".
In production that object wraps a shapely polygon built by `src.fire_data`.
Here it is a plain rectangle, so the pure core can be exercised with no
geospatial dependency and no data files — which is the whole point of keeping
`solver.py` and `route_planner.py` free of geometry libraries.

Coordinates are projected metres (EPSG:32610 in production), so `x` is easting
and `y` is northing.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence

Coord = tuple[float, float]


def _orientation(a: Coord, b: Coord, c: Coord) -> float:
    """Cross product of (b-a) x (c-a). Sign gives the turn direction."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Coord, b: Coord, p: Coord) -> bool:
    """True when collinear point `p` lies within the bounding box of a-b."""
    return (
        min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
    )


def segments_intersect(a: Coord, b: Coord, c: Coord, d: Coord) -> bool:
    """Exact segment/segment intersection test, collinear cases included."""
    d1 = _orientation(c, d, a)
    d2 = _orientation(c, d, b)
    d3 = _orientation(a, b, c)
    d4 = _orientation(a, b, d)

    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    if d1 == 0 and _on_segment(c, d, a):
        return True
    if d2 == 0 and _on_segment(c, d, b):
        return True
    if d3 == 0 and _on_segment(a, b, c):
        return True
    if d4 == 0 and _on_segment(a, b, d):
        return True
    return False


@dataclasses.dataclass(frozen=True)
class Box:
    """An axis-aligned rectangle of burned ground."""

    minx: float
    miny: float
    maxx: float
    maxy: float

    def contains(self, point: Coord) -> bool:
        x, y = point
        return self.minx <= x <= self.maxx and self.miny <= y <= self.maxy

    def crosses(self, geometry: Sequence[Coord]) -> bool:
        points = list(geometry)
        if any(self.contains(p) for p in points):
            return True

        corners = [
            (self.minx, self.miny),
            (self.maxx, self.miny),
            (self.maxx, self.maxy),
            (self.minx, self.maxy),
        ]
        sides = list(zip(corners, corners[1:] + corners[:1]))

        for a, b in zip(points, points[1:]):
            for c, d in sides:
                if segments_intersect(a, b, c, d):
                    return True
        return False


@dataclasses.dataclass(frozen=True)
class Boxes:
    """Several rectangles of burned ground, treated as one fire."""

    boxes: tuple[Box, ...]

    @classmethod
    def of(cls, *boxes: Box) -> "Boxes":
        return cls(tuple(boxes))

    def contains(self, point: Coord) -> bool:
        return any(b.contains(point) for b in self.boxes)

    def crosses(self, geometry: Sequence[Coord]) -> bool:
        return any(b.crosses(geometry) for b in self.boxes)


@dataclasses.dataclass(frozen=True)
class NoFire:
    """Nothing has burned. Used to prove that frames never un-burn a road."""

    def contains(self, point: Coord) -> bool:
        return False

    def crosses(self, geometry: Sequence[Coord]) -> bool:
        return False


@dataclasses.dataclass(frozen=True)
class EdgeSetFire:
    """Burns a named set of edges directly, bypassing geometry.

    Used only where a test is about graph direction rather than about geometry,
    so that the assertion cannot be confused by a coordinate mistake.
    """

    burned: frozenset[tuple[int, int]]
    positions: dict[int, Coord] = dataclasses.field(default_factory=dict)

    @classmethod
    def of(cls, edges: Iterable[tuple[int, int]], positions: dict[int, Coord] | None = None):
        return cls(frozenset(edges), dict(positions or {}))

    def contains(self, point: Coord) -> bool:
        return False

    def crosses(self, geometry: Sequence[Coord]) -> bool:
        points = list(geometry)
        if len(points) < 2:
            return False
        head, tail = points[0], points[-1]
        for (u, v) in self.burned:
            pu, pv = self.positions.get(u), self.positions.get(v)
            if pu is None or pv is None:
                continue
            if (head, tail) == (pu, pv) or (head, tail) == (pv, pu):
                return True
        return False
