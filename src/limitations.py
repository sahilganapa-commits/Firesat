"""Canonical LIMITATIONS text.

This module is the SINGLE SOURCE OF TRUTH for the limitations block that the
project promises to reproduce verbatim in three places:

  1. the app (interface/static/index.html, injected at render time)
  2. the README (README.md, between the marker comments)
  3. the writeup (docs/WRITEUP.md, between the marker comments)

`tests/test_limitations.py` asserts all three copies are byte-identical to
LIMITATIONS_TEXT. If you edit the text, run `python -m src.limitations --sync`
to rewrite every copy, then re-run the tests.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

BEGIN_MARKER = "<!-- BEGIN LIMITATIONS -->"
END_MARKER = "<!-- END LIMITATIONS -->"

LIMITATIONS_TEXT = """\
Firebreak is a planning tool, not navigation. It tells you what would happen and
when, based on observations that are already hours old. Do not drive by it.

Firebreak cannot see people. A VIIRS pixel is 375 m across — four football
fields — and a person standing beside an 800 °C fire front is thermally
invisible against that background.

The ground sensor detects powered radios, not people. It counts WiFi probe
requests. A phone in a pocket with WiFi off is not counted; a parked car with
four phones counts as up to four; one phone can be counted many times.

A crossed road is treated as impassable. There is no partial-passability model:
Firebreak has no way to know whether one lane, one shoulder, or nothing at all
remains usable after a fire front crosses a road.

Fire spread is extrapolated from observed detections, not simulated. Firebreak
does not model fuel, terrain, or wind. Between satellite passes it does not
guess — unobserved ground is reported as UNKNOWN."""


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# (path, how to embed)
TARGETS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "WRITEUP.md",
    REPO_ROOT / "LIMITATIONS.md",
)

HTML_TARGET = REPO_ROOT / "interface" / "static" / "index.html"


def as_html() -> str:
    """The limitations block as HTML paragraphs, for the app."""
    paragraphs = LIMITATIONS_TEXT.split("\n\n")
    out = []
    for para in paragraphs:
        # Re-flow: the source is hard-wrapped for readability in markdown, but
        # the browser should wrap it itself.
        flowed = " ".join(line.strip() for line in para.splitlines())
        out.append(f"      <p>{flowed}</p>")
    return "\n".join(out)


def _extract(text: str) -> str | None:
    """Pull the text between the markers, or None if markers are absent."""
    pattern = re.compile(
        re.escape(BEGIN_MARKER) + r"\n(.*?)\n" + re.escape(END_MARKER),
        re.DOTALL,
    )
    match = pattern.search(text)
    return match.group(1) if match else None


def verify(path: pathlib.Path) -> tuple[bool, str]:
    """Return (ok, detail) for one target file."""
    if not path.exists():
        return False, f"{path} does not exist"
    body = path.read_text(encoding="utf-8")
    found = _extract(body)
    if found is None:
        return False, f"{path} is missing the LIMITATIONS markers"
    if found != LIMITATIONS_TEXT:
        return False, f"{path} LIMITATIONS block has drifted from src/limitations.py"
    return True, f"{path.name} OK"


def sync(path: pathlib.Path) -> bool:
    """Rewrite the block between markers. Returns True if the file changed."""
    body = path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(BEGIN_MARKER) + r"\n.*?\n" + re.escape(END_MARKER),
        re.DOTALL,
    )
    replacement = f"{BEGIN_MARKER}\n{LIMITATIONS_TEXT}\n{END_MARKER}"
    new_body, count = pattern.subn(lambda _m: replacement, body)
    if count == 0:
        raise SystemExit(f"{path} has no LIMITATIONS markers to sync into")
    if new_body != body:
        path.write_text(new_body, encoding="utf-8")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify or sync the LIMITATIONS block.")
    ap.add_argument(
        "--sync",
        action="store_true",
        help="rewrite every copy from src/limitations.py",
    )
    args = ap.parse_args(argv)

    if args.sync:
        changed = [p.name for p in TARGETS if p.exists() and sync(p)]
        print(f"synced: {', '.join(changed) if changed else 'nothing to do'}")
        return 0

    failures = []
    for path in TARGETS:
        ok, detail = verify(path)
        print(("  ok  " if ok else " FAIL ") + detail)
        if not ok:
            failures.append(detail)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
