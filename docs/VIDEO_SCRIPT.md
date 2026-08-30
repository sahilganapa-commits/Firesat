# Submission video — script and shot list

**Target: 2 min 30 s.** Every accuracy number appears as on-screen text, held
long enough to read (≥ 3 s), not only spoken.

> **Production note:** this is the script and shot list. The recorded video is
> not included — rendering it needs a screen capture of the running interface
> and a voice track. Everything the script asks to be on screen is produced by
> commands in this repo, listed per shot.

---

## Cold open — 0:00–0:18

**Visual.** Black. White text, one line at a time:

```
Paradise, California.  8 November 2018.
Five ways off the ridge.
By 09:00, the first one was gone.
85 people died.
```

**VO.**
> Paradise sits on a ridge. There are five ways off it. On the morning of the
> Camp Fire, all five closed. Most of the people who died were in their cars.

**On screen, lower third, held 4 s:**
`Source: NIST TN 2252 · NIST ESCAPE · CAL FIRE`

---

## What Firebreak is — 0:18–0:40

**Visual.** The interface, address selector on "East Paradise (near Pentz Rd)",
scenario slider at 08:30.

**VO.**
> Firebreak is an egress *planning* tool. Pick an address, pick a moment, and
> it tells you which ways out were open, the route, and how long it had left.

**On screen, large, held for the whole shot:**
`PLANNING — NOT NAVIGATION`

**VO, over the amber staleness banner.**
> And it tells you how old the answer is. Every single time.

**Zoom the staleness banner. Hold 4 s.**

---

## The honesty mechanic — 0:40–1:05

**Visual.** Drag the scenario slider forward. Exit cards flip to amber UNKNOWN.

**VO.**
> When the satellite hasn't looked recently, Firebreak doesn't guess. It says
> UNKNOWN. It will not carry a stale fire front forward and call that a
> forecast.

**On screen:**
`Unobserved ≠ safe.  Unobserved = UNKNOWN.`

**Visual.** Cut to a road card flipping red — CUT.

**VO.**
> And once a road is crossed, it's treated as impassable. We can't tell you
> whether a lane survived. We don't pretend to.

---

## The hindcast — 1:05–1:55  *(the core of the video)*

**Visual.** Terminal. Run and capture:

```bash
python3 -m src.hindcast --json report.json
```

**VO.**
> Here's the part that matters. The algorithm is fixed. It has no parameter
> tuned to any fire. So we can replay it over the real Camp Fire satellite
> record and score it against the closures NIST documented.

**On screen, from the report header, held 5 s:**

```
model parameter fingerprint : <fingerprint>
scored events : 3   (excluded unverified: 3)
RECALL           : <value>
MEDIAN LEAD TIME : <value>
```

**VO.**
> Three of the six documented closures are excluded, because their exact times
> are in a report we couldn't read. We left them out rather than guess. That
> lowers our number. It should.

**Visual.** Scroll to the MISSES block. Hold on it.

**VO.**
> And here's every miss, by name, with the reason.

**On screen, held 6 s — this is the most important frame in the video:**

```
MISS: Pentz Road — actual closure 09:00 PST
      last look before it: 01:42 PST  (7.3 h earlier)
MISS: Skyway — actual closure 08:30 PST
      last look before it: 01:42 PST  (6.8 h earlier)
```

**VO.**
> VIIRS passed over Paradise at 01:42 and again at 13:30. The fire started at
> 06:15. The first road closed at 09:00. There was no satellite overhead in
> that window. The instrument wasn't looking.

**On screen:**
`The gap is the finding.`

**VO.**
> That's not a flattering result and we're not hiding it. If this tool reported
> high recall on Camp Fire day-of, that would be a bug — or tuning. It means
> the value is in planning before the day, not warning during it.

---

## The sensor — 1:55–2:15

**Visual.** ESP32 on a bench, serial monitor scrolling CSV.

**VO.**
> There's a ground sensor too — an ESP32 counting WiFi probe requests. It does
> not count people, and we can show you exactly why.

**On screen, the three calibration ratios side by side, held 5 s:**

```
100 people →  149 unique hashes   (older devices)
100 people →  206 unique hashes   (baseline)
100 people → 1013 unique hashes   (screens on)
ratio swings 1.4× – 10.1×
```

**VO.**
> Same hundred people, three answers, from conditions the sensor can't see. So
> it reports change, not a count. MACs are hashed and thrown away every minute.
> It never transmits.

---

## Close — 2:15–2:30

**Visual.** The limitations panel in the app. Scroll it slowly, readable.

**VO.**
> Firebreak is planning, not navigation. It can't see people. A crossed road is
> just impassable. And between passes, it doesn't guess.

**On screen, final card:**

```
FIREBREAK
Planning, not navigation.
Every limitation, in the app · in the README · in the writeup.
```

---

## Shot checklist

| # | Shot | How to produce |
|---|---|---|
| 1 | Title cards | Static text |
| 2 | Interface, East Paradise @ 08:30 | `python3 interface/app.py --demo` |
| 3 | Staleness banner zoom | Same, crop |
| 4 | Slider → UNKNOWN cards | Same, drag slider |
| 5 | Road card CUT state | Same, slider past a crossing |
| 6 | Hindcast run | `python3 -m src.hindcast --json report.json` |
| 7 | MISSES block | Same output, scrolled |
| 8 | ESP32 serial | Flash `device_counter.ino`, open monitor @ 115200 |
| 9 | Calibration ratios | `python3 hardware/device_counter/calibrate.py --markdown` |
| 10 | Limitations panel | Interface, scrolled to bottom |

## Rules for the edit

- Every number on screen ≥ 3 s. The misses frame ≥ 6 s.
- Never show a route without the staleness banner in the same frame.
- Never use the words "real time", "live", or "navigate".
- Do not show the demo-data run without its synthetic-data warning visible, or
  crop the warning out.
