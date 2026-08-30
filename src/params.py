"""FROZEN MODEL PARAMETERS.

The central claim of this project is that the algorithm is fixed and never
tunes itself per fire. That claim is only worth something if it is mechanically
enforceable, so:

  1. Every value below is derived from SENSOR GEOMETRY or PUBLISHED LITERATURE.
     None is fit to an outcome in GROUND_TRUTH.md. Each carries its provenance
     in a comment. "It made Paradise score better" is never a provenance.

  2. There is no per-fire dictionary here, and there cannot be one. `PARAMS` is
     a frozen dataclass with scalar fields; `assert_no_per_fire_tuning()` walks
     it at import time and rejects any dict/list-valued field, which is the
     shape per-fire overrides would have to take.

  3. `params_fingerprint()` hashes the frozen values. Every hindcast report
     embeds the fingerprint. If someone tunes a parameter to improve a score,
     the fingerprint in the report changes and the previous report no longer
     reproduces. The tuning becomes visible in the diff.

The primary closure detector uses only PIXEL_SIZE_M, ROAD_BUFFER_M and
MIN_CONFIDENCE — all three are properties of the instrument and standard FIRMS
practice, not choices tuned against Paradise. The detector has no free
parameter fit to any outcome. Forward projection uses front velocity MEASURED
from this fire's own consecutive passes; the literature cap is a sanity clamp
that only ever slows a projection down.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json


@dataclasses.dataclass(frozen=True)
class FrozenParams:
    """Immutable. Scalars only. See module docstring for why."""

    # --- Sensor geometry (NASA VIIRS 375 m active fire product, VNP14IMG) ---
    # Nominal I-band pixel edge length at nadir. This is an instrument spec.
    # Source: NASA VIIRS 375 m Active Fire product documentation.
    PIXEL_SIZE_M: float = 375.0

    # A detection centroid within half a pixel edge of a road centreline means
    # the pixel footprint overlaps the road. Purely geometric: PIXEL_SIZE_M / 2.
    # NOT a tuned threshold — it is the radius at which overlap becomes certain.
    ROAD_BUFFER_M: float = 187.5

    # --- Standard FIRMS practice ---
    # VIIRS confidence is categorical: 'l' low, 'n' nominal, 'h' high.
    # Dropping 'l' is the conventional filter for analysis use; low-confidence
    # VIIRS detections are dominated by sun glint and small/edge cases.
    # Source: FIRMS FAQ / VIIRS active fire product user guide.
    MIN_CONFIDENCE: str = "n"

    # --- Honesty caps on extrapolation (NOT accuracy tuning) ---
    # How far past the last observation the tool is willing to project at all.
    # Beyond this the answer is UNKNOWN, never a guess. Chosen to be well under
    # the ~12 h single-satellite VIIRS revisit interval so that a projection is
    # always a short step from a real observation, never a substitute for one.
    # Lowering this makes the tool MORE conservative, never more accurate.
    MAX_PROJECTION_H: float = 1.5

    # Upper sanity clamp on measured front velocity, km/h. Wind-driven crown
    # fire rate-of-spread in the published fire-behaviour literature tops out in
    # the low tens of km/h. This clamp only ever REDUCES a projected spread
    # rate; it cannot manufacture a warning. It exists to stop a geolocation
    # error in one pass from producing an absurd velocity.
    # Deliberately NOT derived from Camp Fire's own observed spread rate —
    # that would be fitting a parameter to the test set.
    ROS_SANITY_CAP_KMH: float = 12.0

    # Minimum detections in a pass before a front velocity is estimated at all.
    # Two points define a line through noise; three is the smallest number that
    # can disagree with itself.
    MIN_DETECTIONS_FOR_VELOCITY: int = 3

    # Time gap that separates one satellite overpass from the next. A VIIRS
    # swath crosses a town-scale AOI in seconds; consecutive overpasses are
    # hours apart. Any threshold between minutes and hours partitions the data
    # identically, so this is a data-shaping constant, not a tuned value.
    PASS_GAP_MINUTES: float = 30.0

    # --- Scoring convention (hindcast only; not used by the model) ---
    # A warning issued more than this many hours before actual closure is
    # counted as a hit but reported separately: warning a full day early is not
    # obviously useful. Scoring convention, stated up front, applied uniformly.
    WARN_LEAD_WINDOW_H: float = 6.0


PARAMS = FrozenParams()


def assert_no_per_fire_tuning(params: FrozenParams = PARAMS) -> None:
    """Reject any parameter shaped like a per-fire override.

    A per-fire override has to be a container keyed by fire (or an equivalent
    sequence). Scalars cannot encode "except for Paradise". This is called at
    import time and by the hindcast before it will produce a report.
    """
    for field in dataclasses.fields(params):
        value = getattr(params, field.name)
        if isinstance(value, (dict, list, tuple, set)):
            raise TypeError(
                f"PARAMS.{field.name} is a {type(value).__name__}. Frozen model "
                "parameters must be scalars — a container here is the shape a "
                "per-fire override would take, and per-fire tuning is the one "
                "thing this project claims it does not do."
            )
        if not isinstance(value, (int, float, str, bool)):
            raise TypeError(
                f"PARAMS.{field.name} has non-scalar type {type(value).__name__}."
            )


def as_dict(params: FrozenParams = PARAMS) -> dict[str, object]:
    return {f.name: getattr(params, f.name) for f in dataclasses.fields(params)}


def params_fingerprint(params: FrozenParams = PARAMS) -> str:
    """Stable short hash of the frozen values, embedded in every report."""
    blob = json.dumps(as_dict(params), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


assert_no_per_fire_tuning()
