"""Firebreak planning interface — stdlib HTTP server, no dependencies.

    python interface/app.py --demo            # synthetic data, no API key
    python interface/app.py --csv data/camp.csv
    python interface/app.py --port 8080

The server holds one scenario's detections in memory and answers planning
queries against them. Every response carries `as_of` and `observation_age_h`
so the page physically cannot render advice without its staleness.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.firms import PACIFIC_STANDARD, Detection, load_csv_file  # noqa: E402
from src.hindcast import demo_detections  # noqa: E402
from src.plan import build_plan, plan_to_dict  # noqa: E402
from src.roads import KNOWN_PLACES  # noqa: E402

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
SCENARIO_DATE = dt.date(2018, 11, 8)


class Scenario:
    """The detections the server answers against."""

    def __init__(self, detections: list[Detection], provenance: str) -> None:
        self.detections = detections
        self.provenance = provenance


SCENARIO: Scenario | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "Firebreak/1.0"

    # -- helpers ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This tool serves planning output only; no embedding, no framing.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(
            status,
            json.dumps(payload, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            page = (STATIC_DIR / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")

        if route == "/api/places":
            assert SCENARIO is not None
            return self._json(
                {
                    "places": [{"id": p.id, "label": p.label} for p in KNOWN_PLACES],
                    "provenance": SCENARIO.provenance,
                    "scenario_date": SCENARIO_DATE.isoformat(),
                }
            )

        if route == "/api/plan":
            assert SCENARIO is not None
            address = (query.get("address") or [""])[0]
            try:
                minutes = int((query.get("minutes") or ["510"])[0])
            except ValueError:
                return self._json({"error": "minutes must be an integer"}, 400)
            minutes = max(0, min(minutes, 24 * 60 - 1))

            scenario_time = dt.datetime.combine(
                SCENARIO_DATE,
                dt.time(hour=minutes // 60, minute=minutes % 60),
                tzinfo=PACIFIC_STANDARD,
            )
            plan = build_plan(SCENARIO.detections, address, scenario_time)
            payload = plan_to_dict(plan)
            payload["provenance"] = SCENARIO.provenance
            return self._json(payload)

        return self._json({"error": "not found"}, 404)


def load_scenario(args: argparse.Namespace) -> Scenario:
    if args.csv:
        return Scenario(load_csv_file(args.csv), f"local CSV {args.csv}")
    return Scenario(
        demo_detections(),
        "SYNTHETIC DEMO DATA — not the Camp Fire. Pass --csv for real detections.",
    )


def main(argv: list[str] | None = None) -> int:
    global SCENARIO

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", help="FIRMS archive CSV to serve")
    ap.add_argument("--demo", action="store_true", help="synthetic data (default)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    SCENARIO = load_scenario(args)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Firebreak planning interface  →  http://{args.host}:{args.port}")
    print(f"  data: {SCENARIO.provenance}")
    print("  PLANNING TOOL — not navigation. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
