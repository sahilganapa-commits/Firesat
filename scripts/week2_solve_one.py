"""One timestamp. Who is cut off, and what is everyone else's plan.

    python -m scripts.week2_solve_one --demo --at 2018-11-08T13:30-08:00

This is the end-to-end slice: FIRMS detections -> cumulative fire polygons
(`src.fire_data`) -> backward reachability (`src.solver`) -> a route and a
deadline per building (`src.route_planner`).

WHAT THE ROAD NETWORK HERE IS, AND IS NOT
-----------------------------------------
The five Paradise arteries come from `src/roads.py`: approximate centrelines,
a handful of waypoints each, roughly +/-100 m. They alone are not a network —
Pentz and Clark do not touch Skyway anywhere in that file, so a graph built
from them is three disconnected corridors and the routing layer has nothing to
route around.

Paradise is in fact joined by east-west cross streets. Rather than enter street
geometry from memory, this module joins the arteries with straight SCHEMATIC
CONNECTORS at three latitude bands. They are marked `schematic=True` in the
graph, rendered as "schematic connector" in any route, and called out in the
printed report. They carry the right connectivity and the wrong geometry.

Pass `--graph network.json` to replace all of it with a real network (Person B's
OSM extract). The solver and the router never see the difference: they take
integer node ids, edge geometry, and exit node ids.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import pathlib
import sys
from collections.abc import Mapping, Sequence

import networkx as nx

from src.fire_data import CLOSE_RADIUS_M, build_frames, to_utm10
from src.firms import PACIFIC_STANDARD, Detection, load_csv_file
from src.limitations import LIMITATIONS_TEXT
from src.params import PARAMS
from src.roads import KNOWN_PLACES, ROADS
from src.route_planner import Route, RoutePlanner, add_straight_line_lengths
from src.solver import Frame

# A building is attached to the nearest intersection. Beyond this it is refused
# rather than snapped to a road it plainly does not sit on. Generous, because
# these arteries carry only a handful of waypoints each.
MAX_SNAP_M = 3_000.0

# Where the schematic cross streets run. Chosen as the latitudes at which all
# three north-south arteries have a waypoint, so a connector joins vertices
# that really are roughly abreast of each other.
CONNECTOR_BANDS: tuple[tuple[str, ...], ...] = (
    ("pentz_rd", "clark_rd", "skyway"),
)
CONNECTOR_LATITUDES: tuple[float, ...] = (39.788, 39.762, 39.748)


@dataclasses.dataclass(frozen=True)
class Network:
    """A road network the solver can consume, plus what humans call its parts."""

    graph: nx.DiGraph
    exits: tuple[int, ...]
    buildings: Mapping[str, tuple[float, float]]
    building_labels: Mapping[str, str]
    schematic: bool


@dataclasses.dataclass(frozen=True)
class BuildingPlan:
    building_id: str
    label: str
    # "open"     — a route to an open exit was found at the last observation
    # "cut_off"  — a pass was observed and no route survived it
    # "unknown"  — nothing has been observed yet; NOT a claim either way
    status: str
    origin_node: int
    exit_node: int | None
    exit_name: str | None
    route_nodes: tuple[int, ...]
    road_names: tuple[str, ...]
    length_km: float | None
    cutoff_at: dt.datetime | None
    uses_schematic_connector: bool


@dataclasses.dataclass(frozen=True)
class Report:
    scenario_time: dt.datetime
    as_of: dt.datetime | None
    observation_age_h: float | None
    is_stale: bool
    n_frames: int
    n_detections: int
    plans: Mapping[str, BuildingPlan]
    cut_off_ids: tuple[str, ...]
    open_exits: tuple[str, ...]
    closed_exits: tuple[str, ...]
    burned_area_km2: float
    area_added_by_closing_km2: float
    close_radius_m: float
    schematic_network: bool


# --------------------------------------------------------------------------
# Building the network
# --------------------------------------------------------------------------


def build_demo_network() -> Network:
    """The five arteries, projected to metres and joined by schematic links."""
    graph = nx.DiGraph()
    node_of: dict[tuple[str, int], int] = {}
    next_id = 0

    for road in ROADS:
        last = len(road.polyline) - 1
        for i, (lat, lon) in enumerate(road.polyline):
            is_exit = i == last
            graph.add_node(
                next_id,
                pos=to_utm10(lat, lon),
                lat=lat,
                lon=lon,
                road_id=road.id,
                road_name=road.name,
                is_exit=is_exit,
                terminus_name=road.terminus_name if is_exit else "",
            )
            node_of[(road.id, i)] = next_id
            next_id += 1

        for i in range(last):
            _add_two_way(
                graph,
                node_of[(road.id, i)],
                node_of[(road.id, i + 1)],
                road_id=road.id,
                road_name=road.name,
                schematic=False,
            )

    _merge_coincident_junctions(graph, tolerance_m=350.0)
    _add_schematic_connectors(graph)
    add_straight_line_lengths(graph)

    exits = tuple(sorted(n for n, d in graph.nodes(data=True) if d["is_exit"]))
    buildings = {p.id: to_utm10(*p.point) for p in KNOWN_PLACES}
    labels = {p.id: p.label for p in KNOWN_PLACES}

    return Network(
        graph=graph,
        exits=exits,
        buildings=buildings,
        building_labels=labels,
        schematic=True,
    )


def _add_two_way(
    graph: nx.DiGraph, u: int, v: int, *, road_id: str, road_name: str, schematic: bool
) -> None:
    """Both directions.

    No one-way restrictions are invented here: `src/roads.py` records none, and
    guessing them would be fabricating the exact data the direction-sensitive
    solver is built to respect. A real OSM network supplies them, and the solver
    handles them — see `test_direction_matters_on_one_way_streets`.
    """
    for a, b in ((u, v), (v, u)):
        graph.add_edge(a, b, road_id=road_id, road_name=road_name, schematic=schematic)


def _merge_coincident_junctions(graph: nx.DiGraph, *, tolerance_m: float) -> None:
    """Fuse waypoints from different arteries that describe the same junction.

    Skyway, Neal Rd and Honey Run Rd meet at the south-west corner of town, but
    each polyline records that junction at its own +/-100 m waypoint. Left
    alone they are three separate nodes and the roads never actually connect.
    """
    nodes = list(graph.nodes(data=True))
    merged: dict[int, int] = {}

    for i, (a, da) in enumerate(nodes):
        if a in merged:
            continue
        for b, db in nodes[i + 1 :]:
            if b in merged or da["road_id"] == db["road_id"]:
                continue
            (ax, ay), (bx, by) = da["pos"], db["pos"]
            if ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 <= tolerance_m:
                merged[b] = a

    for old, new in merged.items():
        for _, target, data in list(graph.out_edges(old, data=True)):
            if target != new:
                graph.add_edge(new, merged.get(target, target), **data)
        for source, _, data in list(graph.in_edges(old, data=True)):
            if source != new:
                graph.add_edge(merged.get(source, source), new, **data)
        # An exit must survive the merge, or the town loses a way out.
        if graph.nodes[old]["is_exit"]:
            graph.nodes[new]["is_exit"] = True
            graph.nodes[new]["terminus_name"] = graph.nodes[old]["terminus_name"]
        graph.remove_node(old)


def _add_schematic_connectors(graph: nx.DiGraph) -> None:
    """Join the north-south arteries with straight east-west links.

    These are stand-ins for Paradise's real cross streets. They are labelled so
    that nothing downstream can mistake them for surveyed geometry.
    """
    for chain in CONNECTOR_BANDS:
        for lat in CONNECTOR_LATITUDES:
            picked = [_nearest_node_at(graph, road_id, lat) for road_id in chain]
            picked = [n for n in picked if n is not None]
            for u, v in zip(picked, picked[1:]):
                if u != v and not graph.has_edge(u, v):
                    _add_two_way(
                        graph,
                        u,
                        v,
                        road_id="schematic_connector",
                        road_name="schematic connector",
                        schematic=True,
                    )


def _nearest_node_at(graph: nx.DiGraph, road_id: str, lat: float) -> int | None:
    candidates = [
        (abs(d["lat"] - lat), n)
        for n, d in graph.nodes(data=True)
        if d["road_id"] == road_id
    ]
    return min(candidates)[1] if candidates else None


def load_network(path: str | pathlib.Path) -> Network:
    """Load a real road network — Person B's OSM extract — from JSON.

    Expected shape (coordinates already in EPSG:32610 metres):

        {"nodes":     [{"id": 0, "x": 618047.8, "y": 4401983.8,
                        "is_exit": false, "name": "..."}],
         "edges":     [{"u": 0, "v": 1, "oneway": false,
                        "length_m": 812.0, "road_name": "Skyway"}],
         "buildings": [{"id": "b1", "x": 618000.0, "y": 4402000.0,
                        "label": "..."}]}

    A `oneway: true` edge is added in the given direction only. That is the
    case the backward sweep exists for.
    """
    raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    graph = nx.DiGraph()

    for node in raw["nodes"]:
        graph.add_node(
            int(node["id"]),
            pos=(float(node["x"]), float(node["y"])),
            lat=node.get("lat"),
            lon=node.get("lon"),
            road_id=node.get("road_id", ""),
            road_name=node.get("name", ""),
            is_exit=bool(node.get("is_exit", False)),
            terminus_name=node.get("terminus_name", node.get("name", "")),
        )

    for edge in raw["edges"]:
        u, v = int(edge["u"]), int(edge["v"])
        attrs = {
            "road_id": edge.get("road_id", ""),
            "road_name": edge.get("road_name", ""),
            "schematic": bool(edge.get("schematic", False)),
        }
        if edge.get("length_m") is not None:
            attrs["length_m"] = float(edge["length_m"])
        if edge.get("geometry"):
            attrs["geometry"] = [(float(x), float(y)) for x, y in edge["geometry"]]
        graph.add_edge(u, v, **attrs)
        if not edge.get("oneway", False):
            graph.add_edge(v, u, **attrs)

    add_straight_line_lengths(graph)

    exits = tuple(sorted(n for n, d in graph.nodes(data=True) if d["is_exit"]))
    if not exits:
        raise ValueError(f"{path}: no node is marked \"is_exit\": true")

    buildings = {
        str(b["id"]): (float(b["x"]), float(b["y"])) for b in raw.get("buildings", [])
    }
    labels = {str(b["id"]): b.get("label", str(b["id"])) for b in raw.get("buildings", [])}

    return Network(
        graph=graph,
        exits=exits,
        buildings=buildings,
        building_labels=labels,
        schematic=False,
    )


# --------------------------------------------------------------------------
# Solving
# --------------------------------------------------------------------------


def solve_at(
    detections: Sequence[Detection],
    when: dt.datetime,
    network: Network,
    *,
    close_radius_m: float = CLOSE_RADIUS_M,
) -> Report:
    """Everything the tool is willing to say at one instant.

    Detections acquired after `when` are discarded before anything is computed.
    The answer is therefore exactly what could have been produced at `when`,
    with no hindsight from a later overpass — the same rule `src/model.py`
    enforces for the road-level model.
    """
    visible = [d for d in detections if d.acq <= when]
    frames = [f for f in build_frames(visible, close_radius_m=close_radius_m) if f.time <= when]

    planner = RoutePlanner(network.graph, network.exits)
    planner.snap_buildings(dict(network.buildings), max_snap_m=MAX_SNAP_M)
    plan = planner.run(Frame(f.time, f) for f in frames)

    live = plan.plan_at(when)
    as_of = live.as_of
    age_h = (when - as_of).total_seconds() / 3600.0 if as_of else None

    # With no observation there is no finding. "Cut off" is a statement about
    # what a satellite saw; if nothing has been seen, the honest answer is
    # UNKNOWN, never the alarming one. This mirrors `src/model.py`.
    observed = live.as_of is not None

    plans: dict[str, BuildingPlan] = {}
    for building_id, node in plan.building_node.items():
        route: Route | None = live.routes.get(building_id)
        if route is not None:
            status = "open"
        elif observed:
            status = "cut_off"
        else:
            status = "unknown"

        plans[building_id] = BuildingPlan(
            building_id=building_id,
            label=network.building_labels.get(building_id, building_id),
            status=status,
            origin_node=node,
            exit_node=route.exit_node if route else None,
            exit_name=_exit_name(network.graph, route.exit_node) if route else None,
            route_nodes=route.nodes if route else (),
            road_names=_road_names(network.graph, route.nodes) if route else (),
            length_km=route.length_m / 1000.0 if route else None,
            cutoff_at=live.closed_at.get(building_id) if not route else None,
            uses_schematic_connector=(
                _uses_schematic(network.graph, route.nodes) if route else False
            ),
        )

    last_frame = frames[-1] if frames else None
    return Report(
        scenario_time=when,
        as_of=as_of,
        observation_age_h=age_h,
        is_stale=age_h is not None and age_h > PARAMS.MAX_PROJECTION_H,
        n_frames=len(frames),
        n_detections=last_frame.n_detections if last_frame else 0,
        plans=plans,
        cut_off_ids=tuple(sorted(b for b, p in plans.items() if p.status == "cut_off")),
        open_exits=tuple(
            sorted(_exit_name(network.graph, n) for n in live.open_exits)
        ),
        closed_exits=tuple(
            sorted(_exit_name(network.graph, n) for n in live.closed_exits)
        ),
        burned_area_km2=last_frame.area_km2 if last_frame else 0.0,
        area_added_by_closing_km2=(
            last_frame.area_added_by_closing_km2 if last_frame else 0.0
        ),
        close_radius_m=close_radius_m,
        schematic_network=network.schematic,
    )


def _road_names(graph: nx.DiGraph, nodes: Sequence[int]) -> tuple[str, ...]:
    """The roads a route uses, in order, without consecutive repeats."""
    names: list[str] = []
    for u, v in zip(nodes, nodes[1:]):
        name = graph[u][v].get("road_name") or "unnamed road"
        if not names or names[-1] != name:
            names.append(name)
    return tuple(names)


def _uses_schematic(graph: nx.DiGraph, nodes: Sequence[int]) -> bool:
    return any(graph[u][v].get("schematic") for u, v in zip(nodes, nodes[1:]))


def _exit_name(graph: nx.DiGraph, node: int) -> str:
    """Name an exit by its artery AND its destination.

    Two of the five arteries end at CA-70 and two at Chico. Naming an exit by
    where it comes out prints the same string twice and tells a reader nothing
    about which road to take.
    """
    data = graph.nodes[node]
    road = data.get("road_name") or ""
    terminus = data.get("terminus_name") or ""
    if road and terminus:
        return f"{road} → {terminus}"
    return road or terminus or f"node {node}"


# --------------------------------------------------------------------------
# Demo scenarios (no API key required)
# --------------------------------------------------------------------------


def severe_demo_detections() -> list[Detection]:
    """SYNTHETIC. A front that actually severs the ridge, so the cut-off path
    can be demonstrated without a FIRMS key.

    NOT the Camp Fire, and not calibrated to it. Two overpasses on the real
    VIIRS SNPP cadence: the first well north-east of town and harmless, the
    second an east-west band across the arteries south of the town centre. Any
    time or place it produces is fabricated.
    """
    utc = dt.timezone.utc
    out: list[Detection] = []

    def add(lat: float, lon: float, acq: dt.datetime) -> None:
        out.append(
            Detection(
                lat=lat, lon=lon, acq=acq, confidence="h",
                bright_ti4=340.0, bright_ti5=300.0, frp=25.0,
                satellite="N", daynight="D", scan=0.4, track=0.4,
            )
        )

    # Pass 1, 01:42 PST — north-east of town, touching nothing.
    first = dt.datetime(2018, 11, 8, 9, 42, tzinfo=utc)
    for i in range(9):
        add(39.840 - 0.004 * (i % 3), -121.480 - 0.004 * (i // 3), first)

    # Pass 2, 13:30 PST — a continuous band across the ridge below the centre.
    second = dt.datetime(2018, 11, 8, 21, 30, tzinfo=utc)
    lon = -121.6650
    while lon <= -121.5650:
        add(39.7530, lon, second)
        lon += 0.003

    return out


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _hhmm(when: dt.datetime | None) -> str:
    """Local PST. FIRMS timestamps are UTC and an operator reads a wall clock."""
    return f"{when.astimezone(PACIFIC_STANDARD):%Y-%m-%d %H:%M} PST" if when else "—"


def format_report(report: Report) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 72)
    add(f"FIREBREAK — egress at {_hhmm(report.scenario_time)}")
    add("=" * 72)

    if report.as_of is None:
        add("")
        add("NO USABLE SATELLITE OBSERVATION AT OR BEFORE THIS TIME.")
        add("Firebreak has nothing to say about this instant, and will not guess.")
        add("")
        add(_footer(report))
        return "\n".join(lines)

    add("")
    add(f"  last satellite pass : {_hhmm(report.as_of)}")
    add(f"  observation is      : {report.observation_age_h:.1f} hours old")
    add(f"  passes used         : {report.n_frames}")
    add(f"  detections          : {report.n_detections}")
    add(f"  burned area         : {report.burned_area_km2:.1f} km²")
    add(
        f"  of which invented by closing : "
        f"{report.area_added_by_closing_km2:.3f} km² "
        f"(radius {report.close_radius_m:.1f} m)"
    )
    if report.is_stale:
        add("")
        add(
            f"  *** STALE. The newest look is {report.observation_age_h:.1f} h old, past the "
            f"{PARAMS.MAX_PROJECTION_H:.1f} h horizon."
        )
        add("      What follows describes the fire AS LAST SEEN, not as it is now.")

    add("")
    # "Not in the fire" is not the same as "reachable" — the roads leading to
    # an untouched exit can still be cut, which is the whole point below.
    add("  exits not themselves in the fire:")
    for name in report.open_exits:
        add(f"      {name}")
    if not report.open_exits:
        add("      NONE")
    if report.closed_exits:
        add("  exits inside the fire:")
        for name in report.closed_exits:
            add(f"      {name}")

    cut_off = [report.plans[b] for b in report.cut_off_ids]
    add("")
    add("-" * 72)
    if cut_off:
        add(f"CUT OFF — {len(cut_off)} location(s) had no route to any open exit")
        add("-" * 72)
        for plan in cut_off:
            add(f"  {plan.label}")
            add(f"      no route out as of {_hhmm(plan.cutoff_at or report.as_of)}")
    else:
        add("CUT OFF — none at this observation")
        add("-" * 72)

    still_open = [p for p in report.plans.values() if p.status == "open"]
    add("")
    add("-" * 72)
    add(f"PLANS — {len(still_open)} location(s) still had a way out")
    add("-" * 72)
    for plan in sorted(still_open, key=lambda p: p.label):
        add(f"  {plan.label}")
        add(f"      out via : {plan.exit_name}  ({plan.length_km:.1f} km)")
        add(f"      route   : {' → '.join(plan.road_names)}")
        if plan.uses_schematic_connector:
            add("      note    : this route uses a SCHEMATIC connector — see the header")

    add("")
    add(_footer(report))
    return "\n".join(lines)


def _footer(report: Report) -> str:
    lines = ["-" * 72]
    if report.schematic_network:
        lines.append(
            "NETWORK: the five arteries are approximate centrelines (±100 m). The\n"
            "cross streets joining them are SCHEMATIC connectors — right\n"
            "connectivity, wrong geometry. Pass --graph to use a real network."
        )
        lines.append("")
    lines.append(
        "This is a PLANNING tool. Every answer above is anchored to a satellite\n"
        "observation that is already hours old. It is NOT navigation. Do not\n"
        "drive by it."
    )
    lines.append("")
    lines.append(LIMITATIONS_TEXT.strip())
    lines.append("-" * 72)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _parse_when(raw: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        # An ambiguous local time is how you accidentally shift a fire by eight
        # hours. Refuse rather than assume.
        raise argparse.ArgumentTypeError(
            f"{raw!r} has no timezone. Use e.g. 2018-11-08T13:30-08:00 (PST) "
            "or 2018-11-08T21:30+00:00 (UTC)."
        )
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="week2_solve_one",
        description="At one timestamp: who is cut off, and what is everyone else's plan.",
    )
    ap.add_argument("--at", required=True, type=_parse_when,
                    help="scenario time, ISO 8601 WITH offset (e.g. 2018-11-08T13:30-08:00)")
    ap.add_argument("--csv", help="FIRMS CSV to read instead of fetching")
    ap.add_argument("--demo", action="store_true",
                    help="synthetic detections, no API key needed")
    ap.add_argument("--demo-severe", action="store_true",
                    help="synthetic detections that DO sever the ridge, so the "
                         "cut-off path can be demonstrated without a key")
    ap.add_argument("--graph", help="JSON road network to use instead of the demo one")
    ap.add_argument("--close-radius-m", type=float, default=CLOSE_RADIUS_M,
                    help=f"morphological close radius in metres (default {CLOSE_RADIUS_M})")
    args = ap.parse_args(argv)

    if args.demo_severe:
        detections = severe_demo_detections()
        banner = "SYNTHETIC DEMO DATA (severe scenario) — not the Camp Fire"
    elif args.demo:
        from src.hindcast import demo_detections

        detections = demo_detections()
        banner = "SYNTHETIC DEMO DATA — not the Camp Fire"
    elif args.csv:
        detections = load_csv_file(args.csv)
        banner = f"FIRMS CSV: {args.csv}"
    else:
        print(
            "Give me data: --demo for synthetic, or --csv <FIRMS export>.\n"
            "A live fetch belongs in the hindcast driver, not here.",
            file=sys.stderr,
        )
        return 2

    network = load_network(args.graph) if args.graph else build_demo_network()

    report = solve_at(detections, args.at, network, close_radius_m=args.close_radius_m)
    print(format_report(report))
    if args.demo or args.demo_severe:
        print(f"\n*** {banner}. These names and times describe a fabricated fire.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
