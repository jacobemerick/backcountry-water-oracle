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
    cat area.csv | python3 forecast.py -                # read the CSV from stdin
    python3 forecast.py area.csv --json                 # machine-readable output

EMBEDDING IT (hosts):
    import forecast
    forecast.PRECIP_PROVIDER = my_provider   # (lat, lon, end_date, use_cache)
    rows = [forecast.analyze(s, asof) for s in forecast.load_sources(["x.csv"])]
  The precip backend is the one seam a host needs: swap it to read the series
  from a shared store (Postgres/KV) instead of this script's .cache/ directory.
  Still stdlib-only, still no change to the CLI. See PRECIP_PROVIDER below.

PIPING IT AROUND:
  * `-` as a filename reads the CSV from stdin (and stdin is used automatically
    when no files are given and stdin is not a terminal), so the engine drops
    into a pipeline: your normalizer | forecast.py - --json | your consumer.
  * `--json` prints one JSON object on stdout instead of the text report -- every
    number the text report shows, unrounded-to-4dp, plus the run's parameters.
    In --json mode all diagnostics ([skip]/[error]) go to stderr and are also
    collected under "notes", so stdout stays valid JSON.

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
import bisect, csv, json, math, os, sys, urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
WINDOWS = [30, 60, 90, 180, 270, 365]
ERA5_LAG_DAYS = 6
PRECIP_START = "2007-01-01"
POOL_RADIUS_KM = 25.0   # default neighborhood radius for pooling (see pool_controlled)
# How far two rows sharing a source name may sit apart before that is treated as a
# name collision rather than GPS scatter (see _read_csv). Sub-kilometre differences
# cannot change any number the engine produces -- ERA5's grid is ~9-11 km and the
# precip cache rounds coordinates to 2dp (~1.1 km), so both rows resolve to the same
# cell and the same cached series. Past that, it is likely two different sources
# wearing the same name, and the engine would silently correlate one against the
# other's weather.
COORD_TOLERANCE_KM = 1.0

# --------------------------------------------------------------------------- #
# Input: normalized CSV -> sources
# --------------------------------------------------------------------------- #
def _read_csv(f, label, sources):
    """Accumulate one open CSV stream into `sources` (keyed by source name)."""
    reader = csv.DictReader(f)
    if reader.fieldnames is None:
        raise ValueError(f"{label}: empty input (no CSV header)")
    need = {"source", "lat", "lon", "date", "score"}
    missing = need - set(h.strip() for h in reader.fieldnames)
    if missing:
        raise ValueError(f"{label}: CSV missing column(s): {', '.join(sorted(missing))}")
    for row in reader:
        name = row["source"].strip()
        if not name:
            continue
        lat, lon = float(row["lat"]), float(row["lon"])
        s = sources.get(name)
        if s is None:
            s = sources[name] = {"name": name, "lat": lat, "lon": lon, "reports": []}
        else:
            # Rows sharing a name are one source, so the first row's coordinates used
            # to win silently -- two different springs with the same name would get
            # correlated against ONE of their weather records and look entirely
            # normal. Close enough is still fine (GPS scatter); far apart is a bug in
            # the data that only the author can resolve.
            off_by = _haversine_km(s["lat"], s["lon"], lat, lon)
            if off_by > COORD_TOLERANCE_KM:
                raise ValueError(
                    f"{label} line {reader.line_num}: source {name!r} has conflicting "
                    f"coordinates -- ({s['lat']:.5f}, {s['lon']:.5f}) and "
                    f"({lat:.5f}, {lon:.5f}), {off_by:,.1f} km apart. Rows sharing a "
                    "name are treated as one source: rename one of them, or fix the "
                    "coordinates.")
        sc = max(0.0, min(1.0, float(row["score"])))
        s["reports"].append((date.fromisoformat(row["date"].strip()[:10]), sc))

def load_sources(paths):
    """Return list of {name, lat, lon, reports=[(date, score)]}.
    A path of "-" means stdin, so the engine can sit in a pipeline; stdin can only
    be consumed once, so a repeated "-" is ignored after the first."""
    sources, stdin_done = {}, False
    for p in paths:
        if p == "-":
            if stdin_done:
                continue
            stdin_done = True
            _read_csv(sys.stdin, "<stdin>", sources)
        else:
            with open(p, newline="", encoding="utf-8") as f:
                _read_csv(f, p, sources)
    for s in sources.values():
        s["reports"].sort()
    return list(sources.values())

# --------------------------------------------------------------------------- #
# Precipitation: a swappable provider
# ---------------------------------------------------------------------------
# Everything downstream needs is one daily series per coordinate:
#
#     provider(lat, lon, end_date, use_cache) -> {"daily": {"time": [ISO...],
#                                                 "precipitation_sum": [inches...]}}
#
# The default provider fetches Open-Meteo's ERA5 archive and caches it under
# CACHE_DIR. An embedding host (a web app on serverless, say) that needs the
# series in a shared store instead can assign its own callable:
#
#     import forecast
#     forecast.PRECIP_PROVIDER = my_postgres_provider   # same signature
#
# ...and the engine will use it everywhere, with no new dependency and no change
# to the CLI. A host that only wants different STORAGE can wrap the built-in:
# call open_meteo_provider() on a miss and keep the result wherever it likes.
# CACHE_DIR itself is assignable too, for the simple "just put it elsewhere" case.
#
# Contract notes:
#   * `time` must be ascending ISO dates; `precipitation_sum` inches, same length.
#     None is allowed and read as 0.0. The series should start at PRECIP_START --
#     a short one silently shortens the analog pool it can draw on.
#   * `end_date` is the latest day the caller needs. Returning MORE than that is
#     fine (the engine trims), which lets a host keep one series per coordinate
#     and serve every as-of date from it.
#   * `use_cache=False` means "the caller wants fresh data" -- bypass your cache.
# --------------------------------------------------------------------------- #
def _open_meteo_url(lat, lon, end_date):
    return ("https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat:.4f}&longitude={lon:.4f}"
            f"&start_date={PRECIP_START}&end_date={end_date.isoformat()}"
            "&daily=precipitation_sum&precipitation_unit=inch"
            "&timezone=America%2FPhoenix")

def _cache_path(lat, lon):
    """One file per rounded coordinate. Deliberately NOT keyed on end_date: the
    series is a prefix of itself, so a file fetched through a later date already
    answers every earlier one. Keying on the end date meant a fresh ~19-year
    download for every source on every new day."""
    return os.path.join(CACHE_DIR, f"{round(lat,2)}_{round(lon,2)}.json")

def _trim_daily(data, end_date):
    """Cut a series down to end_date, so a cached series that runs longer gives
    bit-identical results to one fetched for exactly this date."""
    daily = data["daily"]
    times = daily["time"]
    iso = end_date.isoformat()
    if not times or times[-1] <= iso:
        return data
    cut = bisect.bisect_right(times, iso)         # ISO dates sort chronologically
    out = dict(data)
    out["daily"] = dict(daily, time=times[:cut],
                        precipitation_sum=daily["precipitation_sum"][:cut])
    return out

def open_meteo_provider(lat, lon, end_date, use_cache=True):
    """Default provider: Open-Meteo ERA5 archive, cached per rounded coordinate."""
    cpath = _cache_path(lat, lon)
    if use_cache and os.path.exists(cpath):
        try:
            cached = json.load(open(cpath))
            times = cached.get("daily", {}).get("time") or []
            if times and times[-1] >= end_date.isoformat():
                return _trim_daily(cached, end_date)   # cache runs far enough
        except (ValueError, KeyError):
            pass                                       # unreadable cache -> refetch
    with urllib.request.urlopen(_open_meteo_url(lat, lon, end_date), timeout=120) as r:
        data = json.load(r)
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump(data, open(cpath, "w"))
    return data

PRECIP_PROVIDER = open_meteo_provider

def _check_daily(data, who):
    """Fail loudly, at the seam, on a provider that returns the wrong shape --
    otherwise a host debugs a KeyError from deep inside the stats code."""
    try:
        times = data["daily"]["time"]
        vals = data["daily"]["precipitation_sum"]
    except (TypeError, KeyError):
        raise ValueError(f"{who} must return {{'daily': {{'time': [...], "
                         f"'precipitation_sum': [...]}}}}, got {type(data).__name__}")
    if len(times) != len(vals):
        raise ValueError(f"{who} returned {len(times)} dates but {len(vals)} "
                         "precipitation values")
    if not times:
        raise ValueError(f"{who} returned an empty series")
    return data

def fetch_precip(lat, lon, end_date, use_cache=True):
    """Get the daily precip series from the active provider (see PRECIP_PROVIDER)."""
    provider = PRECIP_PROVIDER
    who = f"precip provider {getattr(provider, '__name__', provider)!r}"
    data = _check_daily(provider(lat, lon, end_date, use_cache), who)
    # Trim here too, not just in the built-in: a host is allowed to keep one long
    # series per coordinate and hand back all of it, and the as-of read must not
    # depend on how much extra it chose to return.
    return _trim_daily(data, end_date)

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

# --------------------------------------------------------------------------- #
# JSON output: the same numbers the text report shows, for a consumer instead of
# a human. Everything the verdict depends on is explicit (own vs pooled r, how
# much was borrowed, the window it was read at) so a caller can re-derive or
# second-guess the headline without re-running the engine.
# --------------------------------------------------------------------------- #
def _r4(x):
    return round(x, 4)

def source_json(a):
    ctrl = dict(a["ctrl_cors"])
    return {
        "name": a["name"], "lat": a["lat"], "lon": a["lon"],
        "n": a["n"], "small_n": a["n"] < 25,
        "pct_dry": a["pct_dry"],
        "mean_flow": _r4(a["mean"]),
        "annual_precip_in": round(a["annual_precip"], 2),
        "type": a["type"],
        "mean_flow_by_month": {str(m): _r4(v) for m, v in a["by_month"].items()},
        # per-window, strongest raw first -- each source's OWN numbers
        "correlations": [{"window": name, "days": int(name[:-1]),
                          "raw_r": _r4(r), "ctrl_r": _r4(ctrl[name])}
                         for r, name in a["cors"]],
        # the window the verdict was read at; `r` is pooled, `own_ctrl_r` is not
        "best": {"window": a["best"], "days": int(a["best"][:-1]),
                 "r": _r4(a["best_ctrl_r"]),
                 "own_ctrl_r": _r4(a["best_own_ctrl_r"]),
                 "raw_r": _r4(a["best_raw_r"]),
                 "borrowed": _r4(a["borrowed_at_best"]),
                 "group_n": a["group_n"],
                 "signal_check": survival_note(a["best_raw_r"], a["best_own_ctrl_r"])},
        "asof": a["asof"].isoformat(),
        "precip_in": round(a["curval"], 3),
        "predicted_flow": _r4(a["pred"]),
        "verdict": running_phrase(a["pred"]),
        "harmonics": a["n_harm"],
    }

def run_json(rows, notes, asof, do_pool, radius_km, n_harm, use_cache):
    """One object on stdout. Sources stay in input order -- sort client-side."""
    return {
        "asof": asof.isoformat(),
        "params": {"pool": do_pool, "pool_radius_km": radius_km,
                   "harmonics": n_harm, "cache": use_cache, "windows": WINDOWS},
        "sources": [source_json(a) for a in rows],
        "notes": notes,
    }

# Flags that take a value, as {flag: (option key, parser, what it wants)}. The
# parser is what turns the string into the option, and its name is what the user
# is told when it rejects one.
_VALUE_FLAGS = {
    "--asof":        ("asof",      date.fromisoformat, "an ISO date, e.g. 2026-08-15"),
    "--harmonics":   ("n_harm",    int,                "a whole number, e.g. 1"),
    "--pool-radius": ("radius_km", float,              "a distance in km, e.g. 25"),
}
# Flags that are just on/off, as {flag: (option key, value when present)}.
_BOOL_FLAGS = {
    "--no-cache": ("use_cache", False),
    "--no-pool":  ("do_pool",   False),
    "--json":     ("as_json",   True),
}

def parse_args(argv):
    """argv -> (files, options), in ONE pass that consumes each flag's value as it
    goes. The previous two-pass version had to ask "is this argv element the value
    of some flag?" after the fact, which it answered by object identity -- true
    only by accident of both sides coming from the same list. Consuming inline
    removes the question. Raises ValueError (message in the [error] house style)
    for a missing value, an unparseable one, or an unknown flag."""
    files = []
    opts = {"asof": date.today(), "use_cache": True, "do_pool": True,
            "as_json": False, "n_harm": 1, "radius_km": POOL_RADIUS_KM}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            name, eq, inline = a.partition("=")
            if name in _VALUE_FLAGS:
                key, parse, want = _VALUE_FLAGS[name]
                if eq:
                    raw = inline
                else:
                    if i + 1 >= len(argv):          # flag in final position
                        raise ValueError(f"{name} requires a value ({want})")
                    i += 1
                    raw = argv[i]
                try:
                    opts[key] = parse(raw)
                except ValueError:
                    raise ValueError(f"{name}: expected {want}, got {raw!r}")
            elif name in _BOOL_FLAGS:
                if eq:
                    raise ValueError(f"{name} takes no value (got {inline!r})")
                key, val = _BOOL_FLAGS[name]
                opts[key] = val
            else:
                known = ", ".join(sorted(list(_VALUE_FLAGS) + list(_BOOL_FLAGS)))
                raise ValueError(f"unknown flag {name}. Known flags: {known}")
        else:
            files.append(a)                          # "-" (stdin) lands here too
        i += 1
    return files, opts

def main(argv):
    try:
        files, opts = parse_args(argv)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr); return 2
    asof, use_cache, do_pool = opts["asof"], opts["use_cache"], opts["do_pool"]
    as_json, n_harm, radius_km = opts["as_json"], opts["n_harm"], opts["radius_km"]
    # No files named? Take the CSV from stdin when it's a pipe, so `... | forecast.py`
    # works bare; a bare invocation from a terminal still prints the usage doc.
    if not files and not sys.stdin.isatty():
        files = ["-"]
    if not files:
        print(__doc__); return 1
    # In --json mode stdout is reserved for the JSON object, so diagnostics go to
    # stderr -- and into "notes", so a consumer reading only stdout still sees them.
    notes = []
    def note(kind, msg, name=None):
        notes.append({"kind": kind, "source": name, "message": msg})
        line = f"[{kind}] " + (f"{name}: " if name else "") + msg
        print(line, file=sys.stderr if as_json else sys.stdout)
    try:
        sources = load_sources(files)
    except Exception as e:
        note("error", str(e))
        if as_json:
            json.dump(run_json([], notes, asof, do_pool, radius_km, n_harm, use_cache),
                      sys.stdout, indent=2)
            print()
        return 2
    # Pass 1: per-source base (fetch precip + per-window r vectors), no window chosen yet.
    bases = []
    for src in sources:
        try:
            b = analyze_base(src, asof, use_cache, n_harm)
            if b is None:
                note("skip", "no reports within precip range", src["name"]); continue
            bases.append(b)
        except Exception as e:
            note("error", str(e), src["name"])
    # Pass 2: pool the season-controlled r across nearby sources (needs >=2 to borrow).
    if do_pool and len(bases) > 1:
        pool_controlled(bases, radius_km)
    # Pass 3: finalize (best window / class / analog read off the pooled r) + report.
    rows = [finalize(b) for b in bases]
    if as_json:
        json.dump(run_json(rows, notes, asof, do_pool, radius_km, n_harm, use_cache),
                  sys.stdout, indent=2)
        print()
        return 0
    for a in rows:
        print_source(a)
    if len(rows) > 1:
        print_table(rows)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
