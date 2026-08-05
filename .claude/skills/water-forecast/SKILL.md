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
   - `source` — a name/id (dedupe: same real source = same name across rows).
     **Two different sources must not share a name.** Trail names repeat
     constantly ("Cottonwood Spring" is everywhere); if you're merging reports
     from different areas, qualify them (`Cottonwood Spring (Mazatzal)`). The
     engine rejects a name whose rows disagree on position by more than ~1 km
     rather than silently correlating one spring against the other's weather —
     if you see that error, you merged two sources.
   - `lat`, `lon` — decimal degrees. Convert if given as DMS
     (e.g. `N34 05.142 W111 29.449` → 34.0857, -111.4908). If coordinates are
     truly absent, ask the user for them — the engine cannot run without them.
     Small disagreements between reporters (GPS scatter) are fine; just be
     consistent per source.
   - one row per dated observation: `date` (ISO) + a `score` (see rubric).
     **Reports before 2007 can't be used** — the precipitation record starts
     there. Include them anyway; the engine counts them rather than dropping
     them silently, and that count is worth relaying (see step 7).

3. **Score each observation 0.0–1.0** (0.0 = bone dry, 1.0 = max flow). Use your
   judgment on free text; the rubric below anchors it. Preserve the original
   wording in the `status` column for provenance.

4. **Write the CSV** to a temp path, e.g. `/tmp/water-forecast.csv`, with header:
   ```
   source,lat,lon,date,score,status
   ```

5. **Run the engine for the numbers — always with `--json`:**
   ```bash
   python3 forecast.py /tmp/water-forecast.csv --json
   ```
   Add `--asof YYYY-MM-DD` if the user named a future trip date. Run it from the
   repo root so it finds its cache. **Read the JSON — never parse the text
   report**; the JSON carries the same numbers in labeled fields, so you can't
   misread a column. (You can also pipe the CSV straight in with `-` instead of a
   temp file — `... | python3 forecast.py - --json` — but a temp file is worth
   keeping: the user can rerun and tweak it.)

   If the engine writes anything to **stderr**, or `notes` is non-empty, say so —
   those are sources that were skipped or failed, and silence would read as
   "all good". `notes[].kind` is `skip` or `error`.

6. **Show the user the engine's own report too**, when they'd want the table:
   rerun the same command without `--json` and show that output verbatim. Precip
   is cached from the first run, so this is instant and costs nothing.

7. **Present the result.** Lead with the verdict per source, then what drives it.
   Map the JSON fields you're speaking from:

   | say this | from |
   |---|---|
   | the call | `verdict`, `predicted_flow` |
   | what kind of source | `type`, `pct_dry` |
   | what it responds to | `best.window` + `best.r` |
   | how much is borrowed | `best.borrowed` (× 100 = %), `best.group_n` |
   | is the signal real | `best.signal_check`, `best.raw_r` vs `best.own_ctrl_r` |
   | how shaky | `small_n` (true = < 25 reports — flag it out loud) |
   | how much data was usable | `reports.total` vs `reports.used` |

   Two honesty rules: if `best.borrowed` is high (say > 0.5), **state that the
   read leans on neighbors** rather than the source's own record. And if
   `best.raw_r` is much larger than `best.own_ctrl_r`, say the raw correlation was
   mostly seasonal — that gap is the whole point of season control.

   A third: when `reports.used < reports.total`, **say so** — "12 reports, 9
   usable; three predate the precipitation record (2007)". The user gave you
   those reports and will otherwise wonder why `n` shrank. If a source is skipped
   entirely it lands in `notes` with the same explanation.

   Always relay the ERA5/monsoon caveat for any summer (Jun–Sep AZ) go/no-go: the
   model is the base rate, not "this week" — cross-check radar (MRMS via IEM, or
   NWS AHPS precip) before committing.

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
inform it — and **the engine now does this borrowing itself**, so your job is to
feed it neighbors and explain what it did.

- **Always include nearby sources** the user has data for, not just the target —
  put them all in one CSV. The engine tables them side by side, and it can only
  pool across sources it can see in one run.
- **Pooling is automatic and on by default.** Sources within `--pool-radius` km
  (default 25) form a neighborhood, and each source's season-controlled rain
  correlation is shrunk toward its neighbors' by empirical Bayes: neighbors that
  agree pool hard, neighbors that disagree barely pool, and a small-n source
  leans on its neighbors more than a data-rich one. You don't set a dial — read
  `best.borrowed` and `best.group_n` to see what happened.
- **Don't redo this reasoning by hand.** The old advice ("lean on the neighbors
  yourself") is now the engine's job — `best.r` is already the pooled number and
  the verdict already keys off it. Your job is to *narrate* it: "this seep only
  has 15 reports, so ~65% of its signal is borrowed from two neighbors within 25
  km."
- Only the correlation is pooled — `pct_dry`, `mean_flow`, and the flow numbers
  are always each source's own. Say so if a user asks why a heavily-pooled source
  still shows its own dry rate.
- Pass `--no-pool` (or a smaller `--pool-radius`) if the user wants each source
  read in isolation — e.g. to see how much a small source is actually leaning.

## CSV schema (the contract)

`source,lat,lon,date,score[,status]` — score is a float 0.0–1.0; extra columns
are ignored; rows with the same `source` name are treated as one source. This is
the only thing the engine understands; everything above is about producing it.

The engine takes this CSV as file path(s) or on stdin (`-`), and answers as the
text report or as `--json`. It never learns an input format — if a new site's
reports don't fit, that's a parsing job for you, not an engine change.
