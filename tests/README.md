# Tests

```bash
python3 tests/test_forecast.py            # or: python3 -m unittest discover tests -t .
```

No dependencies, no config, no network. ~1 second.

## The three rules

- **Stdlib only** — `unittest`, the same dependency-free rule the engine holds to.
- **Offline** — no test may touch the network. Live calls would be slow, rude to a
  free service, and ERA5 revisions would make the numbers flap. Tests that need
  precipitation get it from a committed fixture through `PRECIP_PROVIDER`, and
  `urlopen` is replaced with something that raises, so "did it fetch?" is never
  ambiguous.
- **Deterministic** — every test that reads an as-of date passes one explicitly.
  Nothing here may depend on today's date.

The CLI is exercised by calling `main()` in-process and capturing stdout/stderr
and the return code, rather than by spawning processes: it is much faster, and
it is the only way to keep the precip provider injected. One subprocess test
covers "does this still run as a script at all".

## Fixtures

- `fixtures/precip/{lat}_{lon}.json` — real ERA5 daily series for the three
  Mazatzal coordinates, stored as a start date plus the values (the dates are
  contiguous, so re-deriving them costs nothing and saves ~2/3 of the size).
  They run to `2026-07-30`, past the as-of the tests use, so the engine's trim
  path is always in play.
- `fixtures/golden-mazatzal.json` — the full `--json` payload for the worked
  example.

## The golden test

`TestGolden` compares the entire payload against `golden-mazatzal.json`. It is
the test that catches silent numeric drift from a refactor — the failure mode
nobody can eyeball, in a tool whose wrong answers look exactly as plausible as
its right ones.

If it fails, **read the diff before doing anything else.** Then either the change
was unintended (fix it) or it was intended, in which case:

```bash
python3 tests/make_golden.py
```

Regenerating without reading the diff turns the alarm off. That is the only way
this test can fail to do its job.

`TestGolden.test_the_documented_headline_numbers` additionally spells out the
numbers that appear in the README and the PR history — `n`, best windows, the
type spectrum, the borrowing order, Castersen's season-control collapse. Those
are quoted in prose elsewhere, so a change there is a documentation change too.

## Mutation-checked

The suite has been verified to fail when the engine is broken on purpose: the
cache key regaining its end date (#6), coordinate conflicts being ignored (#9),
report accounting losing its counts (#10), the missing bounds check on flag
values (#11), ranks no longer averaging ties, season control silently disabled,
pooling reading neighbours' pooled results instead of their own, shrinkage
ignoring sample size, a renamed JSON field, the analog pool changing size, and a
nudged verdict threshold. All twelve were caught.

Worth repeating when adding tests here: a green suite that has never been shown
to fail proves nothing.
