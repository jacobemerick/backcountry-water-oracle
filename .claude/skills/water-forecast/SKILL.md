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
     **A source with coordinates but no reports still belongs in the CSV** — write
     one row with `date` and `score` blank (`Unnamed seep,34.09,-111.47,,`). The
     engine answers with rain context and no verdict; see "Sources with no
     reports" below. Never invent an observation to give it a row.
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
   Add `--asof YYYY-MM-DD` if the user named a future trip date. The precip cache
   follows the engine rather than the working directory, so it doesn't matter
   where you run it from. **Read the JSON — never parse the text report**; the
   JSON carries the same numbers in labeled fields, so you can't misread a column. (You can also pipe the CSV straight in with `-` instead of a
   temp file — `... | python3 forecast.py - --json` — but a temp file is worth
   keeping: the user can rerun and tweak it.)

   If the engine writes anything to **stderr**, or `notes` is non-empty, say so —
   silence would read as "all good". `notes[].kind` is:
   - `skip` / `error` — a source that was dropped or blew up (`notes[].source`
     names it). Relay which source, and why.
   - `caveat` — a limitation of the *run*, not of any one source
     (`notes[].source` is null), e.g. `--precip iem:mrms` fitting the model
     across pre-2014 backfill. Pass it on in your own words; it's a reason to
     trust the answer less, and the user can't see it unless you say it.

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
   | how unusual the run-up is | `rain_percentiles` (see below) |

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

## Precip products (`--precip`) — leave it alone by default

The engine can fit on products other than ERA5: `--precip iem:prism` or
`iem:mrms` (CONUS only; `params.precip` in the JSON records which one answered).

**Default to not passing it.** ERA5 is the default for good reasons and the
bake-off found the *fit* barely moves between products — season-controlled r
shifts <0.05, no window or type changes. What moves is the as-of read, enough to
flip verdicts. So a second product is a way to ask "how load-bearing is this
call?", not a better answer:

- If a user is making a **summer go/no-go** and wants the radar view, re-run with
  `--precip iem:mrms` and present it **beside** the ERA5 answer, never instead of
  it — MRMS is backfilled proxy before ~2014, so its historical fit is mixed. Say
  which number came from which product.
- Never compare a stored/earlier forecast against a new one unless
  `params.precip` matches on both. Same for `params.engine_version`.
- Outside CONUS the `iem:*` products fail that source with an `error` note rather
  than quietly substituting ERA5 — if you see that, just re-run without the flag.

## Antecedent rain (`rain_percentiles`) — context, never a verdict

Every source carries each window's antecedent rain ranked against the same
calendar window in every other year of its own record:
`{"180d": {"inches": 4.15, "pct": 21, "n_years": 19, "median_in": 8.14}, ...}`.

Use it to say **how unusual the run-up to this date has been** — that's something
the verdict can't tell you, because the verdict is a base rate. One sentence is
usually enough, and the source's `best.window` is the one worth quoting:

> The last 180 days are in the 21st percentile for this date (4.15" vs a typical
> 8.14") — a dry winter, though the year as a whole is ordinary (61st).

Three rules:

- **Never present it as a flow reading**, and never let it override `verdict`.
  Wet ground is not water in the creek; the mapping from rain to flow is exactly
  what the engine learns per-source from reports, and rain alone doesn't have it.
  If the percentile and the verdict seem to disagree, report both and say the
  verdict is the one built from this source's behavior.
- **Don't quote a precise rank.** ~19 years means ~5-point steps. "Unusually
  dry", "about normal", "unusually wet" is the honest resolution.
- `pct` may be `null` (a window no earlier year covers) — say nothing rather than
  guessing.

## Sources with no reports

A source with **no usable reports** — a bare pin, or one whose every report
predates 2007 — still comes back in `sources`, with `n: 0` and every
verdict-derived field `null` (`verdict`, `best`, `type`, `pct_dry`,
`predicted_flow`, `precip_in`). `rain_percentiles` is populated.

- **Check `n == 0` before speaking about a source.** Don't render a null verdict,
  and don't fill the gap with your own guess from the rain numbers.
- Say plainly what it is: *"nobody has reported on this one, so there's no flow
  call — only that the last 60 days are in the 42nd percentile for this date."*
- **The strongest thing you can offer is a neighbor**: if the CSV has a reported
  source nearby, name it, give its verdict, and say how far away it is and that
  it is a different source. Do this in your prose — the engine does not transfer
  a read across sources, and you must not imply it did.
- If the user wants a real answer for that pin, tell them what would produce one:
  any dated observation at all, or reports from a nearby source to include.

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
are ignored; rows with the same `source` name are treated as one source. A row
with **both** `date` and `score` blank is a coordinate-only source (a pin); one
of the two blank is rejected as a typo. This is the only thing the engine
understands; everything above is about producing it.

The engine takes this CSV as file path(s) or on stdin (`-`), and answers as the
text report or as `--json`. It never learns an input format — if a new site's
reports don't fit, that's a parsing job for you, not an engine change.
