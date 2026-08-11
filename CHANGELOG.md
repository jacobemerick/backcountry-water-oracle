# Changelog

What changed, and — separately — **whether the numbers moved**.

The version covers the Python API and the `--json` payload (README, ["What's
public"](README.md#whats-public)). It does *not* cover the answers: a change to
the method can leave every signature and every key identical and still turn
*Marginal* into *Likely DRY* for the same CSV on the same date. That has happened
twice. So every release below says explicitly whether verdicts moved, and every
payload carries `params.engine_version` so a stored forecast can be traced to the
code that produced it.

## 0.2.0 — 2026-08-11

Precipitation products, and sources with no reports.

**No verdict changed.** Every number in the worked example is byte-identical to
0.1.0 — across the whole release the golden payload gained keys and nothing else,
and the only value that moved was the version string itself.

### Added

- **`--precip {open-meteo,iem:prism,iem:mrms}`** and `run(precip=…)` — choose the
  precipitation product. **Open-Meteo ERA5 stays the default.** `params.precip`
  records which product actually answered. Outside CONUS the `iem:*` providers
  fail that source with an `error` note rather than silently substituting ERA5.
  ([#17](https://github.com/jacobemerick/backcountry-water-oracle/issues/17))
- **Radar cross-check.** `radar_check` per source: MRMS rain over the recent
  window (30d/60d) beside the model's own figure, with a ratio. Reported *next to*
  the model and never inside it — the analog pool is built from the model's own
  history, so radar informs the reader and cannot move the verdict. `--radar none`
  or `RADAR_PROVIDER = None` switches it off; `params.radar` records what
  cross-checked. This retires the "go and check radar yourself" line every summer
  forecast used to end with.
  ([#18](https://github.com/jacobemerick/backcountry-water-oracle/issues/18))
- **`rain_percentiles` per source** — each window's antecedent rain ranked against
  the same calendar window in every other year of that coordinate's record. Needs
  no field reports, so it is the only reading an unreported coordinate gets, and
  it is useful context everywhere else. Explicitly **not** a flow verdict.
  ([#8](https://github.com/jacobemerick/backcountry-water-oracle/issues/8))
- **Coordinate-only CSV rows.** A row with `date` *and* `score` blank is a pin:
  `Unnamed seep,34.09,-111.47,,`. Rain context, no verdict.
- **`neighbors` / `neighbors_disagree`** on a source with no verdict — the reported
  sources within `--pool-radius`, nearest first, each carrying its *own* read.
  Nothing is combined or transferred. `neighbors_disagree` is the field to lead
  with: when nearby sources don't agree on `type`, that is the evidence no
  stand-in was safe.
- **`notes[].kind` can now be `caveat`** — a limitation of the run rather than of
  any one source (`source` is `null`), e.g. `--precip iem:mrms` fitting across
  MRMS's pre-2014 backfill.
- Public: `PRECIP_PROVIDERS`, `resolve_precip()`, `precip_name()`,
  `resolve_radar()`, `RADAR_PROVIDER`. A provider may set `.precip_name` to declare
  which product it serves, so a host serving ERA5 from its own store still stamps
  payloads `open-meteo` and stays comparable with everyone else's.

### Changed

- **Sources with no usable reports now appear in `sources[]`** — with `n: 0` and
  every verdict-derived field `null` (`verdict`, `best`, `type`, `pct_dry`,
  `mean_flow`, `precip_in`, `predicted_flow`) — instead of being dropped from the
  payload and mentioned only in `notes`.

  **This is the one change that can break a consumer.** Anything iterating
  `sources` and reading `verdict` will now meet `null`. Branch on `n == 0`
  (equivalently `verdict === null`) first. Keys are never *absent*, so no consumer
  needs to test for their existence.
- A **deliberate pin no longer produces a `skip` note** — asking what rain alone
  can say is the feature working. A source that *lost* reports still gets one.
- A CSV row with exactly one of `date`/`score` blank is now rejected with a message
  naming the line, instead of failing somewhere inside parsing.
- The summary footer names the precip product when it isn't ERA5, and stops
  telling the reader to cross-check radar by hand once the radar check has run.

### Compatibility

A payload stored by 0.1.0 has no `params.precip` and no `params.radar`. Read an
absent `precip` as `"open-meteo"` and an absent `radar` as `"none"` — those were
the only possibilities at the time.

### Internal

- `tools/precip_bakeoff.py` now asks the engine for its products by name instead of
  carrying its own copy of the fetching; verified against the cache the original
  run left behind, **0 differing days out of 7,134**, so its recorded findings
  stand.
- The radar path does not retry (`RADAR_RETRIES = 1`). Retrying is right when the
  data is required and wrong for an optional second opinion, which was otherwise
  sleeping ~6s per source before dropping a line that was never going to appear.
- Test suite 131 → 231, still stdlib-only, offline and deterministic. The offline
  guarantee is now explicit for the radar seam: because the check swallows every
  exception by design, leaving its live provider enabled let the harness's
  "no network" raiser be caught and discarded.

## 0.1.0 — 2026-08-05

First tagged release: the engine as a pip-installable package with a declared
public surface, so embedders can pin a version instead of vendoring a copy.

Everything before this point is in the git history and the issue tracker. The
method-affecting work that landed on the way — season control, then pooling —
**moved verdicts twice** on identical input, which is why `params.engine_version`
exists and why this file does.
