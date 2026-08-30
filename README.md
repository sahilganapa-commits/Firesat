# Firebreak

**Wildfire egress planning.** Given an address in a fire scenario, Firebreak
shows which ways out were still open, the route, and how long that route had
left — anchored to the satellite observation it actually came from.

It is a planning tool. It is always hours behind the flame front, and it says
so on every answer it gives.

---

## The claim, and how to check it

Firebreak's algorithm is **fixed**. It does not tune itself per fire. That is
the whole basis for taking the hindcast seriously — replaying a frozen model
against a real fire and comparing to documented road failures is a genuine
result; replaying a model whose knobs were turned until Paradise looked good is
not a result at all.

The claim is enforced mechanically, not promised in prose:

| Enforcement | Where |
|---|---|
| Parameters are frozen scalars — a per-fire override can't be expressed | `src/params.py::assert_no_per_fire_tuning` |
| Parameter values are fingerprinted into every report | `src/params.py::params_fingerprint` |
| Model code may not branch on a fire name | `tests/test_no_per_fire_tuning.py` |
| The replay is strictly causal; ground truth enters only at scoring | `src/hindcast.py::score` |
| A warning issued after the last observation aborts the run | `src/hindcast.py::_assert_no_lookahead` |
| Unverifiable ground truth is excluded, which *lowers* recall | `GROUND_TRUTH.md` |

The primary closure detector has **no free parameter at all**. A road is
crossed when a detection centroid falls within half a VIIRS pixel edge
(187.5 m) of its centreline — that is sensor geometry, not a threshold anyone
chose. The only forward-looking number is a closing rate *measured* between two
consecutive overpasses.

---

## Quick start

```bash
python3 -m src.hindcast --demo
```

Runs the whole pipeline on synthetic data with no API key. For real numbers:

```bash
export FIRMS_MAP_KEY=...        # free: https://firms.modaps.eosdis.nasa.gov/api/map_key/
python3 -m src.hindcast --json report.json
```

The planning interface:

```bash
python3 interface/app.py --demo
```

Tests:

```bash
python3 -m pytest -q
```

---

## What's here

```
src/hindcast.py      replay the fixed model over historical FIRMS data and score it
src/model.py         THE FIXED MODEL — detection, measured approach, refusal to guess
src/params.py        frozen parameters, each with provenance
src/plan.py          door-to-safe-zone planning
src/firms.py         NASA FIRMS VIIRS_SNPP_SP client + cache
src/roads.py         the five Paradise egress arteries
GROUND_TRUTH.md      documented Camp Fire road failures, cited, with confidence tiers
interface/           the planning UI (stdlib server, no deps)
hardware/device_counter/   ESP32 coarse presence sensor + calibration
docs/WRITEUP.md      the submission writeup
docs/VIDEO_SCRIPT.md the submission video script
```

---

## Hindcast

`src/hindcast.py` replays the model over Camp Fire (Paradise CA, 8 Nov 2018)
VIIRS_SNPP_SP detections and scores it against `GROUND_TRUTH.md`: **recall,
median lead time, and every miss named** with the observation gap that caused
it.

Ground truth is cited to NIST TN 2252 / NIST ESCAPE and CAL FIRE. Where the
authoritative closure time exists but could not be read at build time, the row
is marked `UNVERIFIED` and **excluded from the metrics** rather than invented —
three of the six rows are currently excluded on that basis. Excluding them can
only lower reported recall.

**Expect a hard result.** VIIRS SNPP passes over Paradise at roughly 01:42 and
13:30 PST. The Camp Fire ran from 06:15 to past 13:00. The first artery closed
at 09:00. There is no satellite observation in that window — the instrument was
not looking. A hindcast that reported high recall on this fire would be
evidence of a bug or of tuning, not of a good model.

That finding is the argument for the tool's framing: Firebreak is for planning
before the day, not warning during it.

---

## Limitations

<!-- BEGIN LIMITATIONS -->
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
guess — unobserved ground is reported as UNKNOWN.
<!-- END LIMITATIONS -->

This block is reproduced verbatim in the app, this README, and the writeup.
`src/limitations.py` is the single source of truth and
`tests/test_limitations.py` fails the build if any copy drifts.

---

## A note on the subject matter

85 people died in the Camp Fire, most of them in or beside their vehicles on
roads that had closed. `GROUND_TRUTH.md` is a record of how people were cut
off, not a benchmark dataset, and it is maintained that way: every scored row
cites a source, and nothing is entered from memory.
