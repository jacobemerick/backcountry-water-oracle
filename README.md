# backcountry-water-oracle

Will that seep be running? This tool correlates a backcountry water source's
historical **field reports** against ~19 years of daily precipitation for its
coordinates, and gives a rough read on whether it's likely to have water — plus
a breakdown of *what kind* of source it is (flashy runoff vs. buffered
groundwater) and a comparison table across the sources in an area.

It's built in two layers so it works for anyone, with any report format:

- **`forecast.py` — the engine.** Deterministic, dependency-free, and
  **format-agnostic**: it consumes one normalized **CSV** and knows nothing about
  any particular website. This is what you embed in your own code.
- **`/water-forecast` — the skill** (for Claude Code users). Give it *anything* —
  pasted report text, a file, or a URL, from hikeArizona / FarOut / a trail
  spreadsheet / your own notes — and it normalizes it into the CSV, runs the
  engine, and shows you the table.

## Two ways to use it

**A. With Claude Code (easy mode):**
```
/water-forecast <raw text | file path | URL>  [more sources...]
```
The skill parses whatever you give it, scores each observation, and runs the
engine. Include several nearby sources at once for a better read (see below).

**B. Directly (any language / no Claude):** produce the CSV yourself and run:
```bash
python3 forecast.py examples/mazatzal-wilderness.csv          # one file, many sources
python3 forecast.py a.csv b.csv                               # combine files
python3 forecast.py sources.csv --asof 2026-08-15            # read for a future date
python3 forecast.py sources.csv --no-cache                    # force precip re-fetch
```
Pure Python standard library — no `pip install`. Precip comes from the free
[Open-Meteo ERA5 archive](https://open-meteo.com/) (no key), cached in `.cache/`.

## The input contract (CSV schema)

The engine understands exactly one thing:

```
source,lat,lon,date,score,status
Chilson Spring,34.08587,-111.49097,2025-10-24,1.0,"Gallon+ per minute, box full"
Castersen Seep,34.09059,-111.46653,2026-06-30,0.0,"Dry"
```

| column | meaning |
|--------|---------|
| `source` | name/id; rows sharing a name are one source |
| `lat`,`lon` | decimal degrees |
| `date` | ISO `YYYY-MM-DD` |
| `score` | **float 0.0–1.0** — `0.0` = dry, `1.0` = max flow |
| `status` | *(optional)* raw text, kept for provenance only |

Anything that can emit this CSV can drive the engine. The mapping from real-world
report language to a `score` lives in the **skill's rubric** (see
`.claude/skills/water-forecast/SKILL.md`), not in the engine.

## How to read the output

- **TYPE** — heuristic label:
  - *Reliable (groundwater-buffered)* — dry <10% of the time; flow barely tracks
    recent rain (that's *why* it's reliable).
  - *Flashy (needs recent rain)* — best predicted by short (≤90-day) windows and
    often dry. On fast after storms, off fast.
  - *Intermediate* — medium-window memory (e.g. creeks with rock tanks).
- **rain-window correlation** — Spearman r between flow and antecedent rain over
  30/60/90/180/270/365 days. Highest |r| = the source's effective "memory."
- **AS-OF READ** — nearest-analog estimate: current value of the best window vs.
  the 5 historical reports with the most similar antecedent rain.

## Multiple sources & the small-n problem

Multi-source is native — one CSV can hold many sources and the engine tables them
together. A data-poor source (like a seep with only ~15 reports) forecasts poorly
alone, but **nearby sources of the same type inform it.** For now, include the
neighbors and lean on the better-sampled ones of the same TYPE when interpreting.
Automated **pooling** (borrow strength across a proximity+type group) is the next
planned feature — see roadmap.

## Trust it this much (limitations)

1. **ERA5 (~9–11 km) smooths/misses isolated monsoon cells.** Trust the
   winter-recharge signal; for a summer "did a storm just hit" call, still check
   radar — [MRMS via IEM](https://mesonet.agron.iastate.edu/) or the
   [NWS AHPS precip analysis](https://water.weather.gov/precip/). This is the
   *base rate*; radar is *this week*.
2. **Season and rain are entangled** — more reports in wet, cool months. The tool
   controls for this: it removes the day-of-year cycle (annual-harmonic
   regression, learned per-site so it's hemisphere-correct) and reports a
   **season-controlled r** beside each raw r. Classification and the headline key
   off the controlled one. Watch the gap — it can expose a big raw correlation as
   mostly seasonal (see the Mazatzal example: Castersen's raw .72 → ~.09).
3. **Scores** are a judgment mapping of free-text reports.
4. **Small-n sources (<25 reports)** are suggestive, not solid (flagged in output).

## Roadmap

- [ ] **Pooling / borrow-strength** for small-n sources (proximity + type group).
- [x] **Season control** (day-of-year, annual harmonics) — reports a
      season-controlled r beside raw; classification keys off it. `--harmonics=N`.
- [ ] **Higher-res precip** option (PRISM / Daymet, or radar QPE for monsoon).
- [ ] **Log-your-own-visits** so each source sharpens over time.
- [ ] **Table export** (Markdown/HTML) for trip notes.

## Example data

`examples/mazatzal-wilderness.csv` — three Mazatzal Wilderness sources
(Castersen Seep, Big Kahuna Falls, Chilson Spring) as a worked, multi-source
example. The original raw reports they were normalized from live in
`examples/raw/` as sample inputs for the skill.
