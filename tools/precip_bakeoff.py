#!/usr/bin/env python3
"""
precip_bakeoff.py -- does changing the precipitation product change the answer?
==============================================================================
Runs the engine over the same sources under three precipitation products and
diffs what a user would actually see: the season-controlled correlations, the
best window, the type, and the verdict.

    python3 tools/precip_bakeoff.py examples/mazatzal-wilderness.csv --asof 2026-07-13
    python3 tools/precip_bakeoff.py area.csv --since 2014-01-01   # fair MRMS window
    python3 tools/precip_bakeoff.py area.csv --probe              # grid resolution only

Products:
  ERA5   Open-Meteo ERA5 archive -- the engine's default (global).
  PRISM  IEM IEMRE `prism_precip_in`  (CONUS only)
  MRMS   IEM IEMRE `mrms_precip_in`   (CONUS only; genuine only from ~2014,
         earlier values are a backfilled proxy -- use --since 2014-01-01 for a
         fair comparison)

THIS IS A RESEARCH TOOL, NOT PART OF THE ENGINE. It hits the network, it is not
covered by the test suite, and nothing in forecast.py imports it. It is stdlib-
only like everything else here, and it caches every fetch so a re-run is free
and IEM gets asked once per coordinate-year.

Findings from the first run (Mazatzal, 2026-08-05) are in tools/README.md and in
the comments on issues #17 and #21.
"""
import json, os, sys, time, urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import backcountry_water_oracle as forecast                          # noqa: E402

IEM_CACHE = os.path.join(forecast.CACHE_DIR, "iem")
PRODUCTS = ("ERA5", "PRISM", "MRMS")

# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def iem_year(lat, lon, year, start, end):
    """One coordinate-year of IEMRE daily data, cached on disk."""
    os.makedirs(IEM_CACHE, exist_ok=True)
    path = os.path.join(IEM_CACHE, f"{lat}_{lon}_{year}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    d1 = max(date(year, 1, 1), start).isoformat()
    d2 = min(date(year, 12, 31), end).isoformat()
    url = f"https://mesonet.agron.iastate.edu/iemre/multiday/{d1}/{d2}/{lat}/{lon}/json"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                data = json.load(r)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)
    rows = [{"date": row["date"], "prism": row.get("prism_precip_in"),
             "mrms": row.get("mrms_precip_in")} for row in data.get("data", [])]
    with open(path, "w") as f:
        json.dump(rows, f)
    time.sleep(0.5)                        # be a good citizen of a free service
    return rows

def iem_series(lat, lon, field, start, end):
    rows = []
    for year in range(start.year, end.year + 1):
        rows.extend(iem_year(lat, lon, year, start, end))
    return {"daily": {"time": [r["date"] for r in rows],
                      "precipitation_sum": [(r[field] or 0.0) for r in rows]}}

def era5_series(lat, lon, end):
    return forecast.open_meteo_provider(lat, lon, end)

def clip(series, since):
    if not since:
        return series
    t, v = series["daily"]["time"], series["daily"]["precipitation_sum"]
    i = next((k for k, x in enumerate(t) if x >= since), len(t))
    return {"daily": {"time": t[i:], "precipitation_sum": v[i:]}}

# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def h(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)

def grid_probe(lat, lon):
    """How far must you move before the SERVED values change? Answers 'what is the
    effective resolution of this endpoint', which is not the same question as
    'what is the native resolution of the product'."""
    h("GRID RESOLUTION PROBE -- how far apart must two points be to differ?")
    print(f"  probing east/west from ({lat}, {lon}), 5-day window\n")
    print(f"  {'offset':>8}{'~km':>7}{'iemre_i':>9}{'prism_i':>9}{'mrms_i':>8}   served data")
    prev = None
    for off in (-0.06, -0.04, -0.02, 0.0, 0.01, 0.02, 0.03, 0.04, 0.06, 0.10):
        url = (f"https://mesonet.agron.iastate.edu/iemre/multiday/"
               f"2024-07-01/2024-07-05/{lat}/{lon + off}/json")
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.load(r)
        vals = tuple(row.get("prism_precip_in") for row in d["data"])
        mark = "" if prev is None else ("  <-- CHANGED" if vals != prev else "  same")
        km = off * 111.19 * 0.83
        print(f"  {off:>8.2f}{km:>7.1f}{d['iemre_i']:>9}{d['prism_grid_i']:>9}"
              f"{d['mrms_iemre_grid_i']:>8}{mark}")
        prev = vals
        time.sleep(0.4)
    print("\n  If the served values only change when iemre_i changes, every product is")
    print("  being resampled onto the IEMRE grid (0.125 deg, ~11.6 km at 34N) and you")
    print("  are NOT getting PRISM's or MRMS's native resolution from this endpoint.")

def run_engine(sources, series_for, asof, product):
    forecast.PRECIP_PROVIDER = lambda lat, lon, end, use_cache=True: series_for(product, lat, lon)
    bases = [forecast.analyze_base(s, asof) for s in sources]
    bases = [b for b in bases if b and b["n"] > 0]
    forecast.pool_controlled(bases, forecast.POOL_RADIUS_KM)
    return {r["name"]: r for r in (forecast.finalize(b) for b in bases)}

def main(argv):
    if not argv or argv[0].startswith("--"):
        print(__doc__)
        return 1
    csv_path = argv[0]
    asof = date.today()
    since = None
    probe_only = "--probe" in argv
    for i, a in enumerate(argv):
        if a == "--asof":
            asof = date.fromisoformat(argv[i + 1])
        elif a.startswith("--asof="):
            asof = date.fromisoformat(a.split("=", 1)[1])
        elif a == "--since":
            since = argv[i + 1]
        elif a.startswith("--since="):
            since = a.split("=", 1)[1]

    sources = forecast.load_sources([csv_path])
    print(f"{len(sources)} source(s) from {csv_path}, as-of {asof}"
          + (f", precip clipped to {since}+" if since else ""))

    if probe_only:
        grid_probe(sources[0]["lat"], sources[0]["lon"])
        return 0

    start = date.fromisoformat(forecast.PRECIP_START)
    end = min(asof, date.today() - timedelta(days=forecast.ERA5_LAG_DAYS))
    cache = {}
    def series_for(product, lat, lon):
        key = (product, round(lat, 5), round(lon, 5))
        if key not in cache:
            if product == "ERA5":
                s = era5_series(lat, lon, end)
            else:
                s = iem_series(lat, lon, product.lower(), start, end)
            cache[key] = clip(s, since)
        return cache[key]

    print("fetching...", flush=True)
    for s in sources:
        for p in PRODUCTS:
            series_for(p, s["lat"], s["lon"])

    # -- are these sources even distinguishable to each product? ------------- #
    h("0. GRID RESOLUTION -- do these sources get distinct series?")
    for product in PRODUCTS:
        vecs = [tuple(series_for(product, s["lat"], s["lon"])["daily"]["precipitation_sum"])
                for s in sources]
        print(f"  {product:<6} {len(set(vecs))} distinct series across {len(sources)} source(s)"
              + ("   <-- all identical: one grid cell" if len(set(vecs)) == 1 and len(sources) > 1 else ""))

    # -- climatology --------------------------------------------------------- #
    h("1. CLIMATOLOGY (inches/yr)")
    print(f"  {'source':<26}{'product':<8}{'annual':>9}{'Jun-Sep':>9}{'wettest day':>13}")
    for s in sources:
        for product in PRODUCTS:
            d = series_for(product, s["lat"], s["lon"])["daily"]
            days = [date.fromisoformat(t) for t in d["time"]]
            vals = d["precipitation_sum"]
            yrs = max((days[-1] - days[0]).days / 365.25, 1e-9)
            wet = sum(v for dd, v in zip(days, vals) if 6 <= dd.month <= 9) / yrs
            print(f"  {s['name'][:24]:<26}{product:<8}{sum(vals)/yrs:>9.2f}{wet:>9.2f}"
                  f"{max(vals):>13.2f}")

    results = {p: run_engine(sources, series_for, asof, p) for p in PRODUCTS}

    # -- correlations -------------------------------------------------------- #
    h("2. SEASON-CONTROLLED r BY WINDOW (each source's own, not pooled)")
    for s in sources:
        name = s["name"]
        if name not in results["ERA5"]:
            continue
        print(f"\n  {name}")
        print(f"    {'window':<8}" + "".join(f"{p:>10}" for p in PRODUCTS))
        for w in forecast.WINDOWS:
            wn = f"{w}d"
            print(f"    {wn:<8}" + "".join(
                f"{dict(results[p][name]['ctrl_cors'])[wn]:>+10.3f}" for p in PRODUCTS))

    # -- what the user sees -------------------------------------------------- #
    h("3. WHAT THE USER SEES")
    for s in sources:
        name = s["name"]
        if name not in results["ERA5"]:
            continue
        print(f"\n  {name}")
        print(f"    {'product':<8}{'best':>6}{'r*':>8}{'borrow':>8}  {'type':<34}{'as-of':>7}  verdict")
        for p in PRODUCTS:
            r = results[p][name]
            print(f"    {p:<8}{r['best']:>6}{r['best_ctrl_r']:>+8.2f}"
                  f"{r['borrowed_at_best']*100:>7.0f}%  {r['type']:<34}"
                  f"{r['curval']:>7.2f}  {forecast.running_phrase(r['pred'])}")

    # -- the bottom line ----------------------------------------------------- #
    h("4. STABILITY -- what actually changed?")
    for s in sources:
        name = s["name"]
        if name not in results["ERA5"]:
            continue
        v = {p: forecast.running_phrase(results[p][name]["pred"]) for p in PRODUCTS}
        best = {p: results[p][name]["best"] for p in PRODUCTS}
        typ = {p: results[p][name]["type"] for p in PRODUCTS}
        print(f"  {name[:32]:<34} verdict {'same' if len(set(v.values()))==1 else 'CHANGED'}"
              f" | window {'same' if len(set(best.values()))==1 else 'CHANGED'}"
              f" | type {'same' if len(set(typ.values()))==1 else 'CHANGED'}")
        if len(set(v.values())) > 1:
            for p in PRODUCTS:
                print(f"      {p:<6} {v[p]:<42} (pred {results[p][name]['pred']:.3f},"
                      f" {results[p][name]['curval']:.2f}\" @ {results[p][name]['best']})")

    # -- where the products disagree most ------------------------------------ #
    h("5. BIGGEST SINGLE-DAY DISAGREEMENTS (Jun-Sep, first source)")
    s = sources[0]
    e = series_for("ERA5", s["lat"], s["lon"])["daily"]
    pr = series_for("PRISM", s["lat"], s["lon"])["daily"]
    mr = series_for("MRMS", s["lat"], s["lon"])["daily"]
    ei = {t: i for i, t in enumerate(e["time"])}
    pi = {t: i for i, t in enumerate(pr["time"])}
    rows = []
    for i, t in enumerate(mr["time"]):
        if t not in ei or t not in pi:
            continue
        d = date.fromisoformat(t)
        if not (6 <= d.month <= 9):
            continue
        ev = e["precipitation_sum"][ei[t]]
        rows.append((mr["precipitation_sum"][i] - ev, t, ev,
                     pr["precipitation_sum"][pi[t]], mr["precipitation_sum"][i]))
    rows.sort(reverse=True)
    print(f"  {'date':<12}{'ERA5':>8}{'PRISM':>8}{'MRMS':>8}    (sorted by MRMS-ERA5 gap)")
    for gap, t, ev, pv, mv in rows[:10]:
        print(f"  {t:<12}{ev:>8.2f}{pv:>8.2f}{mv:>8.2f}")
    print("\n  A large MRMS value where ERA5 and PRISM both read ~0 is a convective cell")
    print("  that only the radar product saw. Dates before ~2014 are MRMS backfill, not")
    print("  genuine radar -- re-run with --since 2014-01-01 to exclude them.")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
