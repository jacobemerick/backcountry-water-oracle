#!/usr/bin/env python3
"""Regenerate the golden payload used by TestGolden.

    python3 tests/make_golden.py

Run this ONLY when you have deliberately changed what the engine outputs, and
read the resulting `git diff` before committing it -- the whole point of the
golden test is that unintended numeric drift shows up as a failure, and blindly
regenerating turns that alarm off.

It builds the payload exactly as the test does: the committed precip fixture in
through PRECIP_PROVIDER, no network, an explicit as-of date.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
from test_forecast import ASOF, EXAMPLE_CSV, FIXTURES, fixture_provider, run_cli  # noqa: E402
import backcountry_water_oracle as forecast                                       # noqa: E402

forecast.PRECIP_PROVIDER = fixture_provider
code, out, err = run_cli([EXAMPLE_CSV, "--asof", ASOF.isoformat(), "--json"])
if code != 0:
    sys.exit(f"engine exited {code}: {err}")

payload = json.loads(out)
dest = os.path.join(FIXTURES, "golden-mazatzal.json")
with open(dest, "w") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")

print(f"wrote {dest}")
for s in payload["sources"]:
    b = s["best"]
    print(f"  {s['name'][:34]:<36} n={s['n']:<4} {b['window']:>5} "
          f"r={b['r']:+.4f} borrowed={b['borrowed']:.4f} -> {s['verdict']}")
