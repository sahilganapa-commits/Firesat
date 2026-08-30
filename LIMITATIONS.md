# LIMITATIONS

This block is reproduced **verbatim** in the app, the README, and the writeup.
`src/limitations.py` is the single source of truth; `tests/test_limitations.py`
fails the build if any copy drifts.

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
