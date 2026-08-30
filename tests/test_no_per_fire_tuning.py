"""The no-per-fire-tuning claim, enforced mechanically.

Firebreak's credibility rests on the algorithm being fixed. A claim that only
lives in a README is worth nothing, so these tests make it a build failure.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from src import params as params_mod
from src.params import PARAMS, assert_no_per_fire_tuning, params_fingerprint

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

# Files that implement the algorithm. Ground truth and the hindcast scorer are
# allowed to name fires; the model is not.
MODEL_FILES = ("model.py", "params.py", "geo.py", "roads.py")

# Identifiers that would indicate the model branching on which fire it is.
FIRE_NAME_PATTERN = re.compile(
    r"\b(camp_fire|camp\s+fire|paradise|dixie|tubbs|woolsey|caldor|thomas)\b",
    re.IGNORECASE,
)


def _code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Source lines with comments, docstrings and string literals removed.

    Prose may discuss Paradise freely — that is documentation. What must not
    exist is *executable* logic keyed to a fire.
    """
    text = path.read_text(encoding="utf-8")
    # Strip triple-quoted blocks (docstrings).
    text = re.sub(r'"""(?:.|\n)*?"""', '""', text)
    text = re.sub(r"'''(?:.|\n)*?'''", "''", text)

    out: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0]
        # Strip remaining single-line string literals.
        line = re.sub(r'"[^"]*"', '""', line)
        line = re.sub(r"'[^']*'", "''", line)
        if line.strip():
            out.append((lineno, line))
    return out


@pytest.mark.parametrize("filename", MODEL_FILES)
def test_model_code_never_branches_on_a_fire(filename: str) -> None:
    path = SRC / filename
    offenders = [
        (lineno, line.strip())
        for lineno, line in _code_lines(path)
        if FIRE_NAME_PATTERN.search(line)
    ]
    # roads.py legitimately names Paradise geography in identifiers; allow only
    # data definitions there, never conditionals.
    offenders = [
        (n, l)
        for n, l in offenders
        if filename != "roads.py" or re.search(r"\b(if|elif|match|case)\b", l)
    ]
    assert not offenders, (
        f"{filename} contains executable logic keyed to a specific fire:\n"
        + "\n".join(f"  line {n}: {l}" for n, l in offenders)
        + "\n\nThe model must be identical for every fire."
    )


def test_params_are_scalars_only() -> None:
    """A per-fire override would have to be a container. Reject the shape."""
    assert_no_per_fire_tuning(PARAMS)


def test_params_reject_container_values() -> None:
    """The guard actually fires when a per-fire override is introduced."""

    class Sneaky:
        # Mimics `FrozenParams` but with a per-fire dict, the exact shape the
        # guard exists to catch.
        ROAD_BUFFER_M = {"camp_fire": 400.0, "default": 187.5}

    tampered = params_mod.FrozenParams()
    object.__setattr__(tampered, "ROAD_BUFFER_M", {"camp_fire": 400.0})

    with pytest.raises(TypeError, match="per-fire"):
        assert_no_per_fire_tuning(tampered)


def test_params_are_frozen() -> None:
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        PARAMS.ROAD_BUFFER_M = 999.0  # type: ignore[misc]


def test_fingerprint_changes_when_a_parameter_changes() -> None:
    """Tuning a value to improve a score must be visible in every report."""
    baseline = params_fingerprint(PARAMS)
    tuned = params_mod.FrozenParams(ROAD_BUFFER_M=400.0)
    assert params_fingerprint(tuned) != baseline


def test_road_buffer_is_half_a_pixel() -> None:
    """The one threshold in the detector is geometry, not a tuned number."""
    assert PARAMS.ROAD_BUFFER_M == pytest.approx(PARAMS.PIXEL_SIZE_M / 2)
