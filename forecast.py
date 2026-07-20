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
  * Season and rain are entangled (more reports in wet, cool months). The tool
    removes the day-of-year cycle (annual-harmonic regression, learned from each
    site's own precip so it's hemisphere-correct anywhere) and reports a
    SEASON-CONTROLLED r beside each raw r. Classification, the best-predictor,
    and the analog read all key off the controlled r. It can collapse a small-n
    source's headline (e.g. Castersen's raw .72/180d -> ~.09 controlled).
  * Small-n sources (< ~25 reports) are suggestive, not solid (flagged below).
    Borrowing strength from data-rich neighbors is the next planned feature.
"""
import csv, json, math, os, sys, urllib.request
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
# Season control: remove the day-of-year cycle (learned from the data, so it is
# hemisphere-/climate-correct anywhere) via annual-harmonic regression, then
# correlate the residuals -> the rain signal net of the calendar.
# --------------------------------------------------------------------------- #
def _solve(A, b):
    """Solve A x = b for a small dense system (Gaussian elimination w/ pivot)."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        piv = M[c][c]
        M[c] = [v / piv for v in M[c]]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c]
                M[r] = [M[r][k] - f * M[c][k] for k in range(n + 1)]
    return [M[i][n] for i in range(n)]

def deseasonalize(dates, values, n_harm=1):
    """Return values with the annual day-of-year cycle regressed out (residuals).
    Falls back to mean-removal if there aren't enough points to fit the harmonics."""
    n = len(values)
    p = 2 * n_harm + 1
    if n < p + 2:                              # too few points -> just de-mean
        m = sum(values) / n
        return [v - m for v in values]
    def basis(d):
        t = d.timetuple().tm_yday / 365.25
        row = [1.0]
        for k in range(1, n_harm + 1):
            row += [math.sin(2 * math.pi * k * t), math.cos(2 * math.pi * k * t)]
        return row
    X = [basis(d) for d in dates]
    # normal equations: (X^T X) beta = X^T y
    XtX = [[sum(X[r][i] * X[r][j] for r in range(n)) for j in range(p)] for i in range(p)]
    Xty = [sum(X[r][i] * values[r] for r in range(n)) for i in range(p)]
    beta = _solve(XtX, Xty)
    if beta is None:
        m = sum(values) / n
        return [v - m for v in values]
    return [values[r] - sum(X[r][i] * beta[i] for i in range(p)) for r in range(n)]

def season_controlled_r(dates, flow, window_vals, n_harm=1):
    return spearman(deseasonalize(dates, flow, n_harm),
                    deseasonalize(dates, window_vals, n_harm))

def survival_note(r_raw, r_ctrl):
    if abs(r_raw) < 0.05:
        return "no raw signal to test"
    frac = abs(r_ctrl) / abs(r_raw)
    if frac >= 0.70: return "survives -> genuine rain response"
    if frac >= 0.40: return "partly seasonal, real signal remains"
    return "mostly a seasonal artifact -> weak true rain signal"

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

def analyze(src, asof, use_cache=True, n_harm=1):
    data = fetch_precip(src["lat"], src["lon"],
                        min(asof, date.today() - timedelta(days=ERA5_LAG_DAYS)), use_cache)
    idx = build_precip_index(data)
    recs = [(d, s) for d, s in src["reports"] if idx["first"] < d <= idx["last"]]
    dates = [d for d, _ in recs]
    y = [s for _, s in recs]
    n = len(y)
    if n == 0:
        return None
    pct_dry = round(100 * sum(1 for v in y if v == 0) / n)
    feats = {f"{w}d": [window_sum(idx, d, w) for d, _ in recs] for w in WINDOWS}
    cors = sorted(((spearman(xs, y), name) for name, xs in feats.items()),
                  key=lambda t: -abs(t[0]))
    raw = {name: r for r, name in cors}
    # season-controlled correlations (day-of-year cycle removed) -- THE HEADLINE:
    # classification + best-predictor + the analog read all key off these, so we
    # lead with the trustworthy number, not the season-flattered raw one.
    ctrl = {name: season_controlled_r(dates, y, xs, n_harm) for name, xs in feats.items()}
    ctrl_cors = sorted(ctrl.items(), key=lambda t: -abs(t[1]))
    best, best_ctrl_r = ctrl_cors[0]
    best_w = int(best[:-1])
    best_raw_r = raw[best]
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
            "best_ctrl_r": best_ctrl_r, "best_raw_r": best_raw_r,
            "type": classify(pct_dry, best_w),
            "curval": curval, "pred": pred, "asof": asof_eff, "n_harm": n_harm,
            "ctrl_cors": ctrl_cors,
            "by_month": {m: sum(v) / len(v) for m, v in sorted(bym.items())}}

def print_source(a):
    print(f"\n{'='*74}\n{a['name']}   ({a['lat']:.5f}, {a['lon']:.5f})")
    print(f"  reports: {a['n']}   |   {a['pct_dry']}% ever dry   |   "
          f"mean flow {a['mean']:.2f} (0-1)   |   ~{a['annual_precip']:.0f}\"/yr")
    print(f"  TYPE: {a['type']}")
    mo = "  ".join(f"{m:02d}:{v:.2f}" for m, v in a["by_month"].items())
    print(f"  mean flow by month:  {mo}")
    print("  rain-window correlation (Spearman, strongest first):")
    ctrl = dict(a["ctrl_cors"])
    for r, name in a["cors"]:
        print(f"     {name:5s} raw r={r:+.2f}  |  season-ctrl r={ctrl[name]:+.2f}  "
              f"{'#' * int(round(abs(ctrl[name]) * 20))}")
    print(f"  >> best predictor (season-controlled): {a['best']} rain  "
          f"r={a['best_ctrl_r']:+.2f}  (raw r={a['best_raw_r']:+.2f}, k={a['n_harm']} harmonic)")
    print(f"     signal check: {survival_note(a['best_raw_r'], a['best_ctrl_r'])}")
    print(f"\n  AS OF {a['asof']}:  {a['best']} rain = {a['curval']:.2f}\"  ->  "
          f"nearest-analog flow ~{a['pred']:.2f} (0-1)")
    print(f"  VERDICT: {running_phrase(a['pred'])}"
          + ("   [small n - weak confidence]" if a['n'] < 25 else ""))

def print_table(rows):
    print(f"\n{'='*74}\nSUMMARY  (most reliable first)\n")
    rows = sorted(rows, key=lambda a: a["pct_dry"])
    h = f"{'SOURCE':<26}{'N':>4}{'%DRY':>6}{'BEST':>7}{'r*':>7}   {'AS-OF READ'}"
    print(h); print("-" * len(h))
    for a in rows:
        nm = (a["name"][:24] + "..") if len(a["name"]) > 26 else a["name"]
        print(f"{nm:<26}{a['n']:>4}{a['pct_dry']:>5}%{a['best']:>7}{a['best_ctrl_r']:>+7.2f}   "
              f"{running_phrase(a['pred'])}")
    print("\nr* = season-controlled Spearman (day-of-year removed) -- the trustworthy one.")
    print("Type key: <=10% dry = buffered/reliable; flashy = short-window + often dry.")
    print("Reminder: ERA5 misses monsoon cells -- for a summer go/no-go, cross-check radar (MRMS/AHPS).")

_VALUE_FLAGS = ("--asof", "--harmonics")
def _is_flag_value(argv, a):
    """True if `a` is the space-separated value following a value-taking flag."""
    for i, x in enumerate(argv):
        if x in _VALUE_FLAGS and i + 1 < len(argv) and argv[i + 1] is a:
            return True
    return False

def main(argv):
    files = [a for a in argv if not a.startswith("--") and not _is_flag_value(argv, a)]
    asof = date.today()
    use_cache = "--no-cache" not in argv
    n_harm = 1
    for i, a in enumerate(argv):
        if a == "--asof":
            asof = date.fromisoformat(argv[i + 1])
        elif a.startswith("--asof="):
            asof = date.fromisoformat(a.split("=", 1)[1])
        elif a == "--harmonics":
            n_harm = int(argv[i + 1])
        elif a.startswith("--harmonics="):
            n_harm = int(a.split("=", 1)[1])
    if not files:
        print(__doc__); return 1
    try:
        sources = load_sources(files)
    except Exception as e:
        print(f"[error] {e}"); return 2
    rows = []
    for src in sources:
        try:
            a = analyze(src, asof, use_cache, n_harm)
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
