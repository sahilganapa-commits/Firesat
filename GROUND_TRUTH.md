# GROUND TRUTH — Camp Fire, Paradise CA, 8 November 2018

This file is the scoring target for `src/hindcast.py`. It is the reason the
project is credible or isn't.

**Rule of this file:** every scored row cites a source. Nothing is entered from
memory. Where the authoritative source exists but I could not read it, the row
is recorded as `UNVERIFIED` and **excluded from the metrics** rather than
guessed. A fabricated closure time would make the entire hindcast fraudulent —
it would be a model validated against fiction. An honest gap is recoverable; an
invented number is not.

85 people died in this fire, most of them in or beside their vehicles. The
timeline below is not a benchmark dataset. It is a record of how people were
cut off.

---

## Confidence tiers

| Tier | Meaning | Scored? |
|---|---|---|
| `HIGH` | Stated explicitly, with a clock time, in a federal technical report (NIST) or the CAL FIRE investigation. | Yes |
| `MEDIUM` | Stated with a clock time in contemporaneous mainstream reporting, consistent with the HIGH-tier record. | Yes, flagged |
| `WINDOW` | Authoritative source gives a time *range* or an unnamed subset. Scored against the range, not a point. | Yes, as range |
| `UNVERIFIED` | Known to exist in NIST TN 2252 but not readable from open sources at build time. **Excluded from all metrics.** | **No** |

Excluding `UNVERIFIED` rows *lowers* reported recall (the model gets no credit
for anything it might have caught there). That direction is deliberate — the
error is taken against ourselves.

---

## Sources

- **[S1]** NIST TN 2252, *A Case Study of the Camp Fire — Notification,
  Evacuation, Traffic, and Temporary Refuge Areas (NETTRA)*, 278 pp.
  <https://doi.org/10.6028/NIST.TN.2252> ·
  <https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2252.pdf>
- **[S2]** NIST **ESCAPE**, *Camp Fire Egress Artery Closure with Time*.
  <https://escape.nist.gov/evacuation3AddressingFailuresEgresslearnmore1>
- **[S3]** NIST, *Life Safety During the Camp Fire* (project page).
  <https://www.nist.gov/el/fire-research-division-73300/wildland-urban-interface-fire-73305/nist-investigation-california-5>
- **[S4]** NIST TN 2135 — burnover / entrapment catalogue (23 burnovers total
  across TN 2135 + TN 2252). Referenced via [S1].
- **[S5]** ABC7 News, *Camp Fire Timeline of Terror: the evacuation of Paradise
  from beginning to end*. <https://abc7news.com/post/timeline-of-terror-the-evacuation-of-paradise-from-beginning-to-end/4850913/>
- **[S6]** CAL FIRE investigation summary / Wikipedia consolidation of the
  Caribou–Palermo ignition finding. <https://en.wikipedia.org/wiki/Camp_Fire_(2018)>
- **[S7]** NIST, *Paradise urban reconstruction map* (road network reference).
  <https://www.nist.gov/system/files/documents/2020/11/16/Paradise-UR_map.pdf>

---

## Narrative timeline (context, not all scored)

| Time (PST) | Event | Tier | Source |
|---|---|---|---|
| 06:15 | Ignition. PG&E Caribou–Palermo 115 kV line, C-hook failure, near Pulga — ~7 mi NE of Paradise. | HIGH | S6 |
| 07:46 | CAL FIRE IC declares a major incident. | HIGH | S6 |
| ~08:00 | First Paradise evacuation order. Covers only the **eastern quarter** of town. Sources differ on the exact minute (07:46 / 08:01 / 08:03 sheriff tweet); the substantive fact — a zone-limited first order around 08:00 — is consistent across all of them. | HIGH | S5, S6 |
| 08:00 | Traffic still flowing normally on all arteries. | HIGH | S2 |
| 08:30 | Fire overtakes traffic on **Pentz Road** *and* **upper Skyway**. Two burnovers. Evacuation and emergency access both restricted. | HIGH | S2 |
| **09:00** | **Pentz Road closes — the first egress artery to fail.** | HIGH | S2 |
| 09:33 | Evacuation order issued for Carnegie, North Pines, North Fir Haven, South Fir Haven zones — ~1.5 h after the first order. | MEDIUM | S5 |
| 11:30–13:00 | **Three of the four southbound routes close, effectively simultaneously.** Source does not name which three. | WINDOW | S2 |
| through the day | **All five** egress arteries out of Paradise closed due to fire at least once. | HIGH | S2 |

Aggregate outcome: ~40,000 people evacuated from Concow, Paradise and Magalia;
31 temporary refuge areas sheltering 1,200+ civilians; 85 dead. Journeys that
take 25 minutes in normal traffic took hours "if they were passable at all" [S3].

---

## The five egress arteries

Paradise sits on a ridge. Every way out runs downhill and there are five of
them. Two reach CA-99 (Skyway, Neal Rd); two reach CA-70 (Clark Rd, Pentz Rd);
one drops west into Butte Creek Canyon (Honey Run Rd) [S2, S7].

Road centrelines in `src/roads.py` are approximate polylines (a handful of
waypoints per artery, ~100 m positional accuracy). That is adequate for the
only question asked of them — did a 375 m detection pixel cross this road —
and is documented as approximate rather than surveyed.

---

## SCORED CLOSURE EVENTS

This is the table `hindcast.py` reads. It is parsed directly out of this file
so the numbers can never drift from their citations.

```firebreak-ground-truth
# fire_id | road_id   | closed_from | closed_to | tier       | source | note
camp_fire | pentz_rd  | 2018-11-08T09:00:00-08:00 | 2018-11-08T09:00:00-08:00 | HIGH   | S2 | First artery to close. Point time stated explicitly.
camp_fire | skyway    | 2018-11-08T08:30:00-08:00 | 2018-11-08T08:30:00-08:00 | HIGH   | S2 | Upper Skyway burnover; fire overtook traffic. Scored as loss-of-service on the upper segment.
camp_fire | southbound_3of4 | 2018-11-08T11:30:00-08:00 | 2018-11-08T13:00:00-08:00 | WINDOW | S2 | Three of four southbound routes, unnamed. Scored as a set-cardinality event over {skyway, clark_rd, neal_rd, pentz_rd}.
camp_fire | clark_rd  | UNVERIFIED | UNVERIFIED | UNVERIFIED | S1 | Closed at least once (S2 aggregate). Exact time is in TN 2252; not readable at build time. EXCLUDED FROM METRICS.
camp_fire | neal_rd   | UNVERIFIED | UNVERIFIED | UNVERIFIED | S1 | Closed at least once (S2 aggregate). Exact time is in TN 2252; not readable at build time. EXCLUDED FROM METRICS.
camp_fire | honey_run_rd | UNVERIFIED | UNVERIFIED | UNVERIFIED | S1 | Closed at least once (S2 aggregate). Exact time is in TN 2252; not readable at build time. EXCLUDED FROM METRICS.
```

**Scored: 3 events (2 point, 1 window). Excluded: 3 events.**

---

## How to close the three UNVERIFIED rows

They are all in NIST TN 2252. The PDF is 278 pp and ~11 MB, which exceeded the
fetch limit available during this build. To complete the table:

```bash
curl -L -o data/NIST.TN.2252.pdf https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2252.pdf
python -m src.extract_ground_truth data/NIST.TN.2252.pdf
```

`src/extract_ground_truth.py` searches the extracted text for artery names
within a clock-time context and prints candidate rows **for a human to
confirm** before they are pasted into the block above. It deliberately does not
write to this file: ground truth gets a human in the loop, always.

---

## Known weaknesses of this ground truth

1. **Half the arteries are unscored.** Recall is computed over 3 events. That is
   a small denominator and a single miss moves it by 33 points. Reported as-is.
2. **"Closure" is not one thing.** NIST records burnover, congestion collapse,
   and formal closure as distinct failures. `skyway` at 08:30 is a burnover that
   stopped traffic, not an administrative closure. Treating both as "the road
   stopped working" is a modelling choice, stated here rather than buried.
3. **The window event is weakly constrained.** "Three of four, between 11:30 and
   13:00" is scored as cardinality — the model must flag ≥3 of the four
   southbound arteries inside the window. It cannot be scored per-road.
4. **Single-fire ground truth.** Everything here is one fire on one day. Camp
   Fire is the *hardest* case (extreme wind, pre-dawn ignition, ridge town) and
   also the best documented. It is not a representative sample of anything.
