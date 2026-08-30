"""NASA FIRMS client — VIIRS_SNPP_SP active fire detections.

VIIRS_SNPP_SP is the *standard processing* archive product: quality-controlled,
and the correct source for a 2018 hindcast. (VIIRS_SNPP_NRT is the near-real-
time product and only covers roughly the last two months.)

Requires a FIRMS MAP_KEY for live fetches — free, self-service, from
https://firms.modaps.eosdis.nasa.gov/api/map_key/ — supplied via the
FIRMS_MAP_KEY environment variable. Responses are cached to data/cache/ so a
hindcast re-runs offline and byte-identically once the data has been pulled.

The loader also reads a plain CSV, so an archive download works without ever
touching the API.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import os
import pathlib
import urllib.error
import urllib.request

from .geo import Point

FIRMS_AREA_URL = (
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
    "{map_key}/{source}/{area}/{day_range}/{date}"
)

DEFAULT_SOURCE = "VIIRS_SNPP_SP"
PACIFIC_STANDARD = dt.timezone(dt.timedelta(hours=-8), "PST")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "cache"


@dataclasses.dataclass(frozen=True)
class Detection:
    """One VIIRS active-fire pixel."""

    lat: float
    lon: float
    acq: dt.datetime  # timezone-aware
    confidence: str  # 'l' | 'n' | 'h'
    bright_ti4: float
    bright_ti5: float
    frp: float
    satellite: str
    daynight: str
    scan: float
    track: float

    @property
    def point(self) -> Point:
        return (self.lat, self.lon)


class FirmsError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_CONFIDENCE_ALIASES = {
    "l": "l", "low": "l",
    "n": "n", "nominal": "n",
    "h": "h", "high": "h",
}


def _parse_confidence(raw: str) -> str:
    """VIIRS confidence is categorical. MODIS is numeric 0-100.

    Accept both so an operator who downloads the wrong product still gets a
    sensible error later rather than a silent misparse here.
    """
    raw = (raw or "").strip().lower()
    if raw in _CONFIDENCE_ALIASES:
        return _CONFIDENCE_ALIASES[raw]
    if raw.isdigit():
        pct = int(raw)
        return "l" if pct < 30 else ("n" if pct < 80 else "h")
    return "l"


def _parse_row(row: dict[str, str]) -> Detection | None:
    """Parse one FIRMS CSV row. Returns None for rows we cannot use."""
    try:
        date_s = row["acq_date"].strip()
        time_s = row["acq_time"].strip().zfill(4)
        naive = dt.datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H%M")
        # FIRMS acq_time is UTC.
        acq = naive.replace(tzinfo=dt.timezone.utc)
        return Detection(
            lat=float(row["latitude"]),
            lon=float(row["longitude"]),
            acq=acq,
            confidence=_parse_confidence(row.get("confidence", "")),
            bright_ti4=float(row.get("bright_ti4") or 0.0),
            bright_ti5=float(row.get("bright_ti5") or 0.0),
            frp=float(row.get("frp") or 0.0),
            satellite=(row.get("satellite") or "").strip(),
            daynight=(row.get("daynight") or "").strip(),
            scan=float(row.get("scan") or 0.0),
            track=float(row.get("track") or 0.0),
        )
    except (KeyError, ValueError):
        return None


def parse_csv(text: str) -> list[Detection]:
    """Parse FIRMS CSV text into detections, sorted by acquisition time."""
    stripped = text.lstrip()
    if stripped.lower().startswith(("invalid", "error", "<!doctype", "<html")):
        raise FirmsError(f"FIRMS returned an error page, not CSV: {stripped[:200]!r}")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "latitude" not in reader.fieldnames:
        raise FirmsError(
            f"CSV is missing expected FIRMS columns. Got: {reader.fieldnames!r}"
        )

    detections = [d for d in (_parse_row(r) for r in reader) if d is not None]
    detections.sort(key=lambda d: d.acq)
    return detections


def load_csv_file(path: str | pathlib.Path) -> list[Detection]:
    return parse_csv(pathlib.Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def _cache_path(source: str, area: str, date: str, day_range: int) -> pathlib.Path:
    safe_area = area.replace(",", "_")
    return CACHE_DIR / f"{source}_{safe_area}_{date}_{day_range}d.csv"


def fetch_area(
    *,
    area: tuple[float, float, float, float],
    date: str,
    day_range: int = 1,
    source: str = DEFAULT_SOURCE,
    map_key: str | None = None,
    use_cache: bool = True,
    timeout: float = 60.0,
) -> list[Detection]:
    """Fetch detections for a bbox and date.

    `area` is (west, south, east, north) in degrees. `date` is YYYY-MM-DD.
    """
    area_s = ",".join(f"{v:.4f}" for v in area)
    cache_file = _cache_path(source, area_s, date, day_range)

    if use_cache and cache_file.exists():
        return parse_csv(cache_file.read_text(encoding="utf-8"))

    key = map_key or os.environ.get("FIRMS_MAP_KEY") or os.environ.get("MAP_KEY")
    if not key:
        raise FirmsError(
            "No FIRMS map key. Set FIRMS_MAP_KEY (free, self-service, from\n"
            "  https://firms.modaps.eosdis.nasa.gov/api/map_key/ )\n"
            "or download the archive CSV yourself and pass --csv to the hindcast."
        )

    url = FIRMS_AREA_URL.format(
        map_key=key, source=source, area=area_s, day_range=day_range, date=date
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:  # pragma: no cover - network dependent
        raise FirmsError(f"FIRMS request failed: {exc}") from exc

    detections = parse_csv(body)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(body, encoding="utf-8")
    return detections


# --------------------------------------------------------------------------
# Shaping
# --------------------------------------------------------------------------


def filter_confidence(
    detections: list[Detection], min_confidence: str
) -> list[Detection]:
    """Drop detections below `min_confidence` ('l' < 'n' < 'h')."""
    order = {"l": 0, "n": 1, "h": 2}
    floor = order[min_confidence]
    return [d for d in detections if order[d.confidence] >= floor]


def group_into_passes(
    detections: list[Detection], gap_minutes: float
) -> list[list[Detection]]:
    """Partition time-sorted detections into satellite overpasses.

    A VIIRS swath crosses a town in seconds; overpasses are hours apart. Any
    gap threshold between those two scales produces the same partition.
    """
    if not detections:
        return []
    ordered = sorted(detections, key=lambda d: d.acq)
    gap = dt.timedelta(minutes=gap_minutes)

    passes: list[list[Detection]] = [[ordered[0]]]
    for det in ordered[1:]:
        if det.acq - passes[-1][-1].acq > gap:
            passes.append([det])
        else:
            passes[-1].append(det)
    return passes


def bbox_around(points: list[Point], pad_deg: float = 0.25) -> tuple[float, float, float, float]:
    """(west, south, east, north) bounding box padded around `points`."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (
        min(lons) - pad_deg,
        min(lats) - pad_deg,
        max(lons) + pad_deg,
        max(lats) + pad_deg,
    )
