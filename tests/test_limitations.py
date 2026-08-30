"""The LIMITATIONS block must be verbatim in the app, the README and the writeup."""

from __future__ import annotations

import pathlib

import pytest

from src.limitations import (
    HTML_TARGET,
    LIMITATIONS_TEXT,
    TARGETS,
    as_html,
    verify,
)


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_limitations_block_is_verbatim(path: pathlib.Path) -> None:
    ok, detail = verify(path)
    assert ok, detail


def test_app_contains_the_limitations_text() -> None:
    """The app renders it as HTML, so compare sentence by sentence."""
    assert HTML_TARGET.exists(), f"{HTML_TARGET} missing"
    page = HTML_TARGET.read_text(encoding="utf-8")
    for paragraph in LIMITATIONS_TEXT.split("\n\n"):
        flowed = " ".join(line.strip() for line in paragraph.splitlines())
        assert flowed in page, f"app is missing limitations paragraph: {flowed[:60]}..."


def test_the_five_required_limitations_are_present() -> None:
    """Each of the five mandated claims, by its load-bearing phrase."""
    required = (
        "planning tool, not navigation",
        "375 m",
        "four football fields",
        "thermally invisible",
        "powered radios, not people",
        "crossed road is treated as impassable",
        "no partial-passability model",
        "extrapolated from observed detections, not simulated",
        "UNKNOWN",
    )
    # The source is hard-wrapped for markdown, so a required phrase may straddle
    # a line break. Match against the reflowed text, which is what a reader sees.
    flowed = " ".join(LIMITATIONS_TEXT.split())
    for phrase in required:
        assert phrase in flowed, f"missing required phrase: {phrase!r}"


def test_as_html_escapes_nothing_unexpected() -> None:
    html = as_html()
    assert html.count("<p>") == LIMITATIONS_TEXT.count("\n\n") + 1
    assert "<script" not in html.lower()
