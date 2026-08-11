# backcountry-water-oracle

[![tests](https://github.com/jacobemerick/backcountry-water-oracle/actions/workflows/tests.yml/badge.svg)](https://github.com/jacobemerick/backcountry-water-oracle/actions/workflows/tests.yml)

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

**B. Directly (any language / no Claude):** produce the CSV yourself and run it.
From a checkout, no install needed:
```bash
python3 forecast.py examples/mazatzal-wilderness.csv          # one file, many sources
python3 forecast.py a.csv b.csv                               # combine files
python3 forecast.py sources.csv --asof 2026-08-15            # read for a future date
python3 forecast.py sources.csv --no-cache                    # force precip re-fetch
python3 forecast.py area.csv --pool-radius 15                 # neighbor radius km (pooling)
python3 forecast.py area.csv --no-pool                        # analyze each source alone
python3 forecast.py az.csv --precip iem:mrms                  # a different precip product
cat area.csv | python3 forecast.py -                          # read the CSV from stdin
python3 forecast.py area.csv --json                           # machine-readable output
```
Or install it and get a `water-forecast` command anywhere:

```bash
pip install git+https://github.com/jacobemerick/backcountry-water-oracle@v0.1.0
water-forecast area.csv --json
water-forecast --version
```

Zero runtime dependencies, installed or not — the packaging exists so embedders
can pin a version instead of vendoring a copy, not to open a door to libraries.
Precip comes from the free
[Open-Meteo ERA5 archive](https://open-meteo.com/) (no key). It's cached in
`.cache/` beside the engine when you're working in a checkout, and otherwise in
your platform's user cache directory (`~/Library/Caches/backcountry-water-oracle`,
`~/.cache/...`, `%LOCALAPPDATA%\...`). Set `WATER_ORACLE_CACHE` to override, or
assign `forecast.CACHE_DIR` if you're embedding.

## Embedding the engine (hosts)

`forecast.py` is a module as well as a CLI. There are three seams, and between
them a host should never need to reach for anything private — or reimplement
anything.

```python
import io
import backcountry_water_oracle as bwo
from datetime import date

bwo.PRECIP_PROVIDER = my_provider                        # optional, see below
sources = bwo.load_sources_from([io.StringIO(request_body)])
payload = bwo.run(sources, date(2026, 8, 15))            # what --json prints
```

Pin it. `pip install git+https://github.com/…@v0.1.0` — an exact pin makes every
upgrade deliberate, which is the point after the alternative (a vendored copy)
drifted twice.

### Running the engine

`run()` is `main()` minus argument parsing and output: the same three passes, the
same skip and error handling, returning the same dict `--json` prints. Use it
rather than calling `analyze_base()` / `pool_controlled()` / `finalize()` yourself
— a copy of those passes silently drifts from the engine as it changes, and a
service that did exactly that started returning 500s the first time the engine
learned a new way to skip a source.

```python
payload = forecast.run(sources, asof, harmonics=1, pool=True,
                       pool_radius_km=25.0, use_cache=True, precip=None,
                       notes=None, on_note=None)
```

`asof` defaults to today. `precip` names a built-in product (see
[`--precip`](#choosing-a-precip-product---precip)); `None` means "whatever
`PRECIP_PROVIDER` is", so a host that assigned its own callable keeps it.
`notes` may be a list to append to — pre-seed it with
anything that went wrong earlier (a rejected upload, say) and it appears in the
payload. `on_note` is called with each note as it happens, for logging or
streaming. Sources that can't be analysed become notes; they never raise.

### Loading CSV that isn't on disk

`load_sources()` takes file paths (or `-` for stdin), which an HTTP service
holding a request body has neither of. Use `load_sources_from()`:

```python
import io, forecast

sources = forecast.load_sources_from([io.StringIO(request_body)], labels=["<request>"])
payload = forecast.run(sources, asof)
```

It takes already-open **text** streams — anything `csv.DictReader` can read — and
`labels` name them in error messages the way filenames do, so "CSV missing
column(s)" says *which* input was wrong. Reports come back sorted; the caller owes
nothing. `load_sources()` is implemented on top of it, so both paths are the same
code.

### Where precipitation comes from

This script caches precip to a `.cache/` directory next to itself, which is wrong
for a serverless app that wants the series in a shared store (and rude to
Open-Meteo, since every cold invocation refetches). Assign your own provider:

```python
import forecast

def my_provider(lat, lon, end_date, use_cache=True):
    series = my_store.get(lat, lon)                       # Postgres, KV, S3, ...
    if series is None or series["daily"]["time"][-1] < end_date.isoformat():
        series = forecast.open_meteo_provider(lat, lon, end_date, use_cache)
        my_store.put(lat, lon, series)                    # or fetch it your own way
    return series

forecast.PRECIP_PROVIDER = my_provider
payload = forecast.run(forecast.load_sources(["reports.csv"]), asof)
```

The contract is one daily series per coordinate:

```python
provider(lat, lon, end_date, use_cache) -> {"daily": {
    "time":              ["2007-01-01", ...],   # ascending ISO dates
    "precipitation_sum": [0.0, ...],            # inches, same length; None reads as 0.0
}}
```

- Returning **more** than `end_date` is fine — the engine trims, so you can keep
  one long series per coordinate and serve every as-of date from it.
- `use_cache=False` means the caller wants fresh data; bypass your cache.
- A provider that returns the wrong shape fails at the seam with a message naming
  it, rather than a `KeyError` from inside the stats code.
- `forecast.CACHE_DIR` is assignable too, if you only want the built-in cache
  somewhere else — it defaults to your platform's user cache directory, never to
  wherever the module happens to be installed.

No new dependency, no change to the CLI, and the engine stays sterile — it still
only ever sees lat/lon + flow + precip.

### Choosing a precip product (`--precip`)

Assigning `PRECIP_PROVIDER` is for supplying *your own* backend. To pick one of
the products the engine already ships, name it — on the CLI or in `run()`:

```bash
python3 forecast.py az.csv --precip iem:mrms
```
```python
payload = forecast.run(sources, asof, precip="iem:mrms")
```

| name | product | coverage |
|---|---|---|
| `open-meteo` | **ERA5 reanalysis (the default)** | global |
| `iem:prism` | PRISM via Iowa State's IEMRE point service | CONUS |
| `iem:mrms` | MRMS radar via the same service | CONUS |

Both `iem:*` providers are chunked by year and cached under `.cache/iem/`, and one
download serves both products. **Set expectations from the bake-off**
([`tools/`](tools/), [#17]) before reaching for them:

- **This is not a resolution upgrade.** The IEMRE endpoint resamples every product
  onto its own 0.125° (~11.6 km) grid, which is no finer than ERA5's ~9–11 km. Two
  springs either side of a ridge still share a cell.
- **The fit barely moves** — season-controlled *r* shifts <0.05, and no best window
  or type changed on the worked example. What moves is the **as-of read**, because
  MRMS sees convective cells the others miss (a 3.66" day that ERA5 read as 0.04"
  and PRISM as 0.00"). That is worth a lot in monsoon season and nothing at all to
  the historical fit — which is why [#18] proposes MRMS as a *cross-check beside*
  the ERA5 model rather than a replacement for it.
- **MRMS is only genuine radar from ~2014**; earlier values are a backfilled proxy,
  and `--precip iem:mrms` fits the model across both. The engine says so in a
  `caveat` note on every such run.

Two rules the flag holds to. A product that can't serve a coordinate is an
**error for that source** — never a silent substitution, because a payload that
said `iem:prism` while ERA5 quietly answered would be worse than either outcome.
The rest of the sources still run, and the failure lands in `notes` as a `kind:
"error"`. And `params.precip` records what actually answered, so two stored
forecasts are only comparable when it matches — the same rule as
`params.engine_version`. A host whose own provider serves ERA5 from its own store
can say so with `my_provider.precip_name = "open-meteo"` and keep its payloads
comparable with everyone else's.

## Tests

```bash
python3 tests/test_forecast.py
```

159 tests. No dependencies, no config, **no network**, ~1 second. Precipitation
comes from a committed fixture through `PRECIP_PROVIDER`, and every test that
reads an as-of date passes one explicitly, so nothing depends on today's date or
on ERA5 not being revised. A golden test compares the entire `--json` payload for
the worked example, which is what catches silent numeric drift — the failure mode
you can't eyeball in a tool whose wrong answers look as plausible as its right
ones. See [`tests/README.md`](tests/README.md), especially before regenerating
that golden file.

**CI** runs the suite on every push and pull request, across Python 3.9–3.14 on
Linux plus macOS on 3.14 (the platform and version this is developed on). There
is no install step and there shouldn't ever be one — the engine and the suite are
both stdlib-only by rule.

## What's public

The version number only means something if the surface it covers is written
down. **These are supported** — a breaking change to any of them is a version
bump and a changelog entry:

| | |
|---|---|
| `run(sources, asof, …)` | the three passes; returns the `--json` payload |
| `load_sources(paths)` / `load_sources_from(streams, labels)` | the two loaders |
| `analyze(src, asof, …)` | single source, no pooling |
| `PRECIP_PROVIDER`, `open_meteo_provider(…)`, `CACHE_DIR` | the precip seam |
| `PRECIP_PROVIDERS`, `resolve_precip(name)`, `precip_name(provider)` | the named built-ins, and what a payload calls them |
| the **`--json` payload** | every key and its meaning |
| the **CLI** | flags, `-`/stdin, exit codes (`0` ok, `1` no input, `2` bad input) |
| `__version__` | also `--version`, also `params.engine_version` in the payload |

**Everything else is internal** — anything underscore-prefixed, plus
`analyze_base()`, `finalize()`, `pool_controlled()`, `run_json()`,
`parse_args()`, and the layout of the text report. They're importable, because
Python, but they move without notice. If you need one of them, that's worth an
issue: it usually means a supported seam is missing, which is exactly how
`load_sources_from()` and `run()` came to exist.

### What the version does *not* cover

**The numbers can move without any of the above changing.** A better pooling
prior or a season-control fix can leave every signature and every key identical
and still turn *Marginal* into *Likely DRY* for the same CSV on the same date —
that has already happened twice here (season control, then pooling).

Those changes go in the changelog, and every payload carries
`params.engine_version` so a stored forecast can be traced to the code that made
it. If you need two forecasts to be comparable, check that field: same version
means the method didn't move underneath you, and different versions mean it may
have.

**Check `params.precip` alongside it.** Same method, different rain, different
answer — the bake-off flipped the verdict on all three Mazatzal sources purely by
changing the product. Two forecasts are comparable when *both* fields match.

## The input contract (CSV schema)

The engine understands exactly one thing:

```
source,lat,lon,date,score,status
Chilson Spring,34.08587,-111.49097,2025-10-24,1.0,"Gallon+ per minute, box full"
Castersen Seep,34.09059,-111.46653,2026-06-30,0.0,"Dry"
```

| column | meaning |
|--------|---------|
| `source` | name/id; rows sharing a name are one source — and must agree on coordinates to within ~1 km, or the engine errors out rather than pick one (two different "Cottonwood Spring"s need two names) |
| `lat`,`lon` | decimal degrees |
| `date` | ISO `YYYY-MM-DD` — reports outside the precip record (from 2007) can't be correlated and are excluded, reported as `reports.excluded_*` |
| `score` | **float 0.0–1.0** — `0.0` = dry, `1.0` = max flow |
| `status` | *(optional)* raw text, kept for provenance only |

Anything that can emit this CSV can drive the engine. The mapping from real-world
report language to a `score` lives in the **skill's rubric** (see
`.claude/skills/water-forecast/SKILL.md`), not in the engine.

## Piping it around (`-` and `--json`)

The engine reads stdin and writes JSON, so it drops into a pipeline between your
own normalizer and your own consumer:

```bash
my-scraper | python3 forecast.py - --json | jq '.sources[] | {name, verdict}'
```

- **`-` as a filename reads the CSV from stdin.** It mixes freely with real
  paths (`forecast.py known.csv - --json`), and stdin is used automatically when
  no files are given and stdin isn't a terminal — so a bare `... | forecast.py`
  works too. (stdin is consumed once; a repeated `-` is ignored.)
- **`--json` prints one JSON object on stdout** instead of the text report, with
  every number the text report shows. Diagnostics (`[skip]`/`[error]`) move to
  **stderr** and are also collected under `notes`, so stdout stays valid JSON
  even when a source fails. Exit code is unchanged (`2` on a bad CSV).

```jsonc
{
  "asof": "2026-07-13",
  "params": { "engine_version": "0.1.0", "precip": "open-meteo",
              "pool": true, "pool_radius_km": 25.0, "harmonics": 1,
              "cache": true, "windows": [30, 60, 90, 180, 270, 365] },
  "sources": [{
    "name": "Castersen Seep", "lat": 34.09059, "lon": -111.46653,
    "n": 15, "small_n": true, "pct_dry": 33, "mean_flow": 0.4133,
    // `n` is what the analysis used; `reports` says what came in and what was
    // dropped for falling outside the precip record ("12 reports, 9 usable")
    "reports": { "total": 15, "used": 15, "excluded_before_precip": 0,
                 "excluded_after_precip": 0,
                 "precip_span": ["2007-01-01", "2026-07-13"] },
    "annual_precip_in": 19.22, "type": "Flashy (needs recent rain)",
    "mean_flow_by_month": { "4": 0.8, "5": 0.4, "...": 0 },
    "correlations": [ { "window": "180d", "days": 180,
                        "raw_r": 0.7161, "ctrl_r": 0.0935 } ],   // own numbers
    "best": { "window": "60d", "days": 60,
              "r": 0.4545,            // POOLED season-controlled r — drives the verdict
              "own_ctrl_r": 0.4029,   // this source alone
              "raw_r": 0.6718,        // before season control
              "borrowed": 0.6491, "group_n": 3,
              "signal_check": "partly seasonal, real signal remains" },
    "asof": "2026-07-13", "precip_in": 0.863,
    "predicted_flow": 0.12, "verdict": "Marginal - pools/dripping at best",
    "harmonics": 1
  }],
  "notes": []   // [{kind: "skip"|"error"|"caveat", source, message}, ...]
                // caveat = a limitation of the run itself (source is null),
                // e.g. --precip iem:mrms fitting across pre-2014 backfill
}
```

Sources come back in input order (the text summary's "most reliable first" sort
is a presentation choice — sort client-side on `pct_dry` if you want it). Both
the pooled and un-pooled correlations are exposed, so a consumer can re-derive or
second-guess the headline without re-running the engine.

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
- **r\*** (summary table) — the season-controlled correlation, *pooled* toward
  nearby sources where they agree; this is the number the verdict keys off.
- **POOL** (summary table) — what fraction of `r*` was borrowed from neighbors
  (`-` = no neighbors in range, or `--no-pool`). Each source's per-window table
  still shows its **own** raw and season-controlled r for full transparency.

## Multiple sources & pooling

Multi-source is native — one CSV can hold many sources and the engine tables them
together. A data-poor source (a seep with only ~15 reports) forecasts poorly
alone, so the engine lets it **borrow correlation strength from nearby sources.**

**How pooling works.** Any sources within `--pool-radius` km of each other (default
`25`, straight-line) form a neighborhood. Each source's season-controlled rain
correlation is then shrunk toward its neighbors' — but *how much* is decided by the
data, not by us:

- Neighbors that **agree** → pool hard; neighbors that **disagree** → barely pool
  (a buffered spring next to a flashy falls keeps its own signal).
- A **small-n** source leans on its neighbors more than a data-rich one does.

Nobody sets a shrinkage dial — it falls out of empirical Bayes (the posterior mean
of the between-source variance under a weak prior). The engine only ever sees
lat/lon + flow + precip, so pooling is purely a **geographic** prior: it never
assumes *why* two sources behave alike, only measures whether their rain responses
are consistent. Only the correlation is pooled — each source's `%-dry` and flow
numbers stay its own.

In the Mazatzal example, tiny **Castersen (n=15)** borrows ~65% of its signal from
its neighbors and gets rescued onto the rain window they validate, while buffered
**Chilson (n=58)** and well-sampled **Big Kahuna (n=160)** barely move. Pass
`--no-pool` to switch it off and read every source in isolation.

## Trust it this much (limitations)

1. **ERA5 (~9–11 km) smooths/misses isolated monsoon cells.** Trust the
   winter-recharge signal; for a summer "did a storm just hit" call, still check
   radar — [MRMS via IEM](https://mesonet.agron.iastate.edu/) or the
   [NWS AHPS precip analysis](https://water.weather.gov/precip/). This is the
   *base rate*; radar is *this week*. `--precip iem:mrms` will fit the whole model
   on radar instead, but that trades one problem for another (pre-2014 backfill in
   the analog pool) — reading radar *beside* the ERA5 model is [#18].
2. **Season and rain are entangled** — more reports in wet, cool months. The tool
   controls for this: it removes the day-of-year cycle (annual-harmonic
   regression, learned per-site so it's hemisphere-correct) and reports a
   **season-controlled r** beside each raw r. Classification and the headline key
   off the controlled one. Watch the gap — it can expose a big raw correlation as
   mostly seasonal (see the Mazatzal example: Castersen's raw .72 → ~.09).
3. **Scores** are a judgment mapping of free-text reports.
4. **Small-n sources (<25 reports)** are suggestive, not solid (flagged in output).

## Roadmap

Shipped:

- [x] **Pooling / borrow-strength** for small-n sources — proximity neighborhood
      (`--pool-radius`), data-driven empirical-Bayes shrinkage. `--no-pool` to disable.
- [x] **Season control** (day-of-year, annual harmonics) — reports a
      season-controlled r beside raw; classification keys off it. `--harmonics=N`.
- [x] **JSON output & stdin** (`--json`, `-`) so the engine composes in a pipeline.
- [x] **Injectable precip provider** so an embedding host can supply its own backend
      or shared cache — see [Embedding the engine](#embedding-the-engine-hosts).
- [x] **Test suite + CI** ([#15]) — stdlib, offline, deterministic; run on every
      push and PR across Python 3.9–3.14. See [Tests](#tests).
- [x] **Installable package** ([#26]) — `pip install`, a `water-forecast` command,
      and a declared public surface so embedders pin instead of vendoring.
- [x] **Pluggable precip product** ([#17]) — `--precip {open-meteo,iem:prism,iem:mrms}`,
      ERA5 still the default. The bake-off that reshaped this issue found it is *not*
      a resolution upgrade; see [`--precip`](#choosing-a-precip-product---precip).

Planned — the issue is where the detail and the open questions live:

- [ ] **MRMS radar cross-check** ([#18]) — report radar for the recent window beside
      the ERA5 fit, rather than refitting the model on it. The one the bake-off
      says is worth building.
- [ ] **Log-your-own-visits** so each source sharpens over time ([#19]).
- [ ] **Table export** (Markdown/HTML) for trip notes ([#20]).
- [ ] **Zero-report mode** — antecedent-rain percentile against a site's own
      climatology, for a source with no field reports at all ([#8]).
- [ ] **Earlier precip history** — whether `PRECIP_START` can move back from 2007,
      given ERA5 reaches 1940 ([#21]).

[#8]: https://github.com/jacobemerick/backcountry-water-oracle/issues/8
[#15]: https://github.com/jacobemerick/backcountry-water-oracle/issues/15
[#17]: https://github.com/jacobemerick/backcountry-water-oracle/issues/17
[#18]: https://github.com/jacobemerick/backcountry-water-oracle/issues/18
[#19]: https://github.com/jacobemerick/backcountry-water-oracle/issues/19
[#20]: https://github.com/jacobemerick/backcountry-water-oracle/issues/20
[#21]: https://github.com/jacobemerick/backcountry-water-oracle/issues/21
[#26]: https://github.com/jacobemerick/backcountry-water-oracle/issues/26

## Example data

`examples/mazatzal-wilderness.csv` — three Mazatzal Wilderness sources
(Castersen Seep, Big Kahuna Falls, Chilson Spring) as a worked, multi-source
example. The original raw reports they were normalized from live in
`examples/raw/` as sample inputs for the skill.
