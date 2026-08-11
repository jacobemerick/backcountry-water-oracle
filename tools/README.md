# tools/

Research scripts. **Not part of the engine**, not covered by the test suite, and
nothing in `forecast.py` imports them. They hit the network; the engine and its
tests never do. Stdlib-only, like everything else here.

## `precip_bakeoff.py`

Answers one question: **does changing the precipitation product change the
answer?** Runs the engine over the same sources under ERA5 (the default),
PRISM and MRMS, and diffs what a user would actually see — correlations, best
window, type, verdict.

```bash
python3 tools/precip_bakeoff.py examples/mazatzal-wilderness.csv --asof 2026-07-13
python3 tools/precip_bakeoff.py area.csv --since 2014-01-01   # fair MRMS window
python3 tools/precip_bakeoff.py area.csv --probe              # grid resolution only
```

Works on any source CSV, so a second area can be checked before committing to a
backend. Every fetch is cached under `.cache/iem/`, so re-runs are free and IEM
gets asked once per coordinate-year (with a delay between requests — it's a free
service).

Since [#17] the tool does no fetching of its own: PRISM and MRMS are engine
built-ins now (`--precip iem:prism` / `iem:mrms`) and this asks for them by name,
so a bake-off can't end up measuring its own fetching alongside the products.
Caching and the be-polite delay moved there with them. The refactor was checked
against the cache the original run left behind — **0 differing days out of
7,134** — so the findings below are unchanged.

**`--since` matters for MRMS.** IEM serves MRMS values back to 2007, but the
product is only genuine from ~2014; earlier values are a backfilled proxy. Pass
`--since 2014-01-01` for an honest comparison.

### What the first run found (Mazatzal, 2026-08-05)

Full write-ups are on issues [#17] and [#21]. Three things, in the order they
change decisions:

**1. This endpoint has ~12 km resolution, whatever the product's native grid.**
All three Mazatzal sources — 3.5 km apart — get **byte-identical** series under
ERA5, PRISM *and* MRMS. `--probe` shows why: served values are constant across an
8 km scan and change exactly when `iemre_i` changes. Everything is resampled onto
the IEMRE grid (0.125° ≈ 11.6 km). The `prism_grid_i`/`mrms_iemre_grid_i` fields
track the IEMRE cell centroid, which is what makes it look native. **So `iem:prism`
is not a resolution upgrade over ERA5 (~9–11 km)** and cannot separate two springs
across a ridge.

**2. PRISM is not the monsoon fix. MRMS is.** On the 2014+ genuine window,
Jun–Sep totals run ERA5 6.72"/yr, PRISM 8.02", **MRMS 13.98"** (for a record
ending 2026-07-30 — the per-year averages creep as the record grows, so re-runs
land near these rather than on them; the ratio is the finding, not the digits).
On individual convective days PRISM misses cells right alongside ERA5:

```
date          ERA5   PRISM    MRMS
2016-07-19    0.04    0.00    3.66
2025-09-18    0.37    0.00    3.07
2021-07-29    0.12    0.00    2.66
```

Gauge interpolation has nothing to interpolate from where no gauge sits under the
storm.

**3. The model is robust; the current read is not.** Season-controlled r moves by
<0.05 across products and **no best window or type classification changes**. But
the as-of antecedent totals differ enough (Castersen 60d: ERA5 0.86" / PRISM 0.50"
/ MRMS 2.23") to flip the verdict on all three sources. The value of a better
product sits entirely in "how much rain fell recently" — exactly what the ERA5
caveat in every output already warns about.

That's the evidence behind reshaping [#17]/[#18] toward MRMS-as-cross-check
rather than a wholesale backend swap.

[#17]: https://github.com/jacobemerick/backcountry-water-oracle/issues/17
[#18]: https://github.com/jacobemerick/backcountry-water-oracle/issues/18
[#21]: https://github.com/jacobemerick/backcountry-water-oracle/issues/21
