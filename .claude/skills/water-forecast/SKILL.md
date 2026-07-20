---
name: water-forecast
description: >
  Estimate whether a backcountry water source (seep, spring, creek, tank, falls)
  will be running, from its historical field reports plus precipitation. Use when
  the user asks "will <source> be running?", "what about this water source",
  shares water-report text / a file / a URL from ANY source (hikeArizona, FarOut/
  Guthook, a PCT/AZT water spreadsheet, personal trail notes), or wants the
  reliability table before a trip. Invoke as: /water-forecast <raw text | file path | URL> [more...]
---

# water-forecast

You normalize messy, arbitrary water-report input into the engine's CSV schema,
run the deterministic engine, and present the result. The engine (`forecast.py`)
does the math; YOU do the fuzzy parsing and scoring. Never edit the engine to fit
a new input format — adapt the input to the schema instead.

## Procedure

1. **Gather input(s).** The argument(s) may be:
   - raw pasted text → use as-is
   - a file path → read it
   - a URL → fetch it (WebFetch). If the page is login-gated and you can't read
     it, tell the user and ask them to paste/download the report text.
   You may receive several inputs and/or several sources in one input — that's
   expected and good (see "Multiple sources" below).

2. **Extract, per source:**
   - `source` — a name/id (dedupe: same real source = same name across rows)
   - `lat`, `lon` — decimal degrees. Convert if given as DMS
     (e.g. `N34 05.142 W111 29.449` → 34.0857, -111.4908). If coordinates are
     truly absent, ask the user for them — the engine cannot run without them.
   - one row per dated observation: `date` (ISO) + a `score` (see rubric).

3. **Score each observation 0.0–1.0** (0.0 = bone dry, 1.0 = max flow). Use your
   judgment on free text; the rubric below anchors it. Preserve the original
   wording in the `status` column for provenance.

4. **Write the CSV** to a temp path, e.g. `/tmp/water-forecast.csv`, with header:
   ```
   source,lat,lon,date,score,status
   ```

5. **Run the engine:** `python3 forecast.py /tmp/water-forecast.csv`
   (add `--asof YYYY-MM-DD` if the user named a future trip date). Run it from
   the repo root so it finds its cache.

6. **Present the result:** show the per-source breakdown and, for multiple
   sources, the SUMMARY table. Always relay the ERA5/monsoon caveat for any
   summer (Jun–Sep AZ) go/no-go: the model is the base rate, not "this week" —
   cross-check radar (MRMS via IEM, or NWS AHPS precip) before committing.

## Scoring rubric (0.0 – 1.0)

| score | meaning | example phrasings |
|------|---------|-------------------|
| 0.0 | dry | "dry", "bone dry", "no water", "no flow & no pools" |
| 0.2 | water present, ~not flowing | "dripping", "pools to trickle", "stagnant pools", "spring box full, no flow" |
| 0.4 | trickle / light flow | "light flow", "trickle over the falls", "small but filterable flow" |
| 0.6 | moderate | "medium flow", "quart per minute", "flowing well, easy to filter" |
| 0.8 | strong | "heavy flow", "gallon per minute", "rock-hopping the crossing" |
| 1.0 | max | "gallon+ per minute", "raging", "5+ GPM", "creek running full length" |

Interpolate freely (e.g. "barely a trickle" ≈ 0.3). When a report says the source
itself is dry but downstream tanks/pools hold water, score the *usable water*
present (that's what a hiker cares about) and note it in `status`.

## Multiple sources & borrowing strength (important)

A data-poor source (few reports) forecasts poorly on its own, but nearby sources
of the *same character* inform it. So:
- **Always include nearby sources** the user has data for, not just the target —
  put them all in one CSV. The engine tables them side by side.
- When a target has < ~25 reports, lean your spoken interpretation on its
  better-sampled neighbors of the same TYPE (the engine labels each Reliable /
  Flashy / Intermediate). Trust a data-rich neighbor's best-window "memory" over
  a tiny source's own noisy pick.
- Automated pooling (partial-pooling across a proximity+type group) is a planned
  engine feature; until then, do this reasoning in your summary to the user.

## CSV schema (the contract)

`source,lat,lon,date,score[,status]` — score is a float 0.0–1.0; extra columns
are ignored; rows with the same `source` name are treated as one source. This is
the only thing the engine understands; everything above is about producing it.
