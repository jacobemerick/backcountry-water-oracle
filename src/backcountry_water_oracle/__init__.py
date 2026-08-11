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
    python3 forecast.py az.csv --precip iem:mrms        # a different precip product
    cat area.csv | python3 forecast.py -                # read the CSV from stdin
    python3 forecast.py area.csv --json                 # machine-readable output

EMBEDDING IT (hosts):
    pip install backcountry-water-oracle     # or: pip install git+<repo>@v0.1.0

    import backcountry_water_oracle as bwo
    bwo.PRECIP_PROVIDER = my_provider        # (lat, lon, end_date, use_cache)
    sources = bwo.load_sources_from([io.StringIO(request_body)])
    payload = bwo.run(sources, asof)         # the same dict --json prints
  Three seams, and between them a host needs nothing private and reimplements
  nothing. load_sources_from() takes CSV that never touched the filesystem;
  run() is main() minus the CLI, so a service gets the engine's real behaviour
  instead of a copy that drifts; PRECIP_PROVIDER swaps the precip backend to a
  shared store (Postgres/KV) instead of the on-disk cache. Still stdlib-only,
  still no change to the CLI. See README "What's public" for what is covered by
  the version number, and what is not.

PIPING IT AROUND:
  * `-` as a filename reads the CSV from stdin (and stdin is used automatically
    when no files are given and stdin is not a terminal), so the engine drops
    into a pipeline: your normalizer | forecast.py - --json | your consumer.
  * `--json` prints one JSON object on stdout instead of the text report -- every
    number the text report shows, unrounded-to-4dp, plus the run's parameters.
    In --json mode all diagnostics ([skip]/[error]) go to stderr and are also
    collected under "notes", so stdout stays valid JSON.

Pure standard library -- zero runtime dependencies, installed or not.
Precip: Open-Meteo ERA5 archive (free) by default; --precip picks another product.

TRUST IT THIS MUCH:
  * ERA5 precip is ~9-11 km grid -> it SMOOTHS/MISSES isolated monsoon cells.
    Trust the winter-recharge signal; for a summer "did a storm just hit" call,
    still eyeball radar (MRMS / AHPS). This gives the base rate, not this week.
  * --precip swaps the product the WHOLE run is fit on (PRECIP_PROVIDERS below has
    what the bake-off found: the fit barely moves, the as-of read moves a lot).
    Two forecasts are only comparable if params.precip matches, the same way
    params.engine_version has to.
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
import bisect, csv, json, math, os, sys, time, urllib.request
from datetime import date, timedelta

# Single source of truth: pyproject.toml reads this attribute, so the package
# metadata and the number reported by --version cannot drift apart.
#
# 0.x while the API is still moving. What a bump means:
#   MAJOR/MINOR -- the Python API or the --json schema changed (see README,
#                  "What's public"). While at 0.x, that is the minor field.
#   PATCH       -- a fix that leaves both alone.
# A change to the METHOD -- pooling, season control, the analog read -- can move
# verdicts without touching either surface, so those go in the changelog and are
# stamped into every payload as params.engine_version. The golden test in
# tests/ fails whenever that happens, which is the signal to write it down.
__version__ = "0.1.0"

HERE = os.path.dirname(os.path.abspath(__file__))

def _checkout_cache():
    """A `.cache/` belonging to the source tree this module is part of, if any.

    Looked up by walking a few levels above the module, because the engine lives
    at src/backcountry_water_oracle/ while its cache sits at the repo root -- so
    "beside this file" alone would orphan an existing checkout's downloads and
    silently re-fetch ~19 years per coordinate. Ancestors must look like a
    checkout (.git / pyproject.toml) before their .cache is claimed; an installed
    copy has no such ancestor and falls through to the user cache."""
    d = HERE
    for _ in range(4):
        cache = os.path.join(d, ".cache")
        is_checkout = (os.path.exists(os.path.join(d, ".git"))
                       or os.path.exists(os.path.join(d, "pyproject.toml")))
        if (d == HERE or is_checkout) and os.path.isdir(cache) and os.access(cache, os.W_OK):
            return cache
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None

def _default_cache_dir():
    """Where to keep downloaded precipitation.

    This used to be unconditionally `.cache/` beside this file, which is right for
    a script you run out of a checkout and wrong the moment the engine is
    installed: it would write into site-packages, and simply fail where that is
    read-only (containers, serverless bundles, a system install).

    Order: an explicit WATER_ORACLE_CACHE; else this source tree's existing
    `.cache/`, so a checkout keeps its downloads; else the platform's user cache."""
    env = os.environ.get("WATER_ORACLE_CACHE")
    if env:
        return env
    checkout = _checkout_cache()
    if checkout:
        return checkout
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "backcountry-water-oracle")

CACHE_DIR = _default_cache_dir()      # assignable; a host may point it anywhere
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
        # A row with BOTH date and score blank is a coordinate-only source: "here is
        # a pin, tell me what you can". It registers the source and contributes no
        # observation, which lands it in the same n == 0 path as a source whose every
        # report fell outside the precip record -- rain context, no flow verdict (#8).
        raw_date = (row["date"] or "").strip()
        raw_score = (row["score"] or "").strip()
        if not raw_date and not raw_score:
            continue
        if not raw_date or not raw_score:
            # One of the two blank is a typo, not a request. Guessing (dropping the
            # row, or scoring a dateless report) would silently change the record.
            missing, given = (("date", "score") if not raw_date else ("score", "date"))
            raise ValueError(
                f"{label} line {reader.line_num}: source {name!r} has a {given} but "
                f"no {missing}. Leave BOTH blank for a coordinate-only source (rain "
                f"context, no verdict); fill both in for an observation.")
        sc = max(0.0, min(1.0, float(raw_score)))
        s["reports"].append((date.fromisoformat(raw_date[:10]), sc))

def _load(labelled_streams):
    """The one loading path: consume (stream, label) pairs into finished sources.
    Sorting the reports happens HERE rather than in each caller -- it is a promise
    the loader makes, not a chore it hands back."""
    sources = {}
    for stream, label in labelled_streams:
        _read_csv(stream, label, sources)
    for s in sources.values():
        s["reports"].sort()
    return list(sources.values())

def load_sources_from(streams, labels=None):
    """Return list of {name, lat, lon, reports=[(date, score)]} from already-open
    TEXT streams -- anything csv.DictReader can read, e.g. io.StringIO.

    This is the entry point for an embedding host holding CSV it never wrote to
    disk (an HTTP request body, a database column, an S3 object):

        sources = forecast.load_sources_from([io.StringIO(request_body)])

    `labels` name the streams in error messages, the way filenames do for
    load_sources; a stream with no label is called "<stream N>". Pass something a
    user will recognise -- the label is what turns "CSV missing column(s)" into a
    message that says WHICH input was wrong.

    Reports come back sorted, same as load_sources: callers owe nothing."""
    if hasattr(streams, "read"):        # a bare stream, not a list of them --
        streams = [streams]             # iterating it would read one LINE per "stream"
    if isinstance(labels, str):
        labels = [labels]
    labels = list(labels) if labels is not None else []
    return _load((s, labels[i] if i < len(labels) else f"<stream {i + 1}>")
                 for i, s in enumerate(streams))

def load_sources(paths):
    """Return list of {name, lat, lon, reports=[(date, score)]} from file paths.
    A path of "-" means stdin, so the engine can sit in a pipeline; stdin can only
    be consumed once, so a repeated "-" is ignored after the first.

    Hosts working from memory rather than the filesystem want load_sources_from()."""
    def opened():
        stdin_done = False
        for p in paths:
            if p == "-":
                if stdin_done:
                    continue
                stdin_done = True
                yield sys.stdin, "<stdin>"
            else:
                # The consumer reads the file before this generator resumes, so the
                # `with` still closes each one immediately after it is consumed.
                with open(p, newline="", encoding="utf-8") as f:
                    yield f, p
    return _load(opened())

# --------------------------------------------------------------------------- #
# Precipitation: a swappable provider
# ---------------------------------------------------------------------------
# Everything downstream needs is one daily series per coordinate:
#
#     provider(lat, lon, end_date, use_cache) -> {"daily": {"time": [ISO...],
#                                                 "precipitation_sum": [inches...]}}
#
# There are two ways to choose one, and they answer different questions:
#
#   * A BUILT-IN, by name -- `--precip iem:mrms`, or run(..., precip="iem:mrms").
#     PRECIP_PROVIDERS below is the registry; open-meteo (ERA5) is the default and
#     stays the default. This is for "same engine, different product", and the
#     product that actually answered is stamped into params.precip so a stored
#     payload says which one it was.
#   * A CALLABLE, by assignment -- PRECIP_PROVIDER = my_provider. This is for a
#     host supplying its own storage or its own data entirely.
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
#   * Raise ValueError with a sentence a user can act on when you cannot serve a
#     coordinate at all. The engine turns that into a per-source [error] note and
#     carries on with the other sources; it never substitutes another product.
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
            # Closed explicitly: CPython's refcounting hides a leak here, but a
            # long-lived host process would hold one handle per cached coordinate.
            with open(cpath) as f:
                cached = json.load(f)
            times = cached.get("daily", {}).get("time") or []
            if times and times[-1] >= end_date.isoformat():
                return _trim_daily(cached, end_date)   # cache runs far enough
        except (ValueError, KeyError):
            pass                                       # unreadable cache -> refetch
    with urllib.request.urlopen(_open_meteo_url(lat, lon, end_date), timeout=120) as r:
        data = json.load(r)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cpath, "w") as f:
        json.dump(data, f)
    return data

# --------------------------------------------------------------------------- #
# The other built-in: Iowa State's IEMRE point service, which serves PRISM
# (gauge-interpolated) and MRMS (radar) alongside each other. CONUS only.
#
# What tools/precip_bakeoff.py found, because it decides how much to expect:
#   * This endpoint resamples EVERY product onto the IEMRE grid (0.125 deg,
#     ~11.6 km), so it is NOT a resolution upgrade over ERA5's ~9-11 km. Two
#     springs either side of a ridge still share a cell.
#   * The fit barely notices the product (season-controlled r moves <0.05; no best
#     window or type changed on the worked example). The AS-OF READ moves a lot,
#     because MRMS sees convective cells ERA5 and PRISM both miss -- a 3.66" day
#     read as 0.04" by ERA5 and 0.00" by PRISM. That is the reason to reach for
#     this, and #18 is where it gets used properly (as a cross-check beside the
#     ERA5 fit, rather than by refitting the whole model on radar).
# --------------------------------------------------------------------------- #
IEM_MULTIDAY = ("https://mesonet.agron.iastate.edu/iemre/multiday"
                "/{d1}/{d2}/{lat}/{lon}/json")
# The IEMRE domain, near enough. Checked BEFORE the network call so a source in
# the Alps is told what is wrong in one sentence, rather than being handed 19
# years of nulls that would read downstream as "it never rained there".
IEM_LAT_RANGE = (23.0, 51.0)
IEM_LON_RANGE = (-126.0, -65.0)
IEM_PAUSE_S = 0.5        # between requests -- it is a free service, chunked by year
IEM_RETRIES = 3

def _iem_cache_path(lat, lon, year):
    """One file per rounded coordinate-YEAR, holding every product that year.

    Chunked by year rather than by span because the service is asked for a date
    range: one file per (coordinate, year) is the largest chunk that a later run
    can reuse unchanged. Rounded to 2dp like _cache_path, for the same reason --
    it is far finer than the ~11.6 km grid actually being served, so two sources a
    few hundred metres apart share the download they would have shared anyway.

    Both products land in the same file: the endpoint returns them together, so
    storing one and discarding the other would mean fetching the same year twice
    to answer --precip iem:prism and then --precip iem:mrms."""
    return os.path.join(CACHE_DIR, "iem", f"{round(lat,2)}_{round(lon,2)}_{year}.json")

def _iem_year(lat, lon, year, start, end, use_cache=True):
    """One coordinate-year as [{"date", "prism", "mrms"}, ...], cached on disk."""
    d1 = max(date(year, 1, 1), start)
    d2 = min(date(year, 12, 31), end)
    cpath = _iem_cache_path(lat, lon, year)
    if use_cache and os.path.exists(cpath):
        try:
            with open(cpath) as f:
                rows = json.load(f)
            # The CURRENT year is cached mid-flight, so "the file exists" is not
            # enough -- a file written last week stops before today and would
            # quietly truncate every antecedent window reaching into the gap.
            # Trust it only when it actually spans what was asked for.
            if rows and rows[0]["date"] <= d1.isoformat() and rows[-1]["date"] >= d2.isoformat():
                return rows
        except (ValueError, KeyError, TypeError):
            pass                                       # unreadable cache -> refetch
    url = IEM_MULTIDAY.format(d1=d1.isoformat(), d2=d2.isoformat(), lat=lat, lon=lon)
    for attempt in range(IEM_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                data = json.load(r)
            break
        except Exception:
            if attempt == IEM_RETRIES - 1:
                raise
            time.sleep(3)
    rows = [{"date": row["date"], "prism": row.get("prism_precip_in"),
             "mrms": row.get("mrms_precip_in")} for row in data.get("data", [])]
    os.makedirs(os.path.dirname(cpath), exist_ok=True)
    with open(cpath, "w") as f:
        json.dump(rows, f)
    time.sleep(IEM_PAUSE_S)                  # be a good citizen of a free service
    return rows

def _iem_provider(field, label, caveat=None):
    """Build a provider that reads one IEMRE field. See PRECIP_PROVIDERS below."""
    def provider(lat, lon, end_date, use_cache=True):
        if not (IEM_LAT_RANGE[0] <= lat <= IEM_LAT_RANGE[1]
                and IEM_LON_RANGE[0] <= lon <= IEM_LON_RANGE[1]):
            raise ValueError(
                f"{label} covers CONUS only, and ({lat:.5f}, {lon:.5f}) is outside "
                "it. Re-run without --precip (Open-Meteo ERA5 is global).")
        start = date.fromisoformat(PRECIP_START)
        rows = []
        for year in range(start.year, end_date.year + 1):
            rows.extend(_iem_year(lat, lon, year, start, end_date, use_cache))
        vals = [r[field] for r in rows]
        # Inside the bounding box but outside the grid (offshore, over the border)
        # the service answers with nulls rather than an error. Nineteen years of
        # them is not a dry site, it is no coverage -- say so instead of fitting a
        # model to a flat zero.
        if all(v is None for v in vals):
            raise ValueError(
                f"{label} has no data for ({lat:.5f}, {lon:.5f}) -- inside the "
                "service's bounding box but outside its grid. Re-run without "
                "--precip (Open-Meteo ERA5 is global).")
        return {"daily": {"time": [r["date"] for r in rows],
                          "precipitation_sum": vals}}
    provider.__name__ = f"iem_{field}_provider"
    provider.caveat = caveat
    return provider

iem_prism_provider = _iem_provider(
    "prism", "PRISM (IEM)",
    "PRISM is gauge-interpolated: where no gauge sat under a convective cell it "
    "has nothing to interpolate from, and reads 0.00\" for a storm that happened.")
iem_mrms_provider = _iem_provider(
    "mrms", "MRMS (IEM)",
    "MRMS is genuine radar only from ~2014; earlier values are a backfilled proxy. "
    "This run fits the model across both, so the historical record and the "
    "nearest-analog pool mix radar with proxy.")

# The named built-ins, for --precip and run(precip=...). Adding an entry here is
# all a new product needs; the CLI, the JSON stamp and the error messages read it.
PRECIP_PROVIDERS = {
    "open-meteo": open_meteo_provider,
    "iem:prism":  iem_prism_provider,
    "iem:mrms":   iem_mrms_provider,
}
DEFAULT_PRECIP = "open-meteo"

PRECIP_PROVIDER = PRECIP_PROVIDERS[DEFAULT_PRECIP]

def resolve_precip(name):
    """A built-in's name -> its provider callable. Raises ValueError on an unknown
    name, listing what there is."""
    try:
        return PRECIP_PROVIDERS[name]
    except (KeyError, TypeError):
        raise ValueError(f"unknown precip provider {name!r}. Known: "
                         + ", ".join(sorted(PRECIP_PROVIDERS)))

def precip_name(provider):
    """The reverse: what to call the provider that actually answered, for
    params.precip.

    A host's own callable isn't in the registry, so it falls back to the function's
    name -- the payload must never claim ERA5 answered when something else did. But
    "not in the registry" and "not ERA5" are different things: a host that keeps
    ERA5 in Postgres has swapped the TRANSPORT, not the product, and its payloads
    should still say open-meteo so they stay comparable with everyone else's. Such
    a provider says so by setting the attribute:

        my_provider.precip_name = "open-meteo"   # ERA5, just from our own store
    """
    declared = getattr(provider, "precip_name", None)
    if declared:
        return declared
    for name, p in PRECIP_PROVIDERS.items():
        if p is provider:
            return name
    return getattr(provider, "__name__", None) or "custom"

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

def fetch_precip(lat, lon, end_date, use_cache=True, provider=None):
    """Get the daily precip series from `provider`, defaulting to PRECIP_PROVIDER.

    Passed down the call chain rather than swapped into the global for the length
    of a run: a host serving two requests on two threads may well want two
    different products, and a global would hand one request the other's rain."""
    provider = provider or PRECIP_PROVIDER
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
# Antecedent rain against the site's OWN climatology (#8)
# ---------------------------------------------------------------------------
# "0.86 inches in the last 60 days" means nothing on its own -- it is a lot in one
# place and a drought in another, and a lot in April and nothing in August. Ranking
# it against the same calendar window in every other year of this coordinate's
# record turns it into a sentence a person can act on:
#
#     60-day rain here is in the 12th percentile for mid-August, across 19 years.
#
# It needs NO field reports, which is the point: it is the only thing the engine can
# honestly say about a coordinate nobody has ever reported on. It is also worth
# saying about a well-reported one -- the verdict is a base rate, and this is how
# unusual the run-up to today has been.
#
# It is NOT a flow verdict and must never be presented as one. Wet ground is not
# water in the creek: the whole reason the rest of this file exists is that the map
# from rain to flow is different for every source and has to be learned from that
# source's own reports.
# --------------------------------------------------------------------------- #
def _same_day_of_year(d, year):
    """The same calendar day in another year. 29 Feb becomes 28 Feb rather than
    raising -- one day's shift is far inside the noise of a 30-day window."""
    try:
        return d.replace(year=year)
    except ValueError:
        return d.replace(year=year, day=28)

def _percentile_of(value, sample):
    """Midrank percentile: ties count half. In a desert where a third of Augusts are
    bone dry, a bone-dry August is the 17th percentile, not the 0th -- and it should
    not read as drier than the years it exactly matches."""
    below = sum(1 for v in sample if v < value)
    tied = sum(1 for v in sample if v == value)
    return 100.0 * (below + 0.5 * tied) / len(sample)

def rain_percentiles(idx, asof):
    """{window: {inches, pct, n_years, median_in}} for every window in WINDOWS.

    Each window's antecedent sum at `asof`, ranked against the same window ending
    on the same day-of-year in every OTHER year of the record. The as-of year is
    excluded so the reading is compared against history rather than diluted by
    itself, and a year is only usable when the whole window fits inside the record
    -- so the long windows are ranked against fewer years than the short ones, and
    `n_years` says how many. `pct` is None when no year qualifies at all.

    ~19 years is a small sample: percentiles land on ~5-point steps and the tails
    are the least trustworthy part. Read it as "unusually dry / about normal /
    unusually wet", not as a precise rank."""
    out = {}
    for w in WINDOWS:
        sample = []
        for year in range(idx["first"].year, idx["last"].year + 1):
            if year == asof.year:
                continue
            end = _same_day_of_year(asof, year)
            if end > idx["last"] or end - timedelta(days=w) < idx["first"]:
                continue                      # window runs off the end of the record
            sample.append(window_sum(idx, end, w))
        cur = window_sum(idx, asof, w)
        out[f"{w}d"] = {"inches": cur, "n_years": len(sample),
                        "pct": _percentile_of(cur, sample) if sample else None,
                        "median_in": _median(sample) if sample else None}
    return out

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

def analyze_base(src, asof, use_cache=True, n_harm=1, provider=None):
    """Per-source pass: precip -> raw + season-controlled per-window r vectors, plus
    the machinery finalize() needs. Stops BEFORE choosing a best window, so pooling
    can adjust the controlled r first. `pooled_ctrl` starts equal to the source's own
    `ctrl` (i.e. no neighbors); pool_controlled() overwrites it when pooling runs.

    Returns a dict with n == 0 (and the report accounting, but none of the analysis)
    when nothing the source reported falls inside the precip record, so the caller
    can say WHY it is skipping rather than just that it did."""
    data = fetch_precip(src["lat"], src["lon"],
                        min(asof, date.today() - timedelta(days=ERA5_LAG_DAYS)),
                        use_cache, provider)
    idx = build_precip_index(data)
    recs = [(d, s) for d, s in src["reports"] if idx["first"] < d <= idx["last"]]
    dates = [d for d, _ in recs]
    y = [s for _, s in recs]
    n = len(y)
    # A report outside the precip record can't be correlated against anything, so it
    # is dropped -- but n / %dry / mean / by_month are then computed on the survivors
    # only, which silently misrepresents the record. Count what went, and why.
    n_total = len(src["reports"])
    n_early = sum(1 for d, _ in src["reports"] if d <= idx["first"])
    n_late = sum(1 for d, _ in src["reports"] if d > idx["last"])
    counts = {"n_total": n_total, "n_early": n_early, "n_late": n_late,
              "precip_first": idx["first"], "precip_last": idx["last"]}
    # Rain context, for every source: it needs no reports, so it is computed before
    # the n == 0 exit rather than inside the analysis that n == 0 skips.
    asof_eff = min(asof, idx["last"])
    common = dict(counts, name=src["name"], lat=src["lat"], lon=src["lon"],
                  annual_precip=idx["annual"], rain_pct=rain_percentiles(idx, asof_eff),
                  asof_eff=asof_eff, n_harm=n_harm)
    if n == 0:
        return dict(common, n=0)
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
    return dict(common,
                n=n, pct_dry=pct_dry, mean=sum(y) / n,
                raw=raw, ctrl=ctrl,
                pooled_ctrl=dict(ctrl), borrowed={k: 0.0 for k in ctrl},
                group_n=1,
                idx=idx, recs=recs, asof_req=asof,
                by_month={m: sum(v) / len(v) for m, v in sorted(bym.items())})

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
            "n_total": b["n_total"], "n_early": b["n_early"], "n_late": b["n_late"],
            "precip_first": b["precip_first"], "precip_last": b["precip_last"],
            "annual_precip": b["annual_precip"],
            "cors": sorted(((r, name) for name, r in b["raw"].items()), key=lambda t: -abs(t[0])),
            "ctrl_cors": sorted(b["ctrl"].items(), key=lambda t: -abs(t[1])),   # OWN, for the table
            "best": best, "best_ctrl_r": best_ctrl_r,                           # POOLED, drives verdict
            "best_raw_r": b["raw"][best], "best_own_ctrl_r": b["ctrl"][best],
            "borrowed_at_best": b["borrowed"][best], "group_n": b["group_n"],
            "type": classify(b["pct_dry"], best_w),
            "curval": curval, "pred": pred, "asof": asof_eff, "n_harm": b["n_harm"],
            "by_month": b["by_month"], "rain_pct": b["rain_pct"]}

def finalize_rain_only(b):
    """finalize()'s counterpart for a source with no usable reports (#8).

    Everything a verdict is built from is absent -- there is no correlation to pick
    a window with, and no report history to read an analog flow off -- so this
    carries only what precipitation alone can support. It is a separate function
    rather than a branch inside finalize() because the two produce genuinely
    different objects, and a half-filled verdict dict is exactly the thing that
    gets rendered as a verdict by accident."""
    return {"name": b["name"], "lat": b["lat"], "lon": b["lon"], "n": 0,
            "n_total": b["n_total"], "n_early": b["n_early"], "n_late": b["n_late"],
            "precip_first": b["precip_first"], "precip_last": b["precip_last"],
            "annual_precip": b["annual_precip"], "asof": b["asof_eff"],
            "n_harm": b["n_harm"], "rain_pct": b["rain_pct"]}

# --------------------------------------------------------------------------- #
# Neighbor disclosure (#8, second half)
# ---------------------------------------------------------------------------
# For a source with no verdict, the most useful thing left is what is NEARBY. The
# engine names those sources and stops there -- it does not transfer their read.
#
# Why disclosure rather than transfer, since the issue originally asked for the
# latter: re-running a neighbor's rain->flow mapping against this coordinate's
# rain is arithmetically IDENTICAL to quoting the neighbor, because both sit in
# one precip cell. ERA5's grid is ~9-11 km; the three worked-example sources span
# 3.5 km and share a single series, so curval, every window_sum in hist, and pred
# all come out bit-for-bit equal to the neighbor's own. The machinery would look
# like it computed something and would not have. Past ~11 km the numbers finally
# differ -- and that is exactly where "nearby, so similar" stops being credible.
#
# Two further blocks, if this is ever revisited: the empirical-Bayes arbitration
# that makes pooling honest has no input when a source has no correlation vector
# to disagree with, and a verdict needs pct_dry and the analog flow scores --
# precisely the quantities that are always each source's own, never pooled.
#
# So: say who is near, say what THEY read, and say when they disagree with each
# other -- because that disagreement is the real answer to "can I use one of
# these as a stand-in", and it is always no.
# --------------------------------------------------------------------------- #
def neighbors_of(a, rows, radius_km):
    """The REPORTED sources within radius_km of `a`, nearest first.

    Each entry carries the neighbor's own numbers under its own name. Nothing is
    combined, averaged or transferred: two neighbors produce two rows, not one
    estimate. Other unreported sources are skipped -- a pin cannot inform a pin."""
    out = []
    for o in rows:
        if o is a or o["n"] == 0:
            continue
        km = _haversine_km(a["lat"], a["lon"], o["lat"], o["lon"])
        if km <= radius_km:
            out.append({"name": o["name"], "distance_km": km, "n": o["n"],
                        "type": o["type"], "pct_dry": o["pct_dry"],
                        "verdict": running_phrase(o["pred"]),
                        "predicted_flow": o["pred"]})
    out.sort(key=lambda x: x["distance_km"])
    return out

def attach_neighbors(rows, radius_km):
    """In place: give every source with no verdict its neighbors, everyone else an
    empty list. Same key on every source, for the reason the rest of the payload
    keeps its shape -- a consumer branches on n == 0, never on which keys exist.

    Only sources WITHOUT a verdict get them populated. A reported source already
    says what it borrowed from whom (best.borrowed / best.group_n), and repeating
    every neighbor's row underneath it would bloat a payload the site fetches per
    request to say nothing new."""
    for a in rows:
        near = neighbors_of(a, rows, radius_km) if a["n"] == 0 else []
        a["neighbors"] = near
        # Disagreement is the headline, not a footnote: if the nearby sources do
        # not even agree on what KIND of source they are, no stand-in is safe.
        a["neighbors_disagree"] = len({o["type"] for o in near}) > 1

def analyze(src, asof, use_cache=True, n_harm=1, provider=None):
    """Single-source convenience (no pooling): base pass then finalize.
    None when the source has nothing inside the precip record (see excluded_note).
    `provider` overrides PRECIP_PROVIDER for this call; resolve_precip() turns one
    of the built-in names into one."""
    b = analyze_base(src, asof, use_cache, n_harm, provider)
    return None if b is None or b["n"] == 0 else finalize(b)

def excluded_note(a):
    """One line about reports that fell outside the precip record, or None. Applies
    to a base or a finalized dict, including the n == 0 case."""
    early, late, total = a["n_early"], a["n_late"], a["n_total"]
    if not (early or late):
        return None
    bits = []
    if early:
        bits.append(f"{early} predate{'' if early > 1 else 's'} "
                    f"the precip record ({a['precip_first']})")
    if late:
        bits.append(f"{late} postdate{'' if late > 1 else 's'} "
                    f"{'it' if early else 'the precip record'} ({a['precip_last']})")
    kept = total - early - late
    return (f"{kept} of {total} reports usable -- " + " and ".join(bits)
            + (" -- so n, %dry and mean below describe the usable ones"
               if kept else ""))

def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"

def print_rain_block(a):
    """The antecedent-rain percentiles, with the label doing the work of keeping
    them from being read as a verdict."""
    when = a["asof"].strftime("%b %d")
    print(f"\n  ANTECEDENT RAIN vs this site's own record, for ~{when}:")
    for w in WINDOWS:
        v = a["rain_pct"][f"{w}d"]
        if v["pct"] is None:
            print(f"     {w:>3}d  {v['inches']:>6.2f}\"   (no earlier year covers "
                  "this window)")
            continue
        bar = "#" * int(round(v["pct"] / 5.0))
        print(f"     {w:>3}d  {v['inches']:>6.2f}\"   {_ordinal(round(v['pct'])):>4} pct "
              f"of {v['n_years']} yrs   median {v['median_in']:.2f}\"  {bar}")
    print("     ^ how unusual the run-up to this date has been -- RAIN, not flow.")

def print_rain_only_source(a):
    """A source with no usable reports: everything the engine can honestly say."""
    print(f"\n{'='*74}\n{a['name']}   ({a['lat']:.5f}, {a['lon']:.5f})")
    print(f"  reports: 0 usable   |   ~{a['annual_precip']:.0f}\"/yr")
    excl = excluded_note(a)
    if excl:
        print(f"  NOTE: {excl}")
    print("  NO FLOW VERDICT -- with no usable reports there is nothing to learn")
    print("  how this source answers rain, and rain alone does not decide it.")
    print_rain_block(a)
    print_neighbors(a)

def print_neighbors(a):
    """Who is nearby and what THEY read. Never combined into a read for this one."""
    if not a["neighbors"]:
        print("\n  No reported sources nearby -- the rain above is all there is.")
        return
    print("\n  NEARBY REPORTED SOURCES -- their own reads, NOT this coordinate's:")
    for o in a["neighbors"]:
        # Type is never truncated -- the longest is "Reliable (groundwater-buffered)"
        # at 31, and cutting it drops the paren, which reads like a rendering bug.
        print(f"     {o['distance_km']:>5.2f} km  {o['name'][:26]:<28}"
              f"{o['pct_dry']:>3}% dry  {o['type']:<33}{o['verdict']}")
    if a["neighbors_disagree"]:
        print("     ^ these disagree about what KIND of source they are, so none of")
        print("       them is a safe stand-in for this one.")
    else:
        print("     ^ a nearby source is not this source. Same rain, different ground.")

def print_source(a):
    if a["n"] == 0:
        return print_rain_only_source(a)
    print(f"\n{'='*74}\n{a['name']}   ({a['lat']:.5f}, {a['lon']:.5f})")
    print(f"  reports: {a['n']}   |   {a['pct_dry']}% ever dry   |   "
          f"mean flow {a['mean']:.2f} (0-1)   |   ~{a['annual_precip']:.0f}\"/yr")
    excl = excluded_note(a)
    if excl:
        print(f"  NOTE: {excl}")
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
    print_rain_block(a)

def print_table(rows, precip=DEFAULT_PRECIP):
    print(f"\n{'='*74}\nSUMMARY  (most reliable first)\n")
    # A source with no usable reports has no %-dry to rank by and no read to show,
    # so it cannot have a row here. Listed separately below instead -- inventing a
    # row for it would put a blank where every other line carries a verdict.
    rain_only = [a for a in rows if a["n"] == 0]
    scored = sorted((a for a in rows if a["n"] > 0), key=lambda a: a["pct_dry"])
    if scored:
        h = f"{'SOURCE':<26}{'N':>4}{'%DRY':>6}{'BEST':>7}{'r*':>7}{'POOL':>6}   {'AS-OF READ'}"
        print(h); print("-" * len(h))
        for a in scored:
            nm = (a["name"][:24] + "..") if len(a["name"]) > 26 else a["name"]
            pool = (f"{a['borrowed_at_best']*100:.0f}%" if a["group_n"] > 1 else "-").rjust(6)
            print(f"{nm:<26}{a['n']:>4}{a['pct_dry']:>5}%{a['best']:>7}"
                  f"{a['best_ctrl_r']:>+7.2f}{pool}   {running_phrase(a['pred'])}")
    if rain_only:
        wname = f"{WINDOWS[1]}d"
        print(f"\nNO VERDICT -- no usable reports. Rain context only (in full above):")
        for a in rain_only:
            v = a["rain_pct"][wname]
            pct = "n/a" if v["pct"] is None else f"{_ordinal(round(v['pct']))} pct"
            print(f"  {a['name'][:38]:<40}{wname} rain {v['inches']:>5.2f}\" = "
                  f"{pct} for this date")
    if scored:
        print("\nr* = season-controlled Spearman (day-of-year removed), POOLED toward nearby")
        print("sources where they agree -- the trustworthy one. POOL = % of r* borrowed from")
        print("neighbors ('-' = none in range; --no-pool disables pooling entirely).")
        print("Type key: <=10% dry = buffered/reliable; flashy = short-window + often dry.")
    # The standing monsoon caveat is about ERA5 specifically, so it stops being
    # true the moment --precip picks something else -- say which product answered
    # instead, and let the [caveat] note above carry that product's own limits.
    # It applies to the rain percentiles as much as to the verdicts, so it is
    # printed even when every source here is rain-only.
    if precip == DEFAULT_PRECIP:
        print("Reminder: ERA5 misses monsoon cells -- for a summer go/no-go, cross-check radar (MRMS/AHPS).")
    else:
        print(f"Precip: {precip}, NOT the default ERA5 -- see the [caveat] above, and")
        print("don't compare these numbers against a run made with another product.")

# --------------------------------------------------------------------------- #
# JSON output: the same numbers the text report shows, for a consumer instead of
# a human. Everything the verdict depends on is explicit (own vs pooled r, how
# much was borrowed, the window it was read at) so a caller can re-derive or
# second-guess the headline without re-running the engine.
# --------------------------------------------------------------------------- #
def _r4(x):
    return round(x, 4)

def rain_json(rain):
    """Antecedent rain vs the site's own climatology, per window (#8).
    `pct` is rounded to a whole number: ~19 years cannot support a decimal."""
    return {w: {"inches": round(v["inches"], 3),
                "pct": None if v["pct"] is None else round(v["pct"]),
                "n_years": v["n_years"],
                "median_in": None if v["median_in"] is None else round(v["median_in"], 3)}
            for w, v in rain.items()}

def source_json(a):
    """One source, as the payload sees it.

    A source with no usable reports keeps EVERY key. The verdict-derived ones are
    null (empty for the two containers), never absent, so a consumer branches on
    `n == 0` -- equivalently `verdict is None` -- instead of on which keys happen
    to exist. `rain_percentiles`, `annual_precip_in` and the report accounting are
    real in both cases: they come from precipitation and from counting, neither of
    which needs a usable report."""
    zero = a["n"] == 0
    ctrl = {} if zero else dict(a["ctrl_cors"])
    return {
        "name": a["name"], "lat": a["lat"], "lon": a["lon"],
        "n": a["n"], "small_n": a["n"] < 25,
        # Report accounting: `n` above is what the analysis actually used. A host
        # showing "we have 12 reports, 9 usable" reads it from here.
        "reports": {"total": a["n_total"], "used": a["n"],
                    "excluded_before_precip": a["n_early"],
                    "excluded_after_precip": a["n_late"],
                    "precip_span": [a["precip_first"].isoformat(),
                                    a["precip_last"].isoformat()]},
        "pct_dry": None if zero else a["pct_dry"],
        "mean_flow": None if zero else _r4(a["mean"]),
        "annual_precip_in": round(a["annual_precip"], 2),
        "type": None if zero else a["type"],
        "mean_flow_by_month": {} if zero else
                              {str(m): _r4(v) for m, v in a["by_month"].items()},
        # per-window, strongest raw first -- each source's OWN numbers
        "correlations": [] if zero else
                        [{"window": name, "days": int(name[:-1]),
                          "raw_r": _r4(r), "ctrl_r": _r4(ctrl[name])}
                         for r, name in a["cors"]],
        # the window the verdict was read at; `r` is pooled, `own_ctrl_r` is not
        "best": None if zero else
                {"window": a["best"], "days": int(a["best"][:-1]),
                 "r": _r4(a["best_ctrl_r"]),
                 "own_ctrl_r": _r4(a["best_own_ctrl_r"]),
                 "raw_r": _r4(a["best_raw_r"]),
                 "borrowed": _r4(a["borrowed_at_best"]),
                 "group_n": a["group_n"],
                 "signal_check": survival_note(a["best_raw_r"], a["best_own_ctrl_r"])},
        "asof": a["asof"].isoformat(),
        "precip_in": None if zero else round(a["curval"], 3),
        "predicted_flow": None if zero else _r4(a["pred"]),
        "verdict": None if zero else running_phrase(a["pred"]),
        "harmonics": a["n_harm"],
        # Rain vs this site's own record, for every source -- and the ONLY analysis
        # available when n == 0. Never a flow verdict: see rain_percentiles().
        "rain_percentiles": rain_json(a["rain_pct"]),
        # Nearby REPORTED sources, populated only where there is no verdict of our
        # own (see attach_neighbors). Each entry is that source's own read, under
        # its own name -- nothing here has been transferred onto this coordinate,
        # and `predicted_flow` above stays null regardless of what these say.
        "neighbors": [{"name": o["name"], "distance_km": round(o["distance_km"], 2),
                       "n": o["n"], "type": o["type"], "pct_dry": o["pct_dry"],
                       "verdict": o["verdict"],
                       "predicted_flow": _r4(o["predicted_flow"])}
                      for o in a["neighbors"]],
        # True when those neighbors do not agree on `type`. Derivable, and included
        # anyway for the same reason `small_n` is: it is the thing a reader should
        # lead with, and anything left to be derived is a thing everyone skips.
        "neighbors_disagree": a["neighbors_disagree"],
    }

def run_json(rows, notes, asof, do_pool, radius_km, n_harm, use_cache,
             precip=DEFAULT_PRECIP):
    """One object on stdout. Sources stay in input order -- sort client-side."""
    return {
        "asof": asof.isoformat(),
        # engine_version is here because a stored payload outlives the code that
        # made it. When one seep reads differently two months apart, this is what
        # separates "the seep changed" from "the method changed" -- and the method
        # HAS changed verdicts before, at identical input. `precip` is there for
        # the same reason one level down: same method, different rain, different
        # answer. Comparing two forecasts means checking both.
        "params": {"engine_version": __version__, "precip": precip,
                   "pool": do_pool, "pool_radius_km": radius_km,
                   "harmonics": n_harm, "cache": use_cache, "windows": WINDOWS},
        "sources": [source_json(a) for a in rows],
        "notes": notes,
    }

def _precip_arg(s):
    """Validate --precip at parse time, so an unknown product fails before ~19
    years of the WRONG one have been downloaded."""
    resolve_precip(s)
    return s

# Flags that take a value, as {flag: (option key, parser, what it wants)}. The
# parser is what turns the string into the option, and its name is what the user
# is told when it rejects one.
_VALUE_FLAGS = {
    "--asof":        ("asof",      date.fromisoformat, "an ISO date, e.g. 2026-08-15"),
    "--harmonics":   ("n_harm",    int,                "a whole number, e.g. 1"),
    "--pool-radius": ("radius_km", float,              "a distance in km, e.g. 25"),
    "--precip":      ("precip",    _precip_arg,
                      "a precip product: " + ", ".join(sorted(PRECIP_PROVIDERS))),
}
# Flags that are just on/off, as {flag: (option key, value when present)}.
_BOOL_FLAGS = {
    "--no-cache": ("use_cache", False),
    "--no-pool":  ("do_pool",   False),
    "--json":     ("as_json",   True),
    "--version":  ("version",   True),
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
            "as_json": False, "version": False, "n_harm": 1,
            "radius_km": POOL_RADIUS_KM, "precip": None}
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

# --------------------------------------------------------------------------- #
# The three passes, with no CLI attached
# ---------------------------------------------------------------------------
# This is the engine's actual behaviour, and both entry points go through it, so
# a host cannot end up running a copy that drifts. It drifted once: a service
# reimplementing these passes kept checking `analyze_base(...) is None` after the
# n == 0 case was added, and returned 500s on input the CLI handled fine.
# --------------------------------------------------------------------------- #
def _analyse(sources, asof, use_cache, n_harm, pool, radius_km, note, provider=None):
    """Pass 1 (per-source bases) -> pass 2 (pool) -> pass 3 (finalize). `note` is
    called as note(kind, message, source_name) for anything skipped or failed."""
    caveat = getattr(provider or PRECIP_PROVIDER, "caveat", None)
    if caveat:
        # Said once per run, not once per source, and before the first fetch -- it
        # is a property of the product the user asked for, not of any one spring.
        note("caveat", caveat)
    bases, ordered = [], []
    for src in sources:
        try:
            b = analyze_base(src, asof, use_cache, n_harm, provider)
            if b is None:
                note("skip", "no reports", src["name"])
                continue
            if b["n"] == 0:
                # No verdict -- but since #8 that is no longer the end of the story:
                # the source still gets its rain percentiles, so it stays in the
                # payload (in input order) instead of vanishing into the notes.
                #
                # A note only when reports were LOST. A coordinate-only row is the
                # user deliberately asking what rain alone can say, and calling that
                # a skip would train everyone to ignore the word.
                excl = excluded_note(b)
                if excl:
                    note("skip", excl + " -- no flow verdict, rain context only",
                         src["name"])
                ordered.append((False, b))
                continue
            bases.append(b)
            ordered.append((True, b))
        except Exception as e:
            note("error", str(e), src["name"])
    if pool and len(bases) > 1:                  # needs >=2 sources to borrow
        pool_controlled(bases, radius_km)
    rows = [finalize(b) if full else finalize_rain_only(b) for full, b in ordered]
    # A fourth pass, because a neighbor's entry quotes its VERDICT -- which does
    # not exist until every source has been through finalize(). Deliberately not
    # gated on `pool`: --no-pool means "don't borrow correlation", and saying what
    # is nearby is not borrowing. radius_km sets what counts as nearby for both.
    attach_neighbors(rows, radius_km)
    return rows

def run(sources, asof=None, *, harmonics=1, pool=True, pool_radius_km=POOL_RADIUS_KM,
        use_cache=True, precip=None, notes=None, on_note=None):
    """Run the engine over loaded sources and return the same dict --json prints.

    This is what main() does minus argument parsing and output, so an embedding
    host gets the engine's real behaviour -- including every future change to how
    sources are skipped or classified -- rather than a copy of these passes:

        sources = forecast.load_sources_from([io.StringIO(body)])
        payload = forecast.run(sources, date(2026, 8, 15))

    `asof` defaults to today. `precip` names one of PRECIP_PROVIDERS ("open-meteo",
    "iem:prism", "iem:mrms"); None means "whatever PRECIP_PROVIDER is", so a host
    that assigned its own callable keeps it. `notes` may be a list to append to
    (pre-seed it with anything that went wrong before this point, e.g. a rejected
    input file, and it will appear in the returned payload). `on_note` is called
    with each note dict as it happens, for a caller that wants to log or stream
    them."""
    asof = asof or date.today()
    provider = resolve_precip(precip) if precip else PRECIP_PROVIDER
    notes = notes if notes is not None else []
    def note(kind, msg, name=None):
        entry = {"kind": kind, "source": name, "message": msg}
        notes.append(entry)
        if on_note:
            on_note(entry)
    rows = _analyse(sources, asof, use_cache, harmonics, pool, pool_radius_km, note,
                    provider)
    return run_json(rows, notes, asof, pool, pool_radius_km, harmonics, use_cache,
                    precip_name(provider))

def main(argv):
    try:
        files, opts = parse_args(argv)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr); return 2
    if opts["version"]:
        print(f"backcountry-water-oracle {__version__}")
        return 0
    asof, use_cache, do_pool = opts["asof"], opts["use_cache"], opts["do_pool"]
    as_json, n_harm, radius_km = opts["as_json"], opts["n_harm"], opts["radius_km"]
    # No --precip means "whatever PRECIP_PROVIDER is" rather than "open-meteo", so
    # that a host importing this module and assigning its own provider still gets
    # it when it shells out to the CLI. parse_args already rejected an unknown
    # name, so resolve cannot raise here.
    provider = resolve_precip(opts["precip"]) if opts["precip"] else PRECIP_PROVIDER
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
            json.dump(run_json([], notes, asof, do_pool, radius_km, n_harm, use_cache,
                               precip_name(provider)), sys.stdout, indent=2)
            print()
        return 2
    # The analysis itself lives in _analyse(), shared with run(), so the CLI and an
    # embedding host cannot answer differently for the same input.
    rows = _analyse(sources, asof, use_cache, n_harm, do_pool, radius_km, note, provider)
    if as_json:
        json.dump(run_json(rows, notes, asof, do_pool, radius_km, n_harm, use_cache,
                           precip_name(provider)), sys.stdout, indent=2)
        print()
        return 0
    for a in rows:
        print_source(a)
    if len(rows) > 1:
        print_table(rows, precip_name(provider))
    return 0

def cli():
    """Console-script entry point. Separate from main() because console_scripts
    calls its target with NO arguments, while main() takes argv -- wiring the
    entry point straight to main would TypeError the first time anyone typed the
    installed command."""
    sys.exit(main(sys.argv[1:]))

if __name__ == "__main__":
    cli()
