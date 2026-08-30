"""Geometry helpers.

Everything here works in metres on a local equirectangular approximation. At
the scale Firebreak operates (a town, tens of km) the error against a proper
geodesic is well under a metre — far below the 375 m pixel that dominates every
distance in this system.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8

Point = tuple[float, float]  # (lat, lon) in degrees


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def _local_xy(p: Point, origin: Point) -> tuple[float, float]:
    """Project to local metres east/north of `origin`."""
    lat_rad = math.radians(origin[0])
    x = math.radians(p[1] - origin[1]) * EARTH_RADIUS_M * math.cos(lat_rad)
    y = math.radians(p[0] - origin[0]) * EARTH_RADIUS_M
    return x, y


def point_to_segment_m(p: Point, a: Point, b: Point) -> float:
    """Shortest distance in metres from point `p` to segment `a`–`b`."""
    px, py = _local_xy(p, a)
    ax, ay = 0.0, 0.0
    bx, by = _local_xy(b, a)

    vx, vy = bx - ax, by - ay
    seg_len_sq = vx * vx + vy * vy
    if seg_len_sq == 0.0:
        return math.hypot(px - ax, py - ay)

    t = ((px - ax) * vx + (py - ay) * vy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    projx, projy = ax + t * vx, ay + t * vy
    return math.hypot(px - projx, py - projy)


def point_to_polyline_m(p: Point, polyline: list[Point]) -> float:
    """Shortest distance in metres from `p` to a polyline."""
    if not polyline:
        return float("inf")
    if len(polyline) == 1:
        return haversine_m(p, polyline[0])
    return min(
        point_to_segment_m(p, polyline[i], polyline[i + 1])
        for i in range(len(polyline) - 1)
    )


def nearest_vertex_index(p: Point, polyline: list[Point]) -> int:
    """Index of the polyline vertex closest to `p`."""
    return min(range(len(polyline)), key=lambda i: haversine_m(p, polyline[i]))


def polyline_length_m(polyline: list[Point]) -> float:
    return sum(
        haversine_m(polyline[i], polyline[i + 1]) for i in range(len(polyline) - 1)
    )


def centroid(points: list[Point]) -> Point:
    """Arithmetic mean position. Adequate at town scale."""
    if not points:
        raise ValueError("centroid of empty point set")
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


def bearing_deg(a: Point, b: Point) -> float:
    """Initial bearing from `a` to `b`, degrees clockwise from north."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
