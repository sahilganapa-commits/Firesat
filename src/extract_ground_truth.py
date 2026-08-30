"""Propose ground-truth closure rows from a source PDF — for a human to confirm.

Three rows in GROUND_TRUTH.md are marked UNVERIFIED because their closure times
sit inside NIST TN 2252, a 278-page PDF. This tool finds candidate sentences
that mention an egress artery near a clock time and prints them with their page
number so a person can read the context and decide.

It deliberately DOES NOT write to GROUND_TRUTH.md. Ground truth gets a human in
the loop, always. An automated pipeline that scrapes times out of a PDF and
feeds them straight into its own scoring is how you end up validating a model
against a regex bug.

Usage:
    curl -L -o data/NIST.TN.2252.pdf \\
      https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2252.pdf
    python3 -m src.extract_ground_truth data/NIST.TN.2252.pdf

Text extraction backends, tried in order:
    1. pypdf         (pip install pypdf)
    2. pdftotext     (brew install poppler / apt install poppler-utils)
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys

# Artery names as they plausibly appear in the source.
ARTERY_PATTERNS = {
    "skyway": r"\bskyway\b",
    "pentz_rd": r"\bpentz\b",
    "clark_rd": r"\bclark\b",
    "neal_rd": r"\bneal\b",
    "honey_run_rd": r"\bhoney\s*run\b",
    "pearson_rd": r"\bpearson\b",
}

# 24-hour (09:00, 1130) and 12-hour (9:00 a.m.) clock times.
TIME_RE = re.compile(
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:a\.?m\.?|p\.?m\.?)?\b|\b(?:[01]\d|2[0-3])[0-5]\d\s*(?:hrs?|hours)\b",
    re.IGNORECASE,
)

CLOSURE_HINTS = re.compile(
    r"\b(clos(?:ed|ure|ing)|blocked|impassab\w+|burnover|burned over|cut off|"
    r"overtook|overtaken|inaccessible|shut)\b",
    re.IGNORECASE,
)


def extract_pages(pdf_path: pathlib.Path) -> list[str]:
    """Return per-page text. Tries pypdf, then pdftotext."""
    try:
        import pypdf  # type: ignore

        reader = pypdf.PdfReader(str(pdf_path))
        return [(page.extract_text() or "") for page in reader.pages]
    except ImportError:
        pass

    if shutil.which("pdftotext"):
        proc = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            # pdftotext separates pages with a form feed.
            return proc.stdout.split("\f")
        raise SystemExit(f"pdftotext failed: {proc.stderr[:400]}")

    raise SystemExit(
        "No PDF text backend available. Install one:\n"
        "  pip install pypdf\n"
        "or\n"
        "  brew install poppler   (macOS)\n"
        "  apt install poppler-utils   (Debian/Ubuntu)"
    )


def sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", flat) if s.strip()]


def find_candidates(pages: list[str]) -> list[dict]:
    out: list[dict] = []
    for page_no, page_text in enumerate(pages, start=1):
        for sentence in sentences(page_text):
            times = TIME_RE.findall(sentence)
            if not times:
                continue
            for road_id, pattern in ARTERY_PATTERNS.items():
                if not re.search(pattern, sentence, re.IGNORECASE):
                    continue
                out.append(
                    {
                        "road_id": road_id,
                        "page": page_no,
                        "times": times,
                        "has_closure_word": bool(CLOSURE_HINTS.search(sentence)),
                        "sentence": sentence,
                    }
                )
    # Sentences that actually mention closing come first.
    out.sort(key=lambda c: (not c["has_closure_word"], c["road_id"], c["page"]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", type=pathlib.Path)
    ap.add_argument("--road", help="only this road id (e.g. clark_rd)")
    ap.add_argument("--all", action="store_true",
                    help="include sentences with no closure-related word")
    args = ap.parse_args(argv)

    if not args.pdf.exists():
        raise SystemExit(f"{args.pdf} not found. Download it first — see the module docstring.")

    candidates = find_candidates(extract_pages(args.pdf))
    if args.road:
        candidates = [c for c in candidates if c["road_id"] == args.road]
    if not args.all:
        candidates = [c for c in candidates if c["has_closure_word"]]

    if not candidates:
        print("No candidate sentences found. Try --all, or check the extraction backend.")
        return 1

    print(f"{len(candidates)} candidate sentence(s). "
          "READ EACH ONE before entering anything into GROUND_TRUTH.md.\n")
    for c in candidates:
        print(f"--- {c['road_id']}  (p.{c['page']})  times={c['times']}")
        print(f"    {c['sentence'][:400]}")
        print()

    print("=" * 72)
    print("These are CANDIDATES, not ground truth. For each one you accept, edit")
    print("GROUND_TRUTH.md by hand: set the ISO timestamp, set the tier to HIGH,")
    print("and cite the page. Nothing here is written automatically, on purpose.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
