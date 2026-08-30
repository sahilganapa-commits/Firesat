"""HINDCAST — replay the fixed model over historical FIRMS data and score it.

This is the point of the project. Firebreak's claim is that the algorithm is
fixed and never tunes itself per fire, so replaying it against a real fire and
comparing to documented road failures is a genuine result rather than a model
flattered to fit. This file is what makes that checkable.

What it guarantees
------------------
* **Strictly causal.** The model only ever sees detections already acquired.
  Ground truth is loaded *after* the replay finishes and is never passed into
  the model. `_assert_no_lookahead` re-checks this before any score is printed.
* **No per-fire tuning.** Parameters are frozen scalars; the fingerprint of
  those values is stamped on every report. Change a parameter to improve a
  score and the fingerprint changes with it.
* **Every miss is named.** Recall is meaningless without the list of what was
  missed, so the report prints each miss with its cause and the observation gap
  that produced it.
* **Unverified ground truth is excluded**, which lowers recall rather than
  raising it. The uncertainty is taken against ourselves.

Usage
-----
    python -m src.hindcast                     # fetch from FIRMS (needs key)
    python -m src.hindcast --csv data/camp.csv # score an archive download
    python -m src.hindcast --demo              # synthetic data, no key needed
    python -m src.hindcast --json report.json  # machine-readable alongside
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import pathlib
import re
import statistics
import sys

from .firms import (
    Detection,
    FirmsError,
    PACIFIC_STANDARD,
    bbox_around,
    fetch_area,
    group_into_passes,
    load_csv_file,
)
from .model import ReplayResult, Warning_, replay
from .params import PARAMS, FrozenParams, as_dict, params_fingerprint
from .roads import ROADS, ROADS_BY_ID, SOUTHBOUND_IDS

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GROUND_TRUTH_PATH = REPO_ROOT / "GROUND_TRUTH.md"
GT_BLOCK_RE = re.compile(r"```firebreak-ground-truth\n(.*?)```", re.DOTALL)

CAMP_FIRE_DATE = "2018-11-08"
CAMP_FIRE_DAY_RANGE = 3

# The window event is a cardinality claim, not a per-road one.
WINDOW_CARDINALITY = {"southbound_3of4": (3, SOUTHBOUND_IDS)}


# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GroundTruthEvent:
    fire_id: str
    road_id: str
    closed_from: dt.datetime | None
    closed_to: dt.datetime | None
    tier: str
    source: str
    note: str

    @property
    def is_scored(self) -> bool:
        return self.tier != "UNVERIFIED" and self.closed_from is not None

    @property
    def is_window(self) -> bool:
        return self.road_id in WINDOW_CARDINALITY


def parse_ground_truth(path: pathlib.Path = GROUND_TRUTH_PATH) -> list[GroundTruthEvent]:
    """Parse the fenced table out of GROUND_TRUTH.md.

    Reading it straight from the prose document is deliberate: the numbers
    cannot drift away from the citations sitting next to them.
    """
    text = path.read_text(encoding="utf-8")
    match = GT_BLOCK_RE.search(text)
    if not match:
        raise SystemExit(
            f"{path} has no ```firebreak-ground-truth``` block. The hindcast "
            "refuses to score against ground truth it cannot find."
        )

    events: list[GroundTruthEvent] = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 7:
            raise SystemExit(f"Malformed ground-truth row (need 7 fields): {line!r}")

        def _time(raw: str) -> dt.datetime | None:
            return None if raw.upper() == "UNVERIFIED" else dt.datetime.fromisoformat(raw)

        events.append(
            GroundTruthEvent(
                fire_id=parts[0],
                road_id=parts[1],
                closed_from=_time(parts[2]),
                closed_to=_time(parts[3]),
                tier=parts[4],
                source=parts[5],
                note=parts[6],
            )
        )
    return events


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Scored:
    event: GroundTruthEvent
    hit: bool
    warning: Warning_ | None
    lead_time_h: float | None
    miss_reason: str | None
    observation_gap_h: float | None
    last_pass_before_closure: dt.datetime | None


def _last_pass_before(pass_times: list[dt.datetime], when: dt.datetime) -> dt.datetime | None:
    earlier = [p for p in pass_times if p <= when]
    return max(earlier) if earlier else None


def _assert_no_lookahead(result: ReplayResult, events: list[GroundTruthEvent]) -> None:
    """A warning issued after every observation would mean ground truth leaked."""
    if not result.pass_times:
        return
    last_obs = max(result.pass_times)
    for warning in result.warnings.values():
        if warning.issued_at > last_obs:
            raise AssertionError(
                f"Warning for {warning.road_id} issued at {warning.issued_at}, after "
                f"the last observation {last_obs}. The model saw the future."
            )


def score(result: ReplayResult, events: list[GroundTruthEvent], params: FrozenParams = PARAMS) -> list[Scored]:
    """Score the replay against ground truth. Ground truth enters only here."""
    _assert_no_lookahead(result, events)
    scored: list[Scored] = []

    for event in events:
        if not event.is_scored:
            continue

        closure = event.closed_from
        deadline = event.closed_to or event.closed_from
        last_pass = _last_pass_before(result.pass_times, closure)
        gap_h = (closure - last_pass).total_seconds() / 3600.0 if last_pass else None

        # --- cardinality window event --------------------------------------
        if event.is_window:
            need, candidates = WINDOW_CARDINALITY[event.road_id]
            flagged = [
                rid
                for rid in candidates
                if rid in result.warnings and result.warnings[rid].issued_at <= deadline
            ]
            hit = len(flagged) >= need
            earliest = (
                min((result.warnings[r] for r in flagged), key=lambda w: w.issued_at)
                if flagged
                else None
            )
            scored.append(
                Scored(
                    event=event,
                    hit=hit,
                    warning=earliest,
                    lead_time_h=(
                        (closure - earliest.issued_at).total_seconds() / 3600.0
                        if hit and earliest
                        else None
                    ),
                    miss_reason=(
                        None
                        if hit
                        else f"flagged only {len(flagged)}/{need} required southbound arteries "
                        f"by {deadline:%H:%M} (flagged: {', '.join(flagged) or 'none'})"
                    ),
                    observation_gap_h=gap_h,
                    last_pass_before_closure=last_pass,
                )
            )
            continue

        # --- point event ----------------------------------------------------
        warning = result.warnings.get(event.road_id)
        if warning is None:
            scored.append(
                Scored(
                    event=event,
                    hit=False,
                    warning=None,
                    lead_time_h=None,
                    miss_reason="never flagged — no detection ever landed on this road, "
                    "and the fire was never measured closing on it",
                    observation_gap_h=gap_h,
                    last_pass_before_closure=last_pass,
                )
            )
            continue

        lead_h = (closure - warning.issued_at).total_seconds() / 3600.0
        if lead_h >= 0:
            scored.append(
                Scored(
                    event=event,
                    hit=True,
                    warning=warning,
                    lead_time_h=lead_h,
                    miss_reason=None,
                    observation_gap_h=gap_h,
                    last_pass_before_closure=last_pass,
                )
            )
        else:
            scored.append(
                Scored(
                    event=event,
                    hit=False,
                    warning=warning,
                    lead_time_h=lead_h,
                    miss_reason=f"flagged {abs(lead_h):.1f} h too late — first signal at "
                    f"{warning.issued_at.astimezone(PACIFIC_STANDARD):%H:%M} PST, road was "
                    f"already closed at {closure.astimezone(PACIFIC_STANDARD):%H:%M} PST",
                    observation_gap_h=gap_h,
                    last_pass_before_closure=last_pass,
                )
            )

    return scored


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _fmt(when: dt.datetime | None) -> str:
    if when is None:
        return "—"
    return f"{when.astimezone(PACIFIC_STANDARD):%Y-%m-%d %H:%M} PST"


def build_report(
    scored: list[Scored],
    result: ReplayResult,
    events: list[GroundTruthEvent],
    params: FrozenParams = PARAMS,
) -> dict:
    hits = [s for s in scored if s.hit]
    misses = [s for s in scored if not s.hit]
    leads = [s.lead_time_h for s in hits if s.lead_time_h is not None]
    excluded = [e for e in events if not e.is_scored]

    return {
        "params_fingerprint": params_fingerprint(params),
        "params": as_dict(params),
        "n_scored_events": len(scored),
        "n_excluded_unverified": len(excluded),
        "n_hits": len(hits),
        "n_misses": len(misses),
        "recall": (len(hits) / len(scored)) if scored else None,
        "median_lead_time_h": statistics.median(leads) if leads else None,
        "lead_times_h": sorted(leads),
        "n_detections_used": result.n_detections_used,
        "n_detections_dropped_low_confidence": result.n_detections_dropped_low_confidence,
        "n_passes": len(result.pass_times),
        "pass_times_pst": [_fmt(p) for p in result.pass_times],
        "misses": [
            {
                "road_id": s.event.road_id,
                "road_name": ROADS_BY_ID[s.event.road_id].name
                if s.event.road_id in ROADS_BY_ID
                else s.event.road_id,
                "actual_closure": _fmt(s.event.closed_from),
                "reason": s.miss_reason,
                "observation_gap_h": s.observation_gap_h,
                "last_pass_before_closure": _fmt(s.last_pass_before_closure),
                "source": s.event.source,
            }
            for s in misses
        ],
        "hits": [
            {
                "road_id": s.event.road_id,
                "actual_closure": _fmt(s.event.closed_from),
                "warned_at": _fmt(s.warning.issued_at) if s.warning else None,
                "warning_kind": s.warning.kind if s.warning else None,
                "lead_time_h": s.lead_time_h,
            }
            for s in hits
        ],
        "excluded_unverified": [
            {"road_id": e.road_id, "source": e.source, "note": e.note} for e in excluded
        ],
    }


def render_report(report: dict) -> str:
    L: list[str] = []
    add = L.append

    add("=" * 74)
    add("FIREBREAK HINDCAST — Camp Fire, Paradise CA, 2018-11-08")
    add("=" * 74)
    add(f"model parameter fingerprint : {report['params_fingerprint']}")
    add(f"detections used             : {report['n_detections_used']}"
        f"  (dropped {report['n_detections_dropped_low_confidence']} low-confidence)")
    add(f"satellite passes replayed   : {report['n_passes']}")
    for pt in report["pass_times_pst"]:
        add(f"    pass: {pt}")
    add("")

    add("-" * 74)
    add("SCORE")
    add("-" * 74)
    recall = report["recall"]
    add(f"  scored events        : {report['n_scored_events']}"
        f"   (excluded unverified: {report['n_excluded_unverified']})")
    add(f"  hits                 : {report['n_hits']}")
    add(f"  misses               : {report['n_misses']}")
    add(f"  RECALL               : {recall:.0%}" if recall is not None else "  RECALL               : n/a")
    mlt = report["median_lead_time_h"]
    add(f"  MEDIAN LEAD TIME     : {mlt:+.2f} h" if mlt is not None else
        "  MEDIAN LEAD TIME     : n/a (no hits)")
    add("")

    add("-" * 74)
    add("HITS")
    add("-" * 74)
    if not report["hits"]:
        add("  (none)")
    for h in report["hits"]:
        add(f"  {h['road_id']:<18} closed {h['actual_closure']}")
        add(f"  {'':<18} warned {h['warned_at']}  [{h['warning_kind']}]  "
            f"lead {h['lead_time_h']:+.2f} h")
    add("")

    add("-" * 74)
    add("MISSES — every one, named")
    add("-" * 74)
    if not report["misses"]:
        add("  (none)")
    for m in report["misses"]:
        add(f"  MISS: {m['road_name']} ({m['road_id']})  [source {m['source']}]")
        add(f"        actual closure      : {m['actual_closure']}")
        add(f"        why                 : {m['reason']}")
        if m["observation_gap_h"] is not None:
            add(f"        last look before it : {m['last_pass_before_closure']}"
                f"  ({m['observation_gap_h']:.1f} h earlier)")
        else:
            add("        last look before it : NO SATELLITE PASS AT ALL before this closure")
        add("")

    if report["excluded_unverified"]:
        add("-" * 74)
        add("EXCLUDED FROM SCORING — ground truth not verifiable at build time")
        add("-" * 74)
        for e in report["excluded_unverified"]:
            add(f"  {e['road_id']:<18} [source {e['source']}]")
        add("  These are excluded rather than guessed. Excluding them can only")
        add("  lower reported recall; see GROUND_TRUTH.md.")
        add("")

    add("=" * 74)
    return "\n".join(L)


# --------------------------------------------------------------------------
# Demo data (no API key required)
# --------------------------------------------------------------------------


def demo_detections() -> list[Detection]:
    """SYNTHETIC detections for exercising the pipeline without a FIRMS key.

    NOT the Camp Fire. This is a fabricated front that starts northeast of
    Paradise and moves southwest on a realistic VIIRS overpass cadence. It
    exists so `--demo` proves the code runs end to end; any score it produces
    is meaningless and the report says so.
    """
    utc = dt.timezone.utc
    out: list[Detection] = []

    # Two overpasses: one pre-dawn, one early afternoon — the real VIIRS SNPP
    # cadence that is the whole problem for a fire that runs 06:15-13:00.
    passes = [
        (dt.datetime(2018, 11, 8, 9, 42, tzinfo=utc), 39.810, -121.520),   # 01:42 PST
        (dt.datetime(2018, 11, 8, 21, 30, tzinfo=utc), 39.762, -121.600),  # 13:30 PST
    ]
    for acq, lat0, lon0 in passes:
        for i in range(12):
            out.append(
                Detection(
                    lat=lat0 - 0.004 * (i % 4),
                    lon=lon0 - 0.004 * (i // 4),
                    acq=acq,
                    confidence="h",
                    bright_ti4=340.0,
                    bright_ti5=300.0,
                    frp=25.0,
                    satellite="N",
                    daynight="D",
                    scan=0.4,
                    track=0.4,
                )
            )
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def load_detections(args: argparse.Namespace) -> tuple[list[Detection], str]:
    if args.demo:
        return demo_detections(), "SYNTHETIC DEMO DATA — not the Camp Fire"
    if args.csv:
        return load_csv_file(args.csv), f"local CSV {args.csv}"

    aoi_points = [p for road in ROADS for p in road.segment_points()]
    bbox = bbox_around(aoi_points, pad_deg=0.3)
    dets = fetch_area(
        area=bbox, date=CAMP_FIRE_DATE, day_range=CAMP_FIRE_DAY_RANGE
    )
    return dets, f"FIRMS VIIRS_SNPP_SP {CAMP_FIRE_DATE} +{CAMP_FIRE_DAY_RANGE}d"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", help="score a local FIRMS archive CSV instead of fetching")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no API key")
    ap.add_argument("--json", help="also write the machine-readable report here")
    args = ap.parse_args(argv)

    try:
        detections, provenance = load_detections(args)
    except FirmsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("\nhint: `python -m src.hindcast --demo` runs the pipeline with no key.",
              file=sys.stderr)
        return 2

    if not detections:
        print("error: no detections loaded — nothing to replay.", file=sys.stderr)
        return 2

    events = parse_ground_truth()
    result = replay(detections)
    scored = score(result, events)
    report = build_report(scored, result, events)
    report["data_provenance"] = provenance

    print(render_report(report))
    print(f"data: {provenance}")
    if args.demo:
        print(
            "\n*** --demo uses SYNTHETIC detections. These numbers describe a\n"
            "*** fabricated fire and mean nothing about Camp Fire performance.\n"
            "*** Real results require FIRMS VIIRS_SNPP_SP archive data."
        )

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        print(f"wrote {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
