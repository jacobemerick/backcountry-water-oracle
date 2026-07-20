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
    python3 forecast.py area.csv --pool-radius 15       # neighbor radius km (pooling)
    python3 forecast.py area.csv --no-pool              # analyze each source alone

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
  * POOLING: sources within --pool-radius km (default 25) borrow correlation
    strength from each other. Each source's per-window season-controlled r is
    shrunk toward its neighbors by empirical Bayes (posterior-mean between-source
    variance under a weakly-informative half-Cauchy prior): a group whose members
    agree pools hard, a group that disagrees barely pools, and a small-n source
    leans on neighbors more than a data-rich one does at every window. Geography
    only picks the neighborhood; the data decide how much to borrow. --no-pool
    turns it off. Only the correlation is pooled -- %-dry and the flow numbers
    stay each source's own.
"""
import csv, json, math, os, sys, urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
WINDOWS = [30, 60, 90, 180, 270, 365]
ERA5_LAG_DAYS = 6
PRECIP_START = "2007-01-01"
POOL_RADIUS_KM = 25.0   # default neighborhood radius for pooling (see pool_controlled)

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
# Pooling: let each source borrow correlation strength from nearby sources.
# Geography (haversine radius) only picks the neighborhood; how much a source
# actually borrows is decided by the data via empirical Bayes, so a group whose
# members disagree pools little and a coherent group pools hard. The engine only
# ever sees lat/lon + flow + precip -- it never assumes WHY two sources behave
# alike, only measures whether their rain responses are consistent.
# --------------------------------------------------------------------------- #
def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))

def _fisher_z(r):
    r = max(-0.999, min(0.999, r))     # keep atanh finite at |r|->1
    return math.atanh(r)

def _median(xs):
    ys = sorted(xs)
    m = len(ys) // 2
    return ys[m] if len(ys) % 2 else (ys[m - 1] + ys[m]) / 2.0

# Weakly-informative floor for the half-Cauchy prior scale on the between-source
# SD (Fisher-z units). ~0.25 in z is ~0.24 in r -- a modest "sources this close
# can plausibly differ this much" statement. It only keeps tau^2 from collapsing
# to exactly 0 (which would force all-or-nothing pooling with tiny groups); the
# data push the scale higher when neighbors genuinely disagree. Tune via PR.
POOL_PRIOR_SD_FLOOR = 0.25

def _pool_window(items):
    """Empirical-Bayes shrinkage of a group of correlations toward their group mean,
    on the Fisher-z scale. `items` is a list of (r, n) for the sources in one
    neighborhood at one rain window; returns a list of (pooled_r, borrowed_fraction)
    aligned to it.

    The between-source variance tau^2 is the knob that decides how much to pool: large
    tau^2 (neighbors disagree) -> everyone keeps their own estimate; small tau^2
    (neighbors agree) -> members are pulled to the mean, and within that a small-n
    member (larger sampling variance) is pulled more than a data-rich one. Nobody sets
    a shrinkage constant -- the data set it.

    Rather than a point estimate of tau^2 (DerSimonian-Laird), which with tiny groups
    is unstable and often truncates to exactly 0 (forcing 100% pooling and ignoring n),
    we put a weakly-informative half-Cauchy prior on the between-source SD and use the
    POSTERIOR MEAN of tau^2 over a 1-D grid of the REML marginal likelihood. That mean
    is always > 0, so pooling stays graded and n-dependent at every window, yet still
    grows large -- little pooling -- when the group is genuinely heterogeneous."""
    k = len(items)
    if k < 2:
        return [(items[0][0], 0.0)]
    z = [_fisher_z(r) for r, _ in items]
    s2 = [1.0 / max(n - 3, 1) for _, n in items]        # within-source var of z
    # Half-Cauchy prior scale A: data-driven (observed z-spread and typical sampling
    # SD), floored so a coherent group still can't collapse tau^2 to 0.
    zbar = sum(z) / k
    sd_z = (sum((zi - zbar) ** 2 for zi in z) / k) ** 0.5
    A = max(sd_z, math.sqrt(_median(s2)), POOL_PRIOR_SD_FLOOR)
    tau_hi = max(5 * A, 5 * sd_z, 1.0)                  # grid upper bound for tau
    G = 400
    lls, priors, t2s = [], [], []
    for g in range(G + 1):
        tau = tau_hi * g / G
        t2 = tau * tau
        w = [1.0 / (v + t2) for v in s2]
        sw = sum(w)
        mu = sum(wi * zi for wi, zi in zip(w, z)) / sw
        # REML marginal log-likelihood for the variance component (mu profiled out)
        ll = (-0.5 * sum(math.log(v + t2) for v in s2)
              - 0.5 * math.log(sw)
              - 0.5 * sum(wi * (zi - mu) ** 2 for wi, zi in zip(w, z)))
        lls.append(ll)
        priors.append(1.0 / (1.0 + (tau / A) ** 2))     # half-Cauchy(0, A) kernel
        t2s.append(t2)
    m = max(lls)                                         # log-sum-exp stabilization
    wts = [math.exp(ll - m) * pr for ll, pr in zip(lls, priors)]
    Z = sum(wts) or 1.0
    tau2 = sum(wt * t2 for wt, t2 in zip(wts, t2s)) / Z  # posterior mean of tau^2 (> 0)
    wr = [1.0 / (v + tau2) for v in s2]
    mu_re = sum(wi * zi for wi, zi in zip(wr, z)) / sum(wr)
    out = []
    for zi, v in zip(z, s2):
        own = tau2 / (tau2 + v)          # weight kept on the source's own estimate
        out.append((math.tanh(own * zi + (1 - own) * mu_re), 1 - own))
    return out

def pool_controlled(bases, radius_km):
    """In place: shrink every source's per-window season-controlled r toward the
    sources within radius_km (its neighborhood, itself included). Reads each source's
    own `ctrl` (never the pooled result), so processing order does not matter."""
    windows = [f"{w}d" for w in WINDOWS]
    for b in bases:
        grp = [o for o in bases
               if _haversine_km(b["lat"], b["lon"], o["lat"], o["lon"]) <= radius_km]
        i = grp.index(b)
        for wname in windows:
            pooled = _pool_window([(o["ctrl"][wname], o["n"]) for o in grp])
            b["pooled_ctrl"][wname], b["borrowed"][wname] = pooled[i]
        b["group_n"] = len(grp)

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

def analyze_base(src, asof, use_cache=True, n_harm=1):
    """Per-source pass: precip -> raw + season-controlled per-window r vectors, plus
    the machinery finalize() needs. Stops BEFORE choosing a best window, so pooling
    can adjust the controlled r first. `pooled_ctrl` starts equal to the source's own
    `ctrl` (i.e. no neighbors); pool_controlled() overwrites it when pooling runs."""
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
    raw = {name: spearman(xs, y) for name, xs in feats.items()}
    # season-controlled correlations (day-of-year cycle removed) -- the trustworthy
    # numbers. Classification, best-predictor, and the analog read all key off these
    # (pooled if pooling is on), never the season-flattered raw r.
    ctrl = {name: season_controlled_r(dates, y, xs, n_harm) for name, xs in feats.items()}
    bym = {}
    for d, s in recs:
        bym.setdefault(d.month, []).append(s)
    return {"name": src["name"], "lat": src["lat"], "lon": src["lon"],
            "n": n, "pct_dry": pct_dry, "mean": sum(y) / n,
            "annual_precip": idx["annual"], "raw": raw, "ctrl": ctrl,
            "pooled_ctrl": dict(ctrl), "borrowed": {k: 0.0 for k in ctrl},
            "group_n": 1, "n_harm": n_harm,
            "idx": idx, "recs": recs, "asof_req": asof,
            "by_month": {m: sum(v) / len(v) for m, v in sorted(bym.items())}}

def finalize(b):
    """Turn a (possibly pooled) base into the presentation dict: pick the best window
    from the pooled controlled r, then read classification + nearest-analog off it.
    The per-window table still shows the source's OWN raw/controlled r for honesty;
    only the >> best-predictor decision and verdict use the pooled value."""
    ctrl_cors = sorted(b["pooled_ctrl"].items(), key=lambda t: -abs(t[1]))
    best, best_ctrl_r = ctrl_cors[0]
    best_w = int(best[:-1])
    idx, recs = b["idx"], b["recs"]
    asof_eff = min(b["asof_req"], idx["last"])
    curval = window_sum(idx, asof_eff, best_w)
    hist = sorted(((window_sum(idx, d, best_w), s) for d, s in recs),
                  key=lambda t: abs(t[0] - curval))[:5]
    pred = sum(s for _, s in hist) / len(hist)
    return {"name": b["name"], "lat": b["lat"], "lon": b["lon"],
            "n": b["n"], "pct_dry": b["pct_dry"], "mean": b["mean"],
            "annual_precip": b["annual_precip"],
            "cors": sorted(((r, name) for name, r in b["raw"].items()), key=lambda t: -abs(t[0])),
            "ctrl_cors": sorted(b["ctrl"].items(), key=lambda t: -abs(t[1])),   # OWN, for the table
            "best": best, "best_ctrl_r": best_ctrl_r,                           # POOLED, drives verdict
            "best_raw_r": b["raw"][best], "best_own_ctrl_r": b["ctrl"][best],
            "borrowed_at_best": b["borrowed"][best], "group_n": b["group_n"],
            "type": classify(b["pct_dry"], best_w),
            "curval": curval, "pred": pred, "asof": asof_eff, "n_harm": b["n_harm"],
            "by_month": b["by_month"]}

def analyze(src, asof, use_cache=True, n_harm=1):
    """Single-source convenience (no pooling): base pass then finalize."""
    b = analyze_base(src, asof, use_cache, n_harm)
    return None if b is None else finalize(b)

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
    if a["group_n"] > 1:
        print(f"  >> best predictor (pooled, season-controlled): {a['best']} rain  "
              f"r={a['best_ctrl_r']:+.2f}")
        print(f"     borrowed {a['borrowed_at_best']*100:.0f}% from {a['group_n']-1} "
              f"neighbor(s) in range; this source's own r={a['best_own_ctrl_r']:+.2f} "
              f"(raw {a['best_raw_r']:+.2f}, k={a['n_harm']} harmonic)")
        print(f"     signal check (own data): {survival_note(a['best_raw_r'], a['best_own_ctrl_r'])}")
    else:
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
    h = f"{'SOURCE':<26}{'N':>4}{'%DRY':>6}{'BEST':>7}{'r*':>7}{'POOL':>6}   {'AS-OF READ'}"
    print(h); print("-" * len(h))
    for a in rows:
        nm = (a["name"][:24] + "..") if len(a["name"]) > 26 else a["name"]
        pool = (f"{a['borrowed_at_best']*100:.0f}%" if a["group_n"] > 1 else "-").rjust(6)
        print(f"{nm:<26}{a['n']:>4}{a['pct_dry']:>5}%{a['best']:>7}{a['best_ctrl_r']:>+7.2f}{pool}   "
              f"{running_phrase(a['pred'])}")
    print("\nr* = season-controlled Spearman (day-of-year removed), POOLED toward nearby")
    print("sources where they agree -- the trustworthy one. POOL = % of r* borrowed from")
    print("neighbors ('-' = none in range; --no-pool disables pooling entirely).")
    print("Type key: <=10% dry = buffered/reliable; flashy = short-window + often dry.")
    print("Reminder: ERA5 misses monsoon cells -- for a summer go/no-go, cross-check radar (MRMS/AHPS).")

_VALUE_FLAGS = ("--asof", "--harmonics", "--pool-radius")
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
    do_pool = "--no-pool" not in argv
    n_harm = 1
    radius_km = POOL_RADIUS_KM
    for i, a in enumerate(argv):
        if a == "--asof":
            asof = date.fromisoformat(argv[i + 1])
        elif a.startswith("--asof="):
            asof = date.fromisoformat(a.split("=", 1)[1])
        elif a == "--harmonics":
            n_harm = int(argv[i + 1])
        elif a.startswith("--harmonics="):
            n_harm = int(a.split("=", 1)[1])
        elif a == "--pool-radius":
            radius_km = float(argv[i + 1])
        elif a.startswith("--pool-radius="):
            radius_km = float(a.split("=", 1)[1])
    if not files:
        print(__doc__); return 1
    try:
        sources = load_sources(files)
    except Exception as e:
        print(f"[error] {e}"); return 2
    # Pass 1: per-source base (fetch precip + per-window r vectors), no window chosen yet.
    bases = []
    for src in sources:
        try:
            b = analyze_base(src, asof, use_cache, n_harm)
            if b is None:
                print(f"[skip] {src['name']}: no reports within precip range"); continue
            bases.append(b)
        except Exception as e:
            print(f"[error] {src['name']}: {e}")
    # Pass 2: pool the season-controlled r across nearby sources (needs >=2 to borrow).
    if do_pool and len(bases) > 1:
        pool_controlled(bases, radius_km)
    # Pass 3: finalize (best window / class / analog read off the pooled r) + report.
    rows = [finalize(b) for b in bases]
    for a in rows:
        print_source(a)
    if len(rows) > 1:
        print_table(rows)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
