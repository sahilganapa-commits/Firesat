# Firebreak — writeup

## What it is

Firebreak is a wildfire **egress planning** tool. You give it an address and a
scenario, and it tells you which ways out were still open, the route, and how
long that route had left — always anchored to the satellite observation the
answer came from, and always with that observation's age attached.

It is not navigation. It is hours behind the flame front by construction, and
the design puts that fact in front of the user rather than behind a disclaimer.

## The problem it addresses

Paradise, California sits on a ridge with five ways off it. On 8 November 2018
all five closed. 85 people died, most of them in or beside their vehicles.
NIST's reconstruction records the first artery, Pentz Road, closing at 09:00 —
about an hour after the first evacuation order went out, and that order covered
only the eastern quarter of town.

The question Firebreak tries to answer is the one a household can act on
*before* a fire: given where I live, which exits are fragile, in what order do
they fail, and do I actually have two independent ways out?

## Why the hindcast is the project

Anyone can build a tool that draws a route around a fire polygon. The hard part
is knowing whether it would have been right. So the central deliverable is not
the interface — it is `src/hindcast.py`, which replays the model over
historical FIRMS VIIRS_SNPP_SP detections and scores it against documented road
failures: recall, median lead time, and **every miss named**.

That score only means something if the algorithm is fixed. A model whose
parameters were adjusted until Paradise looked good has not been validated
against Paradise; it has been fitted to it. So the no-tuning property is
enforced mechanically:

- Parameters are frozen scalars. A per-fire override would have to be a
  container, and `assert_no_per_fire_tuning` rejects containers at import.
- The parameter values are hashed into a fingerprint stamped on every report.
  Tune something to improve a score and the fingerprint changes with it.
- A test greps the model source for fire names in executable positions.
- The replay is strictly causal; ground truth is loaded only after it finishes,
  and a warning timestamped after the last observation aborts the run.

The primary detector has **no free parameter at all**. A road is crossed when a
detection centroid lands within half a VIIRS pixel edge — 187.5 m — of its
centreline. That is sensor geometry. Nobody chose it, so nobody can tune it.
The only forward-looking quantity is a closing rate *measured* between two
consecutive overpasses.

## Ground truth, and what we refused to do

`GROUND_TRUTH.md` cites NIST TN 2252, NIST ESCAPE and the CAL FIRE
investigation. Every scored row carries a citation. Three rows —
Clark Rd, Neal Rd, Honey Run Rd — are marked `UNVERIFIED` and **excluded from
the metrics**, because their exact closure times sit inside a 278-page PDF that
could not be read at build time.

Inventing plausible times for those three would have been easy and would have
raised recall. It would also have meant validating the model against fiction,
which is worse than having no validation. Excluding them can only *lower* the
reported number. The uncertainty is taken against ourselves.

## The result, and why it is a hard one

VIIRS SNPP passes over Paradise at roughly **01:42 and 13:30 PST**. The Camp
Fire ignited at 06:15 and Pentz Road closed at 09:00.

There is no satellite observation between those two events. The instrument was
not looking. The hindcast reports the misses with the observation gap that
caused each one — 6.8 h for the 08:30 Skyway burnover, 7.3 h for the 09:00
Pentz closure — and that gap, not the algorithm, is the limiting factor.

This is the finding, and it is not a flattering one. A version of this project
that reported high recall on Camp Fire day-of would be evidence of a bug or of
tuning. What the honest number establishes is where the tool's value actually
is: **planning before the day, not warning during it.** Knowing that Pentz is
your most fragile exit, that three of your four southbound routes can fail
inside a 90-minute window, and that you need a second way out in a different
direction — that is all knowable in advance, and none of it depends on a
satellite being overhead at the right moment.

*Measured recall and median lead time against real FIRMS archive data are
produced by `python3 -m src.hindcast --json report.json` and require a FIRMS
map key. The structural finding above follows from published overpass times and
the documented closure times, independent of that run.*

## The ground sensor

`hardware/device_counter/` is an ESP32 in promiscuous mode counting WiFi probe
requests as a coarse presence signal. The calibration work concluded that the
unique-hash-to-people ratio moves between **1.4× and 10.1×** depending on
conditions the sensor cannot observe, and that at low densities one person can
read as anywhere from zero to six. So it does not report a people count. It
reports change, and `CALIBRATION.md` shows the error bars that justify that
restraint.

MACs are salted-hashed on arrival, the salt rotates every window so windows
cannot be linked, nothing persists, and the firmware has no code path that
could transmit or impersonate a network.

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

## What we would do next

1. **Close the three unverified ground-truth rows** from TN 2252 and re-run.
   Recall over six events is a meaningfully different number than over three.
2. **Add fires with better overpass luck.** Camp Fire is the hardest case and
   the best documented. A fire that ran through a 13:30 pass would separate
   "the model is weak" from "the satellite wasn't looking" — right now those
   two explanations are confounded, and the honest position is that we cannot
   yet distinguish them.
3. **Add GOES.** Geostationary fire detection is 2 km resolution but ~5 minute
   cadence. Coarser in space, vastly better in time — the opposite trade to
   VIIRS, and the obvious fix for the gap that dominates our misses.
