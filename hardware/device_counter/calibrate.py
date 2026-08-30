"""Model the probe-request overcount, and build the error table.

Nothing here is a measurement. This is a Monte-Carlo model of how badly a
unique-MAC count overcounts devices, given documented MAC-randomization
behaviour. It exists so CALIBRATION.md can state expected error with explicit,
inspectable assumptions instead of a hand-waved "it overcounts."

Run:
    python3 hardware/device_counter/calibrate.py
    python3 hardware/device_counter/calibrate.py --markdown

Assumptions (all adjustable; all stated because they drive the answer):

  randomization_rate  Fraction of devices that use a fresh random MAC for each
                      probe burst. iOS 8+ and Android 10+ randomize by default,
                      so this is high on any modern population and is the single
                      biggest driver of overcount.
  bursts_per_window   Probe bursts one device emits per 60 s window. Depends
                      hard on screen state: a screen-on phone scans far more
                      often than one idle in a pocket.
  capture_probability Probability the sensor is on the right channel, in range,
                      and decodes a given burst. Channel hopping across 11
                      channels with a 180 ms dwell means most bursts are missed.
  devices_per_person  Phones, watches, laptops, car head units.
"""

from __future__ import annotations

import argparse
import random
import statistics


def simulate_window(
    n_people: int,
    *,
    devices_per_person: float,
    randomization_rate: float,
    bursts_per_window: float,
    capture_probability: float,
    rng: random.Random,
) -> int:
    """Distinct MAC hashes the sensor would record in one window."""
    n_devices = max(0, int(round(n_people * devices_per_person)))
    distinct = 0

    for _ in range(n_devices):
        bursts = max(0, int(rng.gauss(bursts_per_window, bursts_per_window * 0.4)))
        captured = sum(1 for _ in range(bursts) if rng.random() < capture_probability)
        if captured == 0:
            continue  # device present but never seen — an UNDERcount contribution
        if rng.random() < randomization_rate:
            distinct += captured        # a fresh MAC per captured burst
        else:
            distinct += 1               # stable MAC: one hash however many bursts
    return distinct


def run_density(
    n_people: int,
    *,
    trials: int = 400,
    seed: int = 20181108,
    **kwargs,
) -> dict:
    rng = random.Random(seed + n_people)
    samples = [simulate_window(n_people, rng=rng, **kwargs) for _ in range(trials)]
    samples.sort()

    lo = samples[int(0.05 * len(samples))]
    hi = samples[int(0.95 * len(samples)) - 1]
    med = statistics.median(samples)
    return {
        "people": n_people,
        "median": med,
        "p05": lo,
        "p95": hi,
        "ratio": (med / n_people) if n_people else float("nan"),
    }


DEFAULTS = dict(
    devices_per_person=1.6,
    randomization_rate=0.85,
    bursts_per_window=6.0,
    capture_probability=0.25,
)

DENSITIES = (0, 1, 2, 5, 10, 25, 50, 100, 200)


def build_table(**kwargs) -> list[dict]:
    params = {**DEFAULTS, **kwargs}
    return [run_density(n, **params) for n in DENSITIES]


def render_markdown(rows: list[dict]) -> str:
    out = [
        "| People present | Unique hashes (median) | 90 % range | Implied ratio | Measured (fill in) |",
        "|---:|---:|:---:|---:|:---:|",
    ]
    for r in rows:
        ratio = "—" if r["people"] == 0 else f"{r['ratio']:.1f}×"
        out.append(
            f"| {r['people']} | {r['median']:.0f} | {r['p05']}–{r['p95']} | {ratio} |  |"
        )
    return "\n".join(out)


def render_text(rows: list[dict]) -> str:
    out = [f"{'people':>8} {'median':>8} {'p05':>6} {'p95':>6} {'ratio':>8}"]
    out.append("-" * 40)
    for r in rows:
        ratio = "—" if r["people"] == 0 else f"{r['ratio']:.1f}x"
        out.append(
            f"{r['people']:>8} {r['median']:>8.0f} {r['p05']:>6} {r['p95']:>6} {ratio:>8}"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--randomization-rate", type=float, default=DEFAULTS["randomization_rate"])
    ap.add_argument("--bursts-per-window", type=float, default=DEFAULTS["bursts_per_window"])
    ap.add_argument("--capture-probability", type=float, default=DEFAULTS["capture_probability"])
    ap.add_argument("--devices-per-person", type=float, default=DEFAULTS["devices_per_person"])
    args = ap.parse_args()

    rows = build_table(
        randomization_rate=args.randomization_rate,
        bursts_per_window=args.bursts_per_window,
        capture_probability=args.capture_probability,
        devices_per_person=args.devices_per_person,
    )

    print("MODELLED, NOT MEASURED. Assumptions:")
    print(f"  randomization_rate  = {args.randomization_rate}")
    print(f"  bursts_per_window   = {args.bursts_per_window}")
    print(f"  capture_probability = {args.capture_probability}")
    print(f"  devices_per_person  = {args.devices_per_person}")
    print()
    print(render_markdown(rows) if args.markdown else render_text(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
