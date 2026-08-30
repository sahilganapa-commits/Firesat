"""The Paradise egress network.

Paradise sits on a ridge between two canyons. There are five ways off it. Two
run south to CA-99 (Skyway, Neal Rd), two run south/east to CA-70 (Clark Rd,
Pentz Rd), and one drops west into Butte Creek Canyon (Honey Run Rd).

That 4-southbound + 1-westbound split is what makes the NIST finding
"three of the four southbound routes closed between 11:30 and 13:00"
scoreable — see GROUND_TRUTH.md.

COORDINATE ACCURACY: these centrelines are approximate polylines, a handful of
waypoints per artery, roughly ±100 m. They are not surveyed geometry. That is
adequate for the only question asked of them — did a 375 m detection pixel
overlap this road — where the pixel is nearly four times the positional error.
Anything requiring true lane geometry is out of scope; see LIMITATIONS.
"""

from __future__ import annotations

import dataclasses

from .geo import Point, polyline_length_m

# Ridge-town anchors.
PARADISE_CENTER: Point = (39.7596, -121.6219)
CHICO_CA99: Point = (39.7285, -121.8375)
OROVILLE_CA70: Point = (39.5138, -121.5564)


@dataclasses.dataclass(frozen=True)
class Road:
    id: str
    name: str
    polyline: tuple[Point, ...]
    # Where this artery lets out. Reaching it is what "safe" means here.
    terminus_name: str
    terminus: Point
    # NIST groups the arteries as four southbound + one westbound.
    heading: str  # "south" | "west"

    @property
    def length_m(self) -> float:
        return polyline_length_m(list(self.polyline))

    def segment_points(self) -> list[Point]:
        return list(self.polyline)


ROADS: tuple[Road, ...] = (
    Road(
        id="skyway",
        name="Skyway",
        # Magalia → upper Paradise → town centre → down the ridge → Chico.
        # The main artery; carried the bulk of the evacuation.
        polyline=(
            (39.8120, -121.5780),
            (39.7960, -121.5900),
            (39.7820, -121.6000),
            (39.7700, -121.6130),
            (39.7596, -121.6219),
            (39.7500, -121.6330),
            (39.7450, -121.6400),
            (39.7380, -121.7000),
            (39.7400, -121.7900),
            (39.7285, -121.8375),
        ),
        terminus_name="Chico (CA-99)",
        terminus=CHICO_CA99,
        heading="south",
    ),
    Road(
        id="pentz_rd",
        name="Pentz Road",
        # Easternmost artery — closest to the fire's approach from Pulga.
        # First to close, 09:00.
        polyline=(
            (39.7900, -121.5700),
            (39.7770, -121.5715),
            (39.7640, -121.5730),
            (39.7500, -121.5750),
            (39.7350, -121.5775),
            (39.7200, -121.5810),
            (39.7000, -121.5900),
        ),
        terminus_name="CA-70 south",
        terminus=OROVILLE_CA70,
        heading="south",
    ),
    Road(
        id="clark_rd",
        name="Clark Road (CA-191)",
        polyline=(
            (39.7850, -121.5900),
            (39.7730, -121.5920),
            (39.7600, -121.5945),
            (39.7450, -121.5980),
            (39.7300, -121.6010),
            (39.7100, -121.6070),
            (39.6900, -121.6120),
        ),
        terminus_name="CA-70 south",
        terminus=OROVILLE_CA70,
        heading="south",
    ),
    Road(
        id="neal_rd",
        name="Neal Road",
        polyline=(
            (39.7430, -121.6400),
            (39.7330, -121.6550),
            (39.7200, -121.6700),
            (39.7080, -121.6980),
            (39.6980, -121.7250),
            (39.6900, -121.7500),
        ),
        terminus_name="Chico (CA-99)",
        terminus=CHICO_CA99,
        heading="south",
    ),
    Road(
        id="honey_run_rd",
        name="Honey Run Road",
        # Drops west into Butte Creek Canyon. Narrow, steep, one lane in places.
        polyline=(
            (39.7455, -121.6420),
            (39.7390, -121.6620),
            (39.7320, -121.6810),
            (39.7270, -121.7020),
            (39.7220, -121.7300),
        ),
        terminus_name="Butte Creek Canyon / Chico",
        terminus=CHICO_CA99,
        heading="west",
    ),
)

ROADS_BY_ID: dict[str, Road] = {r.id: r for r in ROADS}

# The set the "three of four southbound" ground-truth event ranges over.
SOUTHBOUND_IDS: tuple[str, ...] = tuple(r.id for r in ROADS if r.heading == "south")


@dataclasses.dataclass(frozen=True)
class KnownPlace:
    """A representative Paradise location, for the planning interface.

    These are approximate neighbourhood centroids, not addresses. The interface
    accepts a free-text address and snaps it to the nearest of these; see
    src/plan.py for why that is honest given what this tool can actually do.
    """

    id: str
    label: str
    point: Point


KNOWN_PLACES: tuple[KnownPlace, ...] = (
    KnownPlace("paradise_downtown", "Downtown Paradise (Skyway & Pearson)", (39.7596, -121.6219)),
    KnownPlace("paradise_east", "East Paradise (near Pentz Rd)", (39.7620, -121.5800)),
    KnownPlace("paradise_north", "North Paradise / Magalia edge", (39.7900, -121.5950)),
    KnownPlace("paradise_south", "South Paradise (near Neal Rd)", (39.7440, -121.6350)),
    KnownPlace("paradise_west", "West Paradise (Honey Run side)", (39.7480, -121.6450)),
    KnownPlace("paradise_central_clark", "Central Paradise (Clark Rd corridor)", (39.7600, -121.5945)),
    KnownPlace("magalia", "Magalia", (39.8120, -121.5780)),
)

KNOWN_PLACES_BY_ID: dict[str, KnownPlace] = {p.id: p for p in KNOWN_PLACES}
