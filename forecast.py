#!/usr/bin/env python3
"""
backcountry-water-oracle -- forecast engine
===========================================
Estimate whether a backcountry water source (seep / spring / creek / falls) is
likely to be running, by correlating its historical field observations against
~19 years of daily precipitation for its coordinates.

This is the deterministic ENGINE. It is format-agnostic: it consumes a single
normalized CSV and knows nothing about any particular report website. Turning
messy real-world reports (hikeArizona, FarOut comments, trail spreadsheets, your
own notes) into that CSV is the job of the `/water-forecast` skill (or you, by
hand). See README.md for the schema and the skill.

Input CSV schema (header required; extra columns ignored):
    source,lat,lon,date,score[,status]
      source  free-text name/id (rows with the same name are one source)
      lat,lon decimal degrees
      date    ISO YYYY-MM-DD
      score   FLOAT 0.0 .. 1.0   (0.0 = dry, 1.0 = max observed flow)
      status  optional raw text, carried for provenance only

Usage:
    python3 forecast.py examples/mazatzal-wilderness.csv
    python3 forecast.py a.csv b.csv                     # combine several files
    python3 forecast.py sources.csv --asof 2026-08-15   # read for a future date
    python3 forecast.py sources.csv --no-cache          # force precip re-fetch

Pure standard library. No pip installs. Precip: Open-Meteo ERA5 archive (free).

TRUST IT THIS MUCH:
  * ERA5 precip is ~9-11 km grid -> it SMOOTHS/MISSES isolated monsoon cells.
    Trust the winter-recharge signal; for a summer "did a storm just hit" call,
    still eyeball radar (MRMS / AHPS). This gives the base rate, not this week.
  * Season and rain are entangled (more reports in wet, cool months) -> v1's raw
    correlations are somewhat flattered. Day-of-year control is a TODO.
  * Small-n sources (< ~25 reports) are suggestive, not solid (flagged below).
    Borrowing strength from data-rich neighbors is the next planned feature.
"""
import csv, json, os, sys, urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
WINDOWS = [30, 60, 90, 180, 270, 365]
ERA5_LAG_DAYS = 6
PRECIP_START = "2007-01-01"

# --------------------------------------------------------------------------- #
# Input: normalized CSV -> sources
# --------------------------------------------------------------------------- #
def load_sources(paths):
    """Return list of {name, lat, lon, reports=[(date, score)]}."""
    sources = {}
    for p in paths:
        with open(p, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            need = {"source", "lat", "lon", "date", "score"}
            missing = need - set(h.strip() for h in (reader.fieldnames or []))
            if missing:
                raise ValueError(f"{p}: CSV missing column(s): {', '.join(sorted(missing))}")
            for row in reader:
                name = row["source"].strip()
                if not name:
                    continue
                s = sources.setdefault(name, {"name": name,
                                              "lat": float(row["lat"]),
                                              "lon": float(row["lon"]),
                                              "reports": []})
                sc = max(0.0, min(1.0, float(row["score"])))
                s["reports"].append((date.fromisoformat(row["date"].strip()[:10]), sc))
    for s in sources.values():
        s["reports"].sort()
    return list(sources.values())

# --------------------------------------------------------------------------- #
# Precipitation (Open-Meteo ERA5 archive, cached per rounded coordinate)
# --------------------------------------------------------------------------- #
def fetch_precip(lat, lon, end_date, use_cache=True):
    key = f"{round(lat,2)}_{round(lon,2)}_{end_date.isoformat()}.json"
    cpath = os.path.join(CACHE_DIR, key)
    if use_cache and os.path.exists(cpath):
        return json.load(open(cpath))
    url = ("https://archive-api.open-meteo.com/v1/archive"
           f"?latitude={lat:.4f}&longitude={lon:.4f}"
           f"&start_date={PRECIP_START}&end_date={end_date.isoformat()}"
           "&daily=precipitation_sum&precipitation_unit=inch"
           "&timezone=America%2FPhoenix")
    with urllib.request.urlopen(url, timeout=120) as r:
        data = json.load(r)
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump(data, open(cpath, "w"))
    return data

def build_precip_index(data):
    days = [date.fromisoformat(t) for t in data["daily"]["time"]]
    vals = [(v or 0.0) for v in data["daily"]["precipitation_sum"]]
    prefix, run = {}, 0.0
    for d, v in zip(days, vals):
        run += v
        prefix[d] = run
    return {"days": days, "first": days[0], "last": days[-1], "prefix": prefix,
            "annual": sum(vals) / max(1, (days[-1] - days[0]).days / 365.25)}

def window_sum(idx, end, w):
    end = min(end, idx["last"])
    start = end - timedelta(days=w)
    hi = idx["prefix"].get(end, idx["prefix"][idx["last"]])
    lo = idx["prefix"].get(start)
    if lo is None:
        lo = 0.0 if start < idx["first"] else idx["prefix"][idx["last"]]
    return hi - lo

# --------------------------------------------------------------------------- #
# Stats: Spearman = Pearson on average ranks (stdlib only)
# --------------------------------------------------------------------------- #
def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r

def _pearson(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((x - mb) ** 2 for x in b) ** 0.5
    return num / (da * db) if da and db else 0.0

def spearman(a, b):
    return _pearson(_ranks(a), _ranks(b))

# --------------------------------------------------------------------------- #
# Analysis + reporting (scores are 0.0 .. 1.0)
# --------------------------------------------------------------------------- #
def classify(pct_dry, best_window):
    if pct_dry <= 10:
        return "Reliable (groundwater-buffered)"
    if best_window <= 90:
        return "Flashy (needs recent rain)"
    return "Intermediate"

def running_phrase(pred):
    if pred < 0.10: return "Likely DRY"
    if pred < 0.30: return "Marginal - pools/dripping at best"
    if pred < 0.50: return "Probably has water (light flow / pools)"
    if pred < 0.70: return "Likely flowing (moderate)"
    return "Likely flowing well"

def analyze(src, asof, use_cache=True):
    data = fetch_precip(src["lat"], src["lon"],
                        min(asof, date.today() - timedelta(days=ERA5_LAG_DAYS)), use_cache)
    idx = build_precip_index(data)
    recs = [(d, s) for d, s in src["reports"] if idx["first"] < d <= idx["last"]]
    y = [s for _, s in recs]
    n = len(y)
    if n == 0:
        return None
    pct_dry = round(100 * sum(1 for v in y if v == 0) / n)
    feats = {f"{w}d": [window_sum(idx, d, w) for d, _ in recs] for w in WINDOWS}
    cors = sorted(((spearman(xs, y), name) for name, xs in feats.items()),
                  key=lambda t: -abs(t[0]))
    best_r, best = cors[0]
    best_w = int(best[:-1])
    asof_eff = min(asof, idx["last"])
    curval = window_sum(idx, asof_eff, best_w)
    hist = sorted(((window_sum(idx, d, best_w), s) for d, s in recs),
                  key=lambda t: abs(t[0] - curval))[:5]
    pred = sum(s for _, s in hist) / len(hist)
    bym = {}
    for d, s in recs:
        bym.setdefault(d.month, []).append(s)
    return {"name": src["name"], "lat": src["lat"], "lon": src["lon"],
            "n": n, "pct_dry": pct_dry, "mean": sum(y) / n,
            "annual_precip": idx["annual"], "cors": cors, "best": best,
            "best_r": best_r, "type": classify(pct_dry, best_w),
            "curval": curval, "pred": pred, "asof": asof_eff,
            "by_month": {m: sum(v) / len(v) for m, v in sorted(bym.items())}}

def print_source(a):
    print(f"\n{'='*74}\n{a['name']}   ({a['lat']:.5f}, {a['lon']:.5f})")
    print(f"  reports: {a['n']}   |   {a['pct_dry']}% ever dry   |   "
          f"mean flow {a['mean']:.2f} (0-1)   |   ~{a['annual_precip']:.0f}\"/yr")
    print(f"  TYPE: {a['type']}")
    mo = "  ".join(f"{m:02d}:{v:.2f}" for m, v in a["by_month"].items())
    print(f"  mean flow by month:  {mo}")
    print("  rain-window correlation (Spearman, strongest first):")
    for r, name in a["cors"]:
        print(f"     {name:5s} r={r:+.2f}  {'#' * int(round(abs(r) * 20))}")
    print(f"  >> best predictor: {a['best']} antecedent rain (r={a['best_r']:+.2f})")
    print(f"\n  AS OF {a['asof']}:  {a['best']} rain = {a['curval']:.2f}\"  ->  "
          f"nearest-analog flow ~{a['pred']:.2f} (0-1)")
    print(f"  VERDICT: {running_phrase(a['pred'])}"
          + ("   [small n - weak confidence]" if a['n'] < 25 else ""))

def print_table(rows):
    print(f"\n{'='*74}\nSUMMARY  (most reliable first)\n")
    rows = sorted(rows, key=lambda a: a["pct_dry"])
    h = f"{'SOURCE':<26}{'N':>4}{'%DRY':>6}{'BEST':>7}{'r':>7}   {'AS-OF READ'}"
    print(h); print("-" * len(h))
    for a in rows:
        nm = (a["name"][:24] + "..") if len(a["name"]) > 26 else a["name"]
        print(f"{nm:<26}{a['n']:>4}{a['pct_dry']:>5}%{a['best']:>7}{a['best_r']:>+7.2f}   "
              f"{running_phrase(a['pred'])}")
    print("\nType key: <=10% dry = buffered/reliable; flashy = short-window + often dry.")
    print("Reminder: ERA5 misses monsoon cells -- for a summer go/no-go, cross-check radar (MRMS/AHPS).")

def main(argv):
    files = [a for a in argv if not a.startswith("--")]
    asof = date.today()
    use_cache = "--no-cache" not in argv
    for i, a in enumerate(argv):
        if a == "--asof":
            asof = date.fromisoformat(argv[i + 1])
        elif a.startswith("--asof="):
            asof = date.fromisoformat(a.split("=", 1)[1])
    if not files:
        print(__doc__); return 1
    try:
        sources = load_sources(files)
    except Exception as e:
        print(f"[error] {e}"); return 2
    rows = []
    for src in sources:
        try:
            a = analyze(src, asof, use_cache)
            if a is None:
                print(f"[skip] {src['name']}: no reports within precip range"); continue
            print_source(a)
            rows.append(a)
        except Exception as e:
            print(f"[error] {src['name']}: {e}")
    if len(rows) > 1:
        print_table(rows)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
