#!/usr/bin/env python3
"""
Test suite for forecast.py
==========================
Run it:  python3 tests/test_forecast.py            (or: python3 -m unittest discover tests -t .)

Three rules this suite holds itself to, from issue #15:

  * STDLIB ONLY -- unittest, same dependency-free rule as the engine.
  * OFFLINE -- no test may touch the network. Live calls would be slow, rude to a
    free service, and ERA5 revisions would make the numbers flap. Every test that
    needs precipitation gets it from a committed fixture through PRECIP_PROVIDER,
    and urlopen is replaced with something that raises, so "did it fetch?" is
    never ambiguous.
  * DETERMINISTIC -- every test that reads an as-of date passes one explicitly.
    Nothing here may depend on today's date.

The CLI is exercised by calling main() in-process (capturing stdout/stderr and the
return code) rather than by spawning processes: it is far faster, and it is the
only way to keep the precip provider injected. One subprocess smoke test covers
"does this still run as a script at all".
"""
import contextlib, io, json, os, subprocess, sys, unittest, urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
# Imported under a short alias: the engine is one module, and every assertion
# below reads better as forecast.X than backcountry_water_oracle.X.
import backcountry_water_oracle as forecast                       # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
PRECIP_DIR = os.path.join(FIXTURES, "precip")
EXAMPLE_CSV = os.path.join(ROOT, "examples", "mazatzal-wilderness.csv")
ASOF = date(2026, 7, 13)          # the worked example's as-of, used throughout

# --------------------------------------------------------------------------- #
# Fixture precip: real ERA5 series for the three Mazatzal coordinates, stored as
# a start date plus the daily values (the dates are contiguous, so re-deriving
# them costs nothing and saves ~2/3 of the file size). They run to 2026-07-30,
# past the as-of the tests use, so the engine's trim path is always in play.
# --------------------------------------------------------------------------- #
_series_cache = {}

def load_fixture_series(lat, lon):
    key = (round(lat, 2), round(lon, 2))
    if key not in _series_cache:
        path = os.path.join(PRECIP_DIR, f"{key[0]}_{key[1]}.json")
        with open(path) as f:
            raw = json.load(f)
        start = date.fromisoformat(raw["start"])
        vals = raw["precip_in"]
        times = [(start + timedelta(days=i)).isoformat() for i in range(len(vals))]
        _series_cache[key] = {"daily": {"time": times, "precipitation_sum": vals}}
    d = _series_cache[key]["daily"]
    return {"daily": {"time": list(d["time"]), "precipitation_sum": list(d["precipitation_sum"])}}

def fixture_provider(lat, lon, end_date, use_cache=True):
    """Deliberately returns the WHOLE series regardless of end_date -- the engine
    is documented to trim, and every test run leans on that being true."""
    return load_fixture_series(lat, lon)

# What it serves is recorded ERA5 -- a different transport, not a different
# product -- so it says so, and every payload here is stamped the way a real run
# would be instead of naming this function (#17).
fixture_provider.precip_name = "open-meteo"

def _no_network(*a, **k):
    raise AssertionError("test attempted a network call")

class OfflineTestCase(unittest.TestCase):
    """Installs the fixture provider and makes any real fetch an error.

    RADAR_PROVIDER is switched OFF here rather than left at its default (#18). It
    defaults to a live IEM provider, and the radar check swallows every exception
    by design -- so leaving it on would let `_no_network` be caught and discarded,
    turning "this test tried to reach the internet" from a loud failure into a
    silent 6-second pause. Tests that want a radar read inject a stub."""
    def setUp(self):
        self._provider = forecast.PRECIP_PROVIDER
        self._radar = forecast.RADAR_PROVIDER
        self._urlopen = urllib.request.urlopen
        self._cache_dir = forecast.CACHE_DIR
        forecast.PRECIP_PROVIDER = fixture_provider
        forecast.RADAR_PROVIDER = None
        urllib.request.urlopen = _no_network

    def tearDown(self):
        forecast.PRECIP_PROVIDER = self._provider
        forecast.RADAR_PROVIDER = self._radar
        urllib.request.urlopen = self._urlopen
        forecast.CACHE_DIR = self._cache_dir

# --------------------------------------------------------------------------- #
# CLI harness
# --------------------------------------------------------------------------- #
class _Tty(io.StringIO):
    def isatty(self): return True

class _Pipe(io.StringIO):
    def isatty(self): return False

def run_cli(args, stdin_text=None):
    """Call main() with stdout/stderr captured. stdin_text=None means "a terminal",
    which is what makes the bare-invocation usage path reachable."""
    out, err = io.StringIO(), io.StringIO()
    fake_stdin = _Tty() if stdin_text is None else _Pipe(stdin_text)
    real_stdin, sys.stdin = sys.stdin, fake_stdin
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = forecast.main(args)
    finally:
        sys.stdin = real_stdin
    return code, out.getvalue(), err.getvalue()

def write_csv(tmpdir, name, rows, header="source,lat,lon,date,score"):
    path = os.path.join(tmpdir, name)
    with open(path, "w") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(r + "\n")
    return path

def source(name="S", lat=34.09, lon=-111.47, reports=None):
    return {"name": name, "lat": lat, "lon": lon, "reports": reports or []}


# =========================================================================== #
# CSV loading -- the input contract
# =========================================================================== #
class TestLoadSources(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_groups_rows_by_name(self):
        p = write_csv(self.tmp, "a.csv", [
            "Spring,34.09,-111.47,2024-01-01,1.0",
            "Spring,34.09,-111.47,2024-02-01,0.0",
            "Creek,34.09,-111.45,2024-01-01,0.5",
        ])
        srcs = {s["name"]: s for s in forecast.load_sources([p])}
        self.assertEqual(sorted(srcs), ["Creek", "Spring"])
        self.assertEqual(len(srcs["Spring"]["reports"]), 2)

    def test_reports_are_sorted_by_date(self):
        p = write_csv(self.tmp, "b.csv", [
            "S,34.09,-111.47,2024-06-01,0.5",
            "S,34.09,-111.47,2024-01-01,1.0",
            "S,34.09,-111.47,2024-03-01,0.2",
        ])
        dates = [d for d, _ in forecast.load_sources([p])[0]["reports"]]
        self.assertEqual(dates, sorted(dates))

    def test_scores_are_clamped_to_0_1(self):
        p = write_csv(self.tmp, "c.csv", [
            "S,34.09,-111.47,2024-01-01,5.0",
            "S,34.09,-111.47,2024-02-01,-2.0",
        ])
        scores = [v for _, v in forecast.load_sources([p])[0]["reports"]]
        self.assertEqual(scores, [1.0, 0.0])

    def test_blank_source_name_is_skipped(self):
        p = write_csv(self.tmp, "d.csv", [
            ",34.09,-111.47,2024-01-01,1.0",
            "S,34.09,-111.47,2024-02-01,0.5",
        ])
        self.assertEqual(len(forecast.load_sources([p])), 1)

    def test_missing_columns_name_every_one(self):
        p = write_csv(self.tmp, "e.csv", ["1,2"], header="a,b")
        with self.assertRaises(ValueError) as cm:
            forecast.load_sources([p])
        msg = str(cm.exception)
        for col in ("source", "lat", "lon", "date", "score"):
            self.assertIn(col, msg)

    def test_empty_input_says_so(self):
        p = os.path.join(self.tmp, "empty.csv")
        open(p, "w").close()
        with self.assertRaises(ValueError) as cm:
            forecast.load_sources([p])
        self.assertIn("empty input", str(cm.exception))

    def test_extra_columns_are_ignored(self):
        p = write_csv(self.tmp, "f.csv", ["S,34.09,-111.47,2024-01-01,1.0,wet,xyz"],
                      header="source,lat,lon,date,score,status,junk")
        self.assertEqual(len(forecast.load_sources([p])[0]["reports"]), 1)

    def test_several_files_merge_into_one_source(self):
        a = write_csv(self.tmp, "g1.csv", ["S,34.09,-111.47,2024-01-01,1.0"])
        b = write_csv(self.tmp, "g2.csv", ["S,34.09,-111.47,2024-02-01,0.5"])
        srcs = forecast.load_sources([a, b])
        self.assertEqual(len(srcs), 1)
        self.assertEqual(len(srcs[0]["reports"]), 2)


class TestCoordinateOnlyRows(unittest.TestCase):
    """A row with date and score blank is a pin, not an observation (#8)."""
    def load(self, text):
        return forecast.load_sources_from([io.StringIO(text)], labels=["<t>"])

    def test_a_blank_row_registers_a_source_with_no_reports(self):
        srcs = self.load("source,lat,lon,date,score\nPin,34.09,-111.47,,\n")
        self.assertEqual(len(srcs), 1)
        self.assertEqual(srcs[0]["name"], "Pin")
        self.assertEqual(srcs[0]["lat"], 34.09)
        self.assertEqual(srcs[0]["reports"], [])

    def test_whitespace_counts_as_blank(self):
        srcs = self.load('source,lat,lon,date,score\nPin,34.09,-111.47,"  ","  "\n')
        self.assertEqual(srcs[0]["reports"], [])

    def test_a_pin_and_real_reports_can_share_a_name(self):
        srcs = self.load("source,lat,lon,date,score\n"
                         "S,34.09,-111.47,,\n"
                         "S,34.09,-111.47,2024-01-01,1.0\n")
        self.assertEqual(len(srcs), 1)
        self.assertEqual(len(srcs[0]["reports"]), 1)

    def test_a_date_with_no_score_is_a_typo_not_a_pin(self):
        with self.assertRaises(ValueError) as cm:
            self.load("source,lat,lon,date,score\nS,34.09,-111.47,2024-01-01,\n")
        self.assertIn("line 2", str(cm.exception))
        self.assertIn("no score", str(cm.exception))

    def test_a_score_with_no_date_is_a_typo_too(self):
        with self.assertRaises(ValueError) as cm:
            self.load("source,lat,lon,date,score\nS,34.09,-111.47,,0.5\n")
        self.assertIn("no date", str(cm.exception))

    def test_the_error_says_how_to_ask_for_a_pin(self):
        with self.assertRaises(ValueError) as cm:
            self.load("source,lat,lon,date,score\nS,34.09,-111.47,,0.5\n")
        self.assertIn("BOTH blank", str(cm.exception))

    def test_a_short_row_is_still_a_pin_not_a_crash(self):
        """DictReader fills missing trailing fields with None, not ''."""
        srcs = self.load("source,lat,lon,date,score\nPin,34.09,-111.47\n")
        self.assertEqual(srcs[0]["reports"], [])


class TestCoordinateConflicts(unittest.TestCase):
    """Issue #9: the first row's coordinates used to win, silently."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_far_apart_rows_sharing_a_name_are_rejected(self):
        p = write_csv(self.tmp, "dup.csv", [
            "Dup,34.0,-111.0,2024-01-01,0.5",
            "Dup,44.0,-121.0,2024-02-01,0.2",
        ])
        with self.assertRaises(ValueError) as cm:
            forecast.load_sources([p])
        msg = str(cm.exception)
        self.assertIn("Dup", msg)
        self.assertIn("conflicting coordinates", msg)
        self.assertIn("line 3", msg)                 # points at the offending row
        self.assertIn("34.00000", msg)               # and shows both positions
        self.assertIn("44.00000", msg)

    def test_gps_scatter_within_tolerance_is_one_source(self):
        p = write_csv(self.tmp, "scatter.csv", [
            "Seep,34.09059,-111.46653,2024-01-01,0.5",
            "Seep,34.09102,-111.46701,2024-02-01,0.2",
            "Seep,34.09011,-111.46600,2024-03-01,0.9",
        ])
        srcs = forecast.load_sources([p])
        self.assertEqual(len(srcs), 1)
        self.assertEqual(len(srcs[0]["reports"]), 3)
        self.assertEqual(srcs[0]["lat"], 34.09059)   # first row's coords kept
        self.assertEqual(srcs[0]["lon"], -111.46653)

    def test_tolerance_boundary(self):
        """Just inside passes, just outside fails -- pins COORD_TOLERANCE_KM."""
        km_per_deg_lon = forecast._haversine_km(34.0, -111.0, 34.0, -110.0)
        inside = forecast.COORD_TOLERANCE_KM * 0.9 / km_per_deg_lon
        outside = forecast.COORD_TOLERANCE_KM * 1.1 / km_per_deg_lon
        ok = write_csv(self.tmp, "in.csv", [
            f"E,34.0,-111.0,2024-01-01,0.5", f"E,34.0,{-111.0 + inside},2024-02-01,0.2"])
        self.assertEqual(len(forecast.load_sources([ok])), 1)
        bad = write_csv(self.tmp, "out.csv", [
            f"E,34.0,-111.0,2024-01-01,0.5", f"E,34.0,{-111.0 + outside},2024-02-01,0.2"])
        with self.assertRaises(ValueError):
            forecast.load_sources([bad])

    def test_different_names_at_the_same_spot_are_fine(self):
        p = write_csv(self.tmp, "same.csv", [
            "A,34.09,-111.47,2024-01-01,0.5",
            "B,34.09,-111.47,2024-02-01,0.2",
        ])
        self.assertEqual(len(forecast.load_sources([p])), 2)


class TestLoadSourcesFrom(unittest.TestCase):
    """Issue #24: the supported way to load CSV that never touched the filesystem.

    The site previously called the private _read_csv() and had to remember the
    reports.sort() itself -- a path nothing here covered, so a refactor could have
    broken it with every test still green."""
    CSV = ("source,lat,lon,date,score\n"
           "S,34.09,-111.47,2024-06-01,0.5\n"
           "S,34.09,-111.47,2024-01-01,1.0\n")

    def test_loads_from_a_stream(self):
        srcs = forecast.load_sources_from([io.StringIO(self.CSV)])
        self.assertEqual(len(srcs), 1)
        self.assertEqual(len(srcs[0]["reports"]), 2)

    def test_reports_come_back_sorted(self):
        """The promise the caller used to owe. The fixture is deliberately out of
        order, so an unsorted result fails here rather than silently skewing a
        window sum somewhere downstream."""
        srcs = forecast.load_sources_from([io.StringIO(self.CSV)])
        dates = [d for d, _ in srcs[0]["reports"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(dates[0], date(2024, 1, 1))

    def test_several_streams_merge(self):
        a = io.StringIO(self.CSV)
        b = io.StringIO("source,lat,lon,date,score\nT,34.09,-111.45,2024-03-01,0.2\n")
        srcs = forecast.load_sources_from([a, b])
        self.assertEqual(sorted(s["name"] for s in srcs), ["S", "T"])

    def test_labels_name_the_stream_in_errors(self):
        bad = io.StringIO("a,b\n1,2\n")
        with self.assertRaises(ValueError) as cm:
            forecast.load_sources_from([bad], labels=["<request body>"])
        self.assertIn("<request body>", str(cm.exception))

    def test_unlabelled_streams_get_a_usable_default(self):
        with self.assertRaises(ValueError) as cm:
            forecast.load_sources_from([io.StringIO(self.CSV), io.StringIO("a,b\n1,2\n")])
        self.assertIn("<stream 2>", str(cm.exception))

    def test_a_single_label_may_be_a_bare_string(self):
        with self.assertRaises(ValueError) as cm:
            forecast.load_sources_from([io.StringIO("a,b\n")], labels="<body>")
        self.assertIn("<body>", str(cm.exception))

    def test_a_bare_stream_is_not_mistaken_for_a_list_of_streams(self):
        """Iterating a stream yields LINES; without the guard each line would be
        treated as its own CSV and the failure would be baffling."""
        srcs = forecast.load_sources_from(io.StringIO(self.CSV))
        self.assertEqual(len(srcs), 1)
        self.assertEqual(len(srcs[0]["reports"]), 2)

    def test_same_validation_as_the_file_path(self):
        for bad, expect in [("a,b\n1,2\n", "missing column"), ("", "empty input")]:
            with self.assertRaises(ValueError) as cm:
                forecast.load_sources_from([io.StringIO(bad)])
            self.assertIn(expect, str(cm.exception))

    def test_coordinate_conflicts_are_caught_here_too(self):
        conflict = ("source,lat,lon,date,score\n"
                    "D,34.0,-111.0,2024-01-01,0.5\nD,44.0,-121.0,2024-02-01,0.2\n")
        with self.assertRaises(ValueError) as cm:
            forecast.load_sources_from([io.StringIO(conflict)])
        self.assertIn("conflicting coordinates", str(cm.exception))

    def test_matches_load_sources_exactly(self):
        """One code path, so the two entry points cannot drift apart."""
        from_file = forecast.load_sources([EXAMPLE_CSV])
        with open(EXAMPLE_CSV) as f:
            from_stream = forecast.load_sources_from([f])
        self.assertEqual(from_stream, from_file)


class TestStdin(unittest.TestCase):
    """Issue #5: `-` reads the CSV from stdin."""
    CSV = ("source,lat,lon,date,score\n"
           "S,34.09,-111.47,2024-01-01,1.0\n"
           "S,34.09,-111.47,2024-02-01,0.0\n")

    def test_dash_reads_stdin(self):
        real, sys.stdin = sys.stdin, _Pipe(self.CSV)
        try:
            srcs = forecast.load_sources(["-"])
        finally:
            sys.stdin = real
        self.assertEqual(len(srcs), 1)
        self.assertEqual(len(srcs[0]["reports"]), 2)

    def test_repeated_dash_is_ignored_not_an_error(self):
        real, sys.stdin = sys.stdin, _Pipe(self.CSV)
        try:
            srcs = forecast.load_sources(["-", "-"])
        finally:
            sys.stdin = real
        self.assertEqual(len(srcs[0]["reports"]), 2)      # not doubled, not empty

    def test_file_and_stdin_merge(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        p = write_csv(tmp, "f.csv", ["Other,34.09,-111.45,2024-01-01,0.5"])
        real, sys.stdin = sys.stdin, _Pipe(self.CSV)
        try:
            srcs = forecast.load_sources([p, "-"])
        finally:
            sys.stdin = real
        self.assertEqual(sorted(s["name"] for s in srcs), ["Other", "S"])


# =========================================================================== #
# Argument parsing -- issue #11
# =========================================================================== #
class TestParseArgs(unittest.TestCase):
    def test_defaults(self):
        files, opts = forecast.parse_args(["a.csv"])
        self.assertEqual(files, ["a.csv"])
        self.assertTrue(opts["use_cache"])
        self.assertTrue(opts["do_pool"])
        self.assertEqual(opts["fmt"], "text")
        self.assertEqual(opts["n_harm"], 1)
        self.assertEqual(opts["radius_km"], forecast.POOL_RADIUS_KM)

    def test_both_spellings_agree(self):
        for argv in (["a.csv", "--asof", "2026-07-13"], ["a.csv", "--asof=2026-07-13"]):
            files, opts = forecast.parse_args(argv)
            self.assertEqual(files, ["a.csv"])
            self.assertEqual(opts["asof"], date(2026, 7, 13))

    def test_bool_flags(self):
        _, opts = forecast.parse_args(["a.csv", "--no-cache", "--no-pool"])
        self.assertFalse(opts["use_cache"])
        self.assertFalse(opts["do_pool"])

    def test_format_defaults_to_text(self):
        _, opts = forecast.parse_args(["a.csv"])
        self.assertEqual(opts["fmt"], "text")

    def test_format_accepts_every_documented_name(self):
        """Both spellings, because --format=json and --format json are both in the
        README and a value flag that only works one way is a bug people hit once."""
        for name in forecast.FORMATS:
            for argv in (["a.csv", "--format", name], ["a.csv", f"--format={name}"]):
                _, opts = forecast.parse_args(argv)
                self.assertEqual(opts["fmt"], name, argv)

    def test_unknown_format_is_rejected_with_the_valid_set(self):
        with self.assertRaises(ValueError) as cm:
            forecast.parse_args(["a.csv", "--format", "html"])
        msg = str(cm.exception)
        self.assertIn("html", msg)
        for name in forecast.FORMATS:
            self.assertIn(name, msg)

    def test_retired_json_flag_says_what_to_type_instead(self):
        """#20 replaced --json with --format json. A retired flag falling through
        to "unknown flag" sends someone hunting for a typo in a flag they spelled
        correctly for two releases -- and --json was the documented way to script
        this engine, so every pipeline that exists is holding it."""
        for argv in (["a.csv", "--json"], ["a.csv", "--json=yes"]):
            with self.assertRaises(ValueError) as cm:
                forecast.parse_args(argv)
            msg = str(cm.exception)
            self.assertIn("--json", msg)
            self.assertIn("--format json", msg)
            self.assertNotIn("unknown flag", msg)

    def test_value_flag_in_final_position_errors(self):
        """The #11 crash: this used to raise IndexError and dump a traceback."""
        for flag in ("--asof", "--harmonics", "--pool-radius"):
            with self.assertRaises(ValueError) as cm:
                forecast.parse_args(["a.csv", flag])
            self.assertIn("requires a value", str(cm.exception))
            self.assertIn(flag, str(cm.exception))

    def test_unparseable_value_errors(self):
        for argv in (["--asof", "tomorrow"], ["--harmonics", "two"],
                     ["--pool-radius", "wide"]):
            with self.assertRaises(ValueError) as cm:
                forecast.parse_args(["a.csv"] + argv)
            self.assertIn("expected", str(cm.exception))

    def test_unknown_flag_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            forecast.parse_args(["a.csv", "--no-poo"])
        self.assertIn("unknown flag", str(cm.exception))

    def test_bool_flag_given_a_value_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            forecast.parse_args(["a.csv", "--no-pool=yes"])
        self.assertIn("takes no value", str(cm.exception))

    def test_a_file_may_be_named_like_a_flag_value(self):
        """What the old identity check protected, now true by construction."""
        files, opts = forecast.parse_args(["--asof", "2026-07-13", "2026-07-13"])
        self.assertEqual(files, ["2026-07-13"])
        self.assertEqual(opts["asof"], date(2026, 7, 13))

    def test_dash_is_a_file_not_a_flag(self):
        files, _ = forecast.parse_args(["-", "--format", "json"])
        self.assertEqual(files, ["-"])

    def test_several_files(self):
        files, _ = forecast.parse_args(["a.csv", "--format", "json", "b.csv"])
        self.assertEqual(files, ["a.csv", "b.csv"])


# =========================================================================== #
# The precip provider seam -- issue #7
# =========================================================================== #
class TestPrecipProvider(OfflineTestCase):
    def test_injected_provider_drives_the_engine(self):
        calls = []
        def provider(lat, lon, end_date, use_cache=True):
            calls.append((round(lat, 2), round(lon, 2), end_date, use_cache))
            return load_fixture_series(lat, lon)
        forecast.PRECIP_PROVIDER = provider
        srcs = forecast.load_sources([EXAMPLE_CSV])
        rows = [forecast.analyze(s, ASOF) for s in srcs]
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(r is not None for r in rows))
        self.assertTrue(all(c[2] == ASOF for c in calls))

    def test_engine_trims_an_over_long_series(self):
        """A host may keep one long series and serve every as-of from it; the read
        must not depend on how much extra it returned."""
        def exact(lat, lon, end_date, use_cache=True):
            full = load_fixture_series(lat, lon)
            return forecast._trim_daily(full, end_date)
        srcs = forecast.load_sources([EXAMPLE_CSV])
        long_rows = [forecast.analyze(s, ASOF) for s in srcs]      # fixture: full series
        forecast.PRECIP_PROVIDER = exact
        exact_rows = [forecast.analyze(s, ASOF) for s in srcs]
        for a, b in zip(long_rows, exact_rows):
            self.assertEqual(a["best"], b["best"])
            self.assertEqual(a["pred"], b["pred"])
            self.assertEqual(a["curval"], b["curval"])
            self.assertEqual(a["ctrl_cors"], b["ctrl_cors"])

    def test_malformed_providers_fail_at_the_seam(self):
        cases = {
            "returns None":    lambda *a, **k: None,
            "no daily key":    lambda *a, **k: {"hourly": {}},
            "length mismatch": lambda *a, **k: {"daily": {"time": ["2007-01-01", "2007-01-02"],
                                                          "precipitation_sum": [0.1]}},
            "empty series":    lambda *a, **k: {"daily": {"time": [], "precipitation_sum": []}},
            "not a dict":      lambda *a, **k: [1, 2, 3],
        }
        src = source(reports=[(date(2024, 1, 1), 1.0)])
        for label, bad in cases.items():
            with self.subTest(label):
                forecast.PRECIP_PROVIDER = bad
                with self.assertRaises(ValueError) as cm:
                    forecast.analyze(src, ASOF)
                self.assertIn("precip provider", str(cm.exception))

    def test_provider_receives_the_lagged_end_date(self):
        """The engine never asks for data ERA5 cannot have yet."""
        seen = []
        def provider(lat, lon, end_date, use_cache=True):
            seen.append(end_date)
            return load_fixture_series(lat, lon)
        forecast.PRECIP_PROVIDER = provider
        far_future = date.today() + timedelta(days=365)
        forecast.analyze(source(reports=[(date(2024, 1, 1), 1.0)]), far_future)
        self.assertLessEqual(seen[0], date.today() - timedelta(days=forecast.ERA5_LAG_DAYS))


# =========================================================================== #
# Choosing a product by name -- issue #17
# =========================================================================== #
class TestPrecipSelection(OfflineTestCase):
    """--precip / run(precip=...) select among the built-ins; ERA5 stays default."""
    def test_the_registry_has_the_documented_names(self):
        self.assertEqual(sorted(forecast.PRECIP_PROVIDERS),
                         ["iem:mrms", "iem:prism", "open-meteo"])
        self.assertEqual(forecast.DEFAULT_PRECIP, "open-meteo")

    def test_the_default_is_still_era5(self):
        """The one guarantee the issue makes about this whole change."""
        self.assertIs(forecast.PRECIP_PROVIDERS[forecast.DEFAULT_PRECIP],
                      forecast.open_meteo_provider)

    def test_resolve_returns_the_callable(self):
        self.assertIs(forecast.resolve_precip("iem:mrms"), forecast.iem_mrms_provider)

    def test_resolve_lists_what_it_knows(self):
        with self.assertRaises(ValueError) as cm:
            forecast.resolve_precip("prism")           # nearly right, still wrong
        for name in forecast.PRECIP_PROVIDERS:
            self.assertIn(name, str(cm.exception))

    def test_run_selects_the_named_provider(self):
        seen = []
        def fake(lat, lon, end_date, use_cache=True):
            seen.append((lat, lon))
            return load_fixture_series(lat, lon)
        forecast.PRECIP_PROVIDERS["test:fake"] = fake
        try:
            payload = forecast.run(forecast.load_sources([EXAMPLE_CSV]), ASOF,
                                   precip="test:fake")
        finally:
            del forecast.PRECIP_PROVIDERS["test:fake"]
        self.assertEqual(len(seen), 3)
        self.assertEqual(payload["params"]["precip"], "test:fake")

    def test_selecting_a_product_does_not_mutate_the_global(self):
        """Threaded through the call chain, not swapped into PRECIP_PROVIDER for
        the length of the run -- two concurrent requests must not cross rain."""
        forecast.PRECIP_PROVIDERS["test:fake"] = fixture_provider
        try:
            forecast.run(forecast.load_sources([EXAMPLE_CSV]), ASOF, precip="test:fake")
        finally:
            del forecast.PRECIP_PROVIDERS["test:fake"]
        self.assertIs(forecast.PRECIP_PROVIDER, fixture_provider)

    def test_no_selection_leaves_an_injected_provider_alone(self):
        """A host that assigned PRECIP_PROVIDER keeps it, through run() and the CLI."""
        payload = forecast.run(forecast.load_sources([EXAMPLE_CSV]), ASOF)
        self.assertEqual(len(payload["sources"]), 3)          # the fixture answered
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        self.assertEqual(len(json.loads(out)["sources"]), 3)

    def test_an_unknown_product_is_rejected_before_anything_is_fetched(self):
        calls = []
        forecast.PRECIP_PROVIDER = lambda *a, **k: calls.append(1)
        code, out, err = run_cli([EXAMPLE_CSV, "--precip", "iem:prims"])
        self.assertEqual(code, 2)
        self.assertIn("[error]", err)
        self.assertIn("iem:mrms", err)                        # tells you the real names
        self.assertEqual(calls, [])

    def test_run_rejects_an_unknown_product(self):
        with self.assertRaises(ValueError):
            forecast.run(forecast.load_sources([EXAMPLE_CSV]), ASOF, precip="nope")

    # -- what the payload says answered ------------------------------------- #
    def test_a_built_in_is_named_by_its_registry_key(self):
        self.assertEqual(forecast.precip_name(forecast.open_meteo_provider), "open-meteo")
        self.assertEqual(forecast.precip_name(forecast.iem_prism_provider), "iem:prism")

    def test_a_host_callable_is_never_mislabelled_as_era5(self):
        def my_provider(lat, lon, end_date, use_cache=True): ...
        self.assertEqual(forecast.precip_name(my_provider), "my_provider")

    def test_a_provider_may_declare_the_product_it_serves(self):
        """ERA5 out of the host's own store is still ERA5 -- and stays comparable."""
        def from_postgres(lat, lon, end_date, use_cache=True): ...
        from_postgres.precip_name = "open-meteo"
        self.assertEqual(forecast.precip_name(from_postgres), "open-meteo")

    def test_the_payload_is_stamped_with_what_answered(self):
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        self.assertEqual(json.loads(out)["params"]["precip"], "open-meteo")

    # -- the caveat that rides along with a product -------------------------- #
    def test_mrms_warns_that_the_early_record_is_not_radar(self):
        notes = []
        forecast._analyse([], ASOF, True, 1, True, 25.0,
                          lambda k, m, n=None: notes.append((k, m)),
                          forecast.iem_mrms_provider)
        self.assertEqual([k for k, _ in notes], ["caveat"])
        self.assertIn("2014", notes[0][1])

    def test_the_default_product_has_nothing_to_caveat(self):
        notes = []
        forecast._analyse([], ASOF, True, 1, True, 25.0,
                          lambda k, m, n=None: notes.append((k, m)),
                          forecast.open_meteo_provider)
        self.assertEqual(notes, [])

    def test_the_caveat_reaches_the_payload(self):
        forecast.PRECIP_PROVIDER = forecast.iem_prism_provider
        payload = forecast.run([], ASOF)
        self.assertEqual(payload["notes"][0]["kind"], "caveat")
        self.assertIsNone(payload["notes"][0]["source"])       # a run-wide fact

    def test_the_summary_footer_stops_claiming_era5(self):
        """The standing 'ERA5 misses monsoon cells' line is false under --precip."""
        rows = forecast._analyse(forecast.load_sources([EXAMPLE_CSV]), ASOF, True, 1,
                                 True, 25.0, lambda *a: None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            forecast.print_table(rows, "iem:mrms")
        self.assertIn("iem:mrms", out.getvalue())
        self.assertNotIn("Reminder: ERA5", out.getvalue())


class TestIemProvider(unittest.TestCase):
    """The IEM built-ins: CONUS-only, year-chunked, cached (issue #17)."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._cache_dir, self._urlopen = forecast.CACHE_DIR, urllib.request.urlopen
        self._start = forecast.PRECIP_START
        self._pause = forecast.IEM_PAUSE_S
        self._radar = forecast.RADAR_PROVIDER
        forecast.RADAR_PROVIDER = None      # tested on its own, below
        forecast.CACHE_DIR = self.tmp
        forecast.PRECIP_START = "2024-01-01"        # keep the fixture years small
        forecast.IEM_PAUSE_S = 0                    # no politeness sleep in tests
        self.fetched = []

        def stub(url, timeout=0):
            self.fetched.append(url)
            d1, d2 = (date.fromisoformat(p) for p in url.split("/multiday/")[1].split("/")[:2])
            rows = [{"date": (d1 + timedelta(days=i)).isoformat(),
                     "prism_precip_in": round((i % 11) * 0.01, 3),
                     "mrms_precip_in": round((i % 7) * 0.02, 3)}
                    for i in range((d2 - d1).days + 1)]
            return io.BytesIO(json.dumps({"data": rows}).encode())
        urllib.request.urlopen = stub

    def tearDown(self):
        forecast.CACHE_DIR, urllib.request.urlopen = self._cache_dir, self._urlopen
        forecast.PRECIP_START, forecast.IEM_PAUSE_S = self._start, self._pause
        forecast.RADAR_PROVIDER = self._radar

    def test_outside_conus_is_an_error_not_a_silent_fallback(self):
        """The whole point: you asked for PRISM, you get PRISM or you get told."""
        for lat, lon, where in [(46.8, 8.2, "the Alps"), (-33.9, 18.4, "Cape Town")]:
            with self.subTest(where):
                with self.assertRaises(ValueError) as cm:
                    forecast.iem_prism_provider(lat, lon, date(2024, 6, 1))
                self.assertIn("CONUS", str(cm.exception))
        self.assertEqual(self.fetched, [])           # and it never asked the service

    def test_a_bad_coordinate_skips_one_source_and_spares_the_rest(self):
        payload = forecast.run(
            [source("Swiss", 46.8, 8.2, [(date(2024, 6, 1), 1.0)])],
            date(2024, 8, 1), precip="iem:prism")
        self.assertEqual(payload["sources"], [])
        errs = [n for n in payload["notes"] if n["kind"] == "error"]
        self.assertEqual(errs[0]["source"], "Swiss")
        self.assertIn("CONUS", errs[0]["message"])
        # ...and nothing anywhere claims ERA5 quietly stepped in
        self.assertEqual(payload["params"]["precip"], "iem:prism")

    def test_nulls_across_the_whole_record_are_no_coverage_not_a_dry_site(self):
        def empty(url, timeout=0):
            d1, d2 = (date.fromisoformat(p) for p in url.split("/multiday/")[1].split("/")[:2])
            return io.BytesIO(json.dumps({"data": [
                {"date": (d1 + timedelta(days=i)).isoformat(),
                 "prism_precip_in": None, "mrms_precip_in": None}
                for i in range((d2 - d1).days + 1)]}).encode())
        urllib.request.urlopen = empty
        with self.assertRaises(ValueError) as cm:
            forecast.iem_prism_provider(34.09, -111.47, date(2024, 6, 1))
        self.assertIn("no data", str(cm.exception))

    def test_series_is_chunked_by_year_and_starts_at_precip_start(self):
        s = forecast.iem_mrms_provider(34.09, -111.47, date(2025, 3, 5))
        self.assertEqual(len(self.fetched), 2)                  # 2024 and 2025
        self.assertEqual(s["daily"]["time"][0], "2024-01-01")
        self.assertEqual(s["daily"]["time"][-1], "2025-03-05")
        self.assertEqual(len(s["daily"]["time"]), len(s["daily"]["precipitation_sum"]))

    def test_both_products_come_from_one_download(self):
        forecast.iem_prism_provider(34.09, -111.47, date(2024, 6, 1))
        n = len(self.fetched)
        mrms = forecast.iem_mrms_provider(34.09, -111.47, date(2024, 6, 1))
        self.assertEqual(len(self.fetched), n)                  # served from cache
        prism = forecast.iem_prism_provider(34.09, -111.47, date(2024, 6, 1))
        self.assertNotEqual(mrms["daily"]["precipitation_sum"],
                            prism["daily"]["precipitation_sum"])

    def test_a_cached_year_is_not_refetched(self):
        forecast.iem_prism_provider(34.09, -111.47, date(2024, 12, 31))
        n = len(self.fetched)
        forecast.iem_prism_provider(34.09, -111.47, date(2024, 12, 31))
        self.assertEqual(len(self.fetched), n)

    def test_a_part_year_cache_is_refetched_when_more_is_asked_for(self):
        """The current year is cached mid-flight; trusting the file's existence
        would silently truncate every window reaching past what it holds."""
        forecast.iem_prism_provider(34.09, -111.47, date(2024, 6, 1))
        n = len(self.fetched)
        s = forecast.iem_prism_provider(34.09, -111.47, date(2024, 9, 1))
        self.assertEqual(len(self.fetched), n + 1)
        self.assertEqual(s["daily"]["time"][-1], "2024-09-01")

    def test_no_cache_bypasses_the_cache(self):
        forecast.iem_prism_provider(34.09, -111.47, date(2024, 6, 1))
        n = len(self.fetched)
        forecast.iem_prism_provider(34.09, -111.47, date(2024, 6, 1), use_cache=False)
        self.assertEqual(len(self.fetched), n + 1)

    def test_an_unreadable_cache_file_is_a_miss_not_a_crash(self):
        forecast.iem_prism_provider(34.09, -111.47, date(2024, 6, 1))
        with open(forecast._iem_cache_path(34.09, -111.47, 2024), "w") as f:
            f.write("{not json")
        n = len(self.fetched)
        forecast.iem_prism_provider(34.09, -111.47, date(2024, 6, 1))
        self.assertEqual(len(self.fetched), n + 1)

    def test_cache_key_rounds_like_the_era5_one(self):
        p = forecast._iem_cache_path(34.087161, -111.452934, 2024)
        self.assertEqual(os.path.basename(p), "34.09_-111.45_2024.json")
        self.assertEqual(os.path.basename(os.path.dirname(p)), "iem")

    def test_the_series_satisfies_the_provider_contract(self):
        """Whatever it returns has to survive the same seam a host's does."""
        src = source(lat=34.09, lon=-111.47, reports=[(date(2024, 6, 1), 1.0),
                                                      (date(2024, 9, 1), 0.0)])
        a = forecast.analyze(src, date(2025, 3, 5), provider=forecast.iem_mrms_provider)
        self.assertIsNotNone(a)
        self.assertEqual(a["n"], 2)


# =========================================================================== #
# The radar cross-check -- issue #18
# =========================================================================== #
def constant_radar(per_day):
    """A radar provider whose every day holds `per_day` inches, so a 30d window is
    exactly 30 * per_day and a ratio can be checked by hand."""
    def provider(lat, lon, end_date, use_cache=True):
        start = date.fromisoformat(forecast.PRECIP_START)
        n = (end_date - start).days + 1
        return {"daily": {"time": [(start + timedelta(days=i)).isoformat()
                                   for i in range(n)],
                          "precipitation_sum": [per_day] * n}}
    provider.precip_name = "test:radar"
    return provider


class TestRadarCheck(OfflineTestCase):
    WET = staticmethod(constant_radar(0.1))          # 3.0" per 30d, 6.0" per 60d

    def payload(self, radar=None, csv=None, **kw):
        forecast.RADAR_PROVIDER = radar
        body = csv if csv is not None else open(EXAMPLE_CSV).read()
        return forecast.run(forecast.load_sources_from([io.StringIO(body)]), ASOF, **kw)

    def test_it_reports_both_short_windows_and_only_those(self):
        rc = self.payload(self.WET)["sources"][0]["radar_check"]
        self.assertEqual(sorted(int(w[:-1]) for w in rc["windows"]), [30, 60])
        self.assertEqual(sorted(forecast.RADAR_WINDOWS), [30, 60])

    def test_the_numbers_are_the_radar_series_not_the_model(self):
        src = self.payload(self.WET)["sources"][0]
        rc = src["radar_check"]
        self.assertAlmostEqual(rc["windows"]["30d"]["radar_in"], 3.0, places=3)
        self.assertAlmostEqual(rc["windows"]["60d"]["radar_in"], 6.0, places=3)
        # ...while the model column is the fitted series, read at the same date the
        # verdict was read at -- so the two columns are genuinely comparable.
        rain = src["rain_percentiles"]
        self.assertAlmostEqual(rc["windows"]["30d"]["model_in"], rain["30d"]["inches"])
        self.assertAlmostEqual(rc["windows"]["60d"]["model_in"], rain["60d"]["inches"])

    def test_the_ratio_is_radar_over_model(self):
        rc = self.payload(self.WET)["sources"][0]["radar_check"]
        for w in ("30d", "60d"):
            v = rc["windows"][w]
            self.assertAlmostEqual(v["ratio_to_model"],
                                   round(v["radar_in"] / v["model_in"], 2), places=2)

    def test_the_product_is_named(self):
        self.assertEqual(self.payload(self.WET)["sources"][0]["radar_check"]["product"],
                         "test:radar")

    def test_params_records_what_cross_checked(self):
        self.assertEqual(self.payload(self.WET)["params"]["radar"], "test:radar")
        self.assertEqual(self.payload(None)["params"]["radar"], "none")

    def test_IT_DOES_NOT_MOVE_THE_VERDICT(self):
        """The load-bearing guarantee of #18: strictly additive. The analog pool is
        built from the model's own windows, so a radar number must never enter it."""
        without = self.payload(None)
        with_radar = self.payload(self.WET)
        self.assertIsNone(without["sources"][0]["radar_check"])
        self.assertIsNotNone(with_radar["sources"][0]["radar_check"])
        for a, b in zip(without["sources"], with_radar["sources"]):
            self.assertEqual({k: v for k, v in a.items() if k != "radar_check"},
                             {k: v for k, v in b.items() if k != "radar_check"})

    def test_a_model_reading_of_zero_gives_no_ratio(self):
        """The headline case -- ERA5 0.04" against MRMS 3.66" -- has a denominator
        that rounds to nothing. A ratio there would be arbitrary, so there isn't one."""
        dry = constant_radar(0.0)
        dry.precip_name = "test:dry-model"
        forecast.PRECIP_PROVIDER = dry                       # the FIT sees no rain
        rc = self.payload(self.WET)["sources"][0]["radar_check"]
        self.assertEqual(rc["windows"]["30d"]["model_in"], 0.0)
        self.assertIsNone(rc["windows"]["30d"]["ratio_to_model"])
        self.assertGreater(rc["windows"]["30d"]["radar_in"], 0)

    def test_a_failing_radar_provider_costs_a_line_not_a_source(self):
        def boom(lat, lon, end_date, use_cache=True):
            raise RuntimeError("IEM is having a day")
        payload = self.payload(boom)
        self.assertEqual(len(payload["sources"]), 3)          # every source survived
        self.assertIsNone(payload["sources"][0]["radar_check"])
        self.assertEqual(payload["notes"], [])                # and it stayed quiet

    def test_a_malformed_radar_provider_is_also_soft(self):
        payload = self.payload(lambda *a, **k: {"nonsense": True})
        self.assertEqual(len(payload["sources"]), 3)
        self.assertIsNone(payload["sources"][0]["radar_check"])

    def test_cross_checking_the_fit_against_itself_is_skipped(self):
        """--precip iem:mrms IS the radar read; a ratio of 1.0 is not a check."""
        forecast.PRECIP_PROVIDER = fixture_provider
        payload = self.payload(fixture_provider)
        self.assertIsNone(payload["sources"][0]["radar_check"])

    def test_a_source_with_no_reports_gets_one_too(self):
        got = self.payload(self.WET, csv="source,lat,lon,date,score\n"
                                         "Pin,34.09,-111.47,,\n")["sources"][0]
        self.assertEqual(got["n"], 0)
        self.assertIsNone(got["verdict"])
        self.assertIsNotNone(got["radar_check"])              # rain is rain

    def test_the_text_report_frames_it_as_a_floor_not_a_correction(self):
        forecast.RADAR_PROVIDER = self.WET
        code, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13"])
        self.assertIn("RADAR CHECK", out)
        self.assertIn("NOT in anything above", out)
        self.assertIn("floor", out)
        self.assertIn("does not correct them", out)

    def test_the_footer_stops_sending_the_reader_to_do_it_by_hand(self):
        """Retiring that instruction is the point of #18 -- it was an admission."""
        forecast.RADAR_PROVIDER = self.WET
        _, on, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13"])
        forecast.RADAR_PROVIDER = None
        _, off, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13"])
        self.assertIn("cross-check radar (MRMS/AHPS)", off)
        self.assertNotIn("cross-check radar (MRMS/AHPS)", on)
        self.assertIn("that cross-check", on)

    # -- selection --------------------------------------------------------- #
    def test_none_switches_it_off_by_name(self):
        self.assertIsNone(forecast.resolve_radar("none"))

    def test_the_radar_variant_does_not_retry(self):
        """Retrying is right for the fit and wrong for an optional second opinion:
        it would sleep through IEM_RETRIES per source to drop a line anyway."""
        self.assertEqual(forecast.RADAR_RETRIES, 1)
        self.assertIs(forecast.resolve_radar("iem:mrms"), forecast.iem_mrms_radar)
        self.assertIsNot(forecast.iem_mrms_radar, forecast.iem_mrms_provider)
        self.assertEqual(forecast.precip_name(forecast.iem_mrms_radar), "iem:mrms")

    def test_an_unknown_radar_product_is_rejected(self):
        with self.assertRaises(ValueError):
            forecast.resolve_radar("iem:mrmz")
        code, out, err = run_cli([EXAMPLE_CSV, "--radar", "iem:mrmz"])
        self.assertEqual(code, 2)
        self.assertIn("[error]", err)

    def test_the_cli_flag_selects_and_disables(self):
        forecast.RADAR_PROVIDER = self.WET
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json",
                             "--radar", "none"])
        payload = json.loads(out)
        self.assertEqual(payload["params"]["radar"], "none")
        self.assertIsNone(payload["sources"][0]["radar_check"])

    def test_no_flag_means_whatever_the_host_configured(self):
        forecast.RADAR_PROVIDER = self.WET
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        self.assertEqual(json.loads(out)["params"]["radar"], "test:radar")


class TestPrecipCache(unittest.TestCase):
    """Issue #6: the cache key must not embed the end date."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._cache_dir = forecast.CACHE_DIR
        self._urlopen = urllib.request.urlopen
        self._radar = forecast.RADAR_PROVIDER
        forecast.CACHE_DIR = self.tmp
        forecast.RADAR_PROVIDER = None          # the stub below only speaks ERA5
        self.fetched = []

        def stub(url, timeout=0):
            self.fetched.append(url)
            end = date.fromisoformat(url.split("end_date=")[1][:10])
            start = date.fromisoformat(forecast.PRECIP_START)
            days = (end - start).days + 1
            return io.BytesIO(json.dumps({"daily": {
                "time": [(start + timedelta(days=i)).isoformat() for i in range(days)],
                "precipitation_sum": [round((i % 37) * 0.01, 3) for i in range(days)],
            }}).encode())
        urllib.request.urlopen = stub

    def tearDown(self):
        forecast.CACHE_DIR = self._cache_dir
        urllib.request.urlopen = self._urlopen

    def test_cache_path_is_coordinate_only(self):
        p = forecast._cache_path(34.09059, -111.46653)
        self.assertEqual(os.path.basename(p), "34.09_-111.47.json")
        self.assertNotIn("2026", os.path.basename(p))

    def test_one_fetch_serves_many_as_of_dates(self):
        forecast.open_meteo_provider(34.09, -111.47, date(2026, 7, 30))
        self.assertEqual(len(self.fetched), 1)
        for asof in (date(2026, 7, 13), date(2025, 6, 1), date(2020, 3, 15)):
            forecast.open_meteo_provider(34.09, -111.47, asof)
        self.assertEqual(len(self.fetched), 1, "earlier dates must reuse the cache")
        self.assertEqual(len(os.listdir(self.tmp)), 1, "one file per coordinate")

    def test_a_cache_that_stops_short_refetches(self):
        forecast.open_meteo_provider(34.09, -111.47, date(2026, 1, 1))
        forecast.open_meteo_provider(34.09, -111.47, date(2026, 7, 30))
        self.assertEqual(len(self.fetched), 2)
        self.assertIn("end_date=2026-07-30", self.fetched[1])
        self.assertIn(f"start_date={forecast.PRECIP_START}", self.fetched[1])

    def test_cached_series_is_trimmed_to_the_request(self):
        long = forecast.open_meteo_provider(34.09, -111.47, date(2026, 7, 30))
        short = forecast.open_meteo_provider(34.09, -111.47, date(2026, 7, 13))
        self.assertEqual(long["daily"]["time"][-1], "2026-07-30")
        self.assertEqual(short["daily"]["time"][-1], "2026-07-13")
        self.assertEqual(len(short["daily"]["time"]), len(short["daily"]["precipitation_sum"]))

    def test_no_cache_bypasses_a_valid_cache(self):
        forecast.open_meteo_provider(34.09, -111.47, date(2026, 7, 30))
        forecast.open_meteo_provider(34.09, -111.47, date(2026, 7, 30), use_cache=False)
        self.assertEqual(len(self.fetched), 2)

    def test_unreadable_cache_is_a_miss_not_a_crash(self):
        with open(forecast._cache_path(34.09, -111.47), "w") as f:
            f.write("{not json")
        data = forecast.open_meteo_provider(34.09, -111.47, date(2026, 7, 30))
        self.assertEqual(len(self.fetched), 1)
        self.assertTrue(data["daily"]["time"])


# =========================================================================== #
# Statistics -- mostly invariants, since the exact values are what is under test
# =========================================================================== #
class TestCacheLocation(unittest.TestCase):
    """Where downloaded precipitation lands.

    It used to be unconditionally `.cache/` beside the module, which writes into
    site-packages once the engine is installed -- and simply fails where that is
    read-only, which is every container and serverless bundle."""
    def setUp(self):
        import tempfile, unittest.mock
        self.tmp = tempfile.mkdtemp()
        self.mock = unittest.mock

    def resolve(self, env=None, platform="linux", here=None):
        with self.mock.patch.dict(os.environ, env or {}, clear=True), \
             self.mock.patch.object(sys, "platform", platform), \
             self.mock.patch.object(forecast, "HERE", here or self.tmp):
            return forecast._default_cache_dir()

    def test_explicit_env_var_wins(self):
        got = self.resolve(env={"WATER_ORACLE_CACHE": "/somewhere/else"})
        self.assertEqual(got, "/somewhere/else")

    def test_env_var_beats_an_adjacent_cache(self):
        os.makedirs(os.path.join(self.tmp, ".cache"))
        got = self.resolve(env={"WATER_ORACLE_CACHE": "/somewhere/else"})
        self.assertEqual(got, "/somewhere/else")

    def test_an_existing_adjacent_cache_is_kept(self):
        """A working checkout must not silently re-download ~19 years per source."""
        beside = os.path.join(self.tmp, ".cache")
        os.makedirs(beside)
        self.assertEqual(self.resolve(), beside)

    def test_no_adjacent_cache_means_the_user_cache(self):
        got = self.resolve(env={"HOME": "/home/someone"})
        self.assertNotIn(self.tmp, got)
        self.assertIn("backcountry-water-oracle", got)

    def test_platform_conventions(self):
        self.assertEqual(
            self.resolve(env={"HOME": "/home/someone"}, platform="linux"),
            "/home/someone/.cache/backcountry-water-oracle")
        self.assertEqual(
            self.resolve(env={"HOME": "/home/someone", "XDG_CACHE_HOME": "/xdg"},
                         platform="linux"),
            "/xdg/backcountry-water-oracle")
        self.assertEqual(
            self.resolve(env={"HOME": "/Users/someone"}, platform="darwin"),
            "/Users/someone/Library/Caches/backcountry-water-oracle")
        self.assertEqual(
            self.resolve(env={"LOCALAPPDATA": r"C:\Users\someone\AppData\Local"},
                         platform="win32"),
            os.path.join(r"C:\Users\someone\AppData\Local", "backcountry-water-oracle"))

    def test_never_resolves_inside_the_module_directory_when_installed(self):
        """The actual bug: an installed engine writing into site-packages."""
        fake_site_packages = os.path.join(self.tmp, "site-packages")
        os.makedirs(fake_site_packages)
        got = self.resolve(env={"HOME": "/home/someone"}, here=fake_site_packages)
        self.assertFalse(got.startswith(fake_site_packages))

    def test_the_module_constant_is_wired_to_the_resolver(self):
        """Not just that the resolver is right, but that CACHE_DIR actually uses it.

        Without this, reverting the constant to `os.path.join(HERE, ".cache")`
        passes every other test in this class -- in a checkout the two expressions
        agree, so only re-importing under a different environment can tell them
        apart. That exact mutation survived until this test existed."""
        import importlib
        try:
            with self.mock.patch.dict(os.environ, {"WATER_ORACLE_CACHE": self.tmp},
                                      clear=False):
                importlib.reload(forecast)
                self.assertEqual(forecast.CACHE_DIR, self.tmp)
        finally:
            # Outside the patch, so the env var is gone before we re-resolve.
            importlib.reload(forecast)
        self.assertNotEqual(forecast.CACHE_DIR, self.tmp)

    def test_the_module_constant_is_still_assignable(self):
        """Hosts point CACHE_DIR wherever they like; that must keep working."""
        prev = forecast.CACHE_DIR
        try:
            forecast.CACHE_DIR = self.tmp
            self.assertEqual(os.path.dirname(forecast._cache_path(34.0, -111.0)), self.tmp)
        finally:
            forecast.CACHE_DIR = prev


class TestStats(unittest.TestCase):
    def test_ranks_average_ties(self):
        self.assertEqual(forecast._ranks([1, 2, 2, 3]), [1.0, 2.5, 2.5, 4.0])
        self.assertEqual(forecast._ranks([5, 5, 5]), [2.0, 2.0, 2.0])

    def test_spearman_perfect_monotonic(self):
        xs = [1, 2, 3, 4, 5]
        self.assertAlmostEqual(forecast.spearman(xs, [10, 20, 30, 40, 50]), 1.0)
        self.assertAlmostEqual(forecast.spearman(xs, [50, 40, 30, 20, 10]), -1.0)

    def test_spearman_is_rank_based_not_linear(self):
        """Monotone but wildly non-linear still reads as a perfect rank match."""
        self.assertAlmostEqual(forecast.spearman([1, 2, 3, 4], [1, 4, 900, 10000]), 1.0)

    def test_constant_vector_gives_zero_not_a_zero_division(self):
        self.assertEqual(forecast._pearson([1, 2, 3], [5, 5, 5]), 0.0)
        self.assertEqual(forecast.spearman([1, 2, 3], [5, 5, 5]), 0.0)

    def test_deseasonalize_removes_the_mean_when_data_is_thin(self):
        dates = [date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)]
        out = forecast.deseasonalize(dates, [1.0, 2.0, 3.0])
        self.assertAlmostEqual(sum(out), 0.0)

    def test_deseasonalize_removes_a_pure_annual_cycle(self):
        dates = [date(2024, 1, 1) + timedelta(days=7 * i) for i in range(52)]
        import math
        vals = [math.sin(2 * math.pi * d.timetuple().tm_yday / 365.25) for d in dates]
        resid = forecast.deseasonalize(dates, vals)
        self.assertLess(max(abs(r) for r in resid), 0.05)

    def test_deseasonalize_keeps_signal_that_is_not_seasonal(self):
        dates = [date(2024, 1, 1) + timedelta(days=7 * i) for i in range(52)]
        vals = [0.0] * 52
        vals[10] = 5.0                                   # a one-off spike survives
        resid = forecast.deseasonalize(dates, vals)
        self.assertGreater(max(resid), 1.0)

    def test_solve_returns_none_for_a_singular_system(self):
        self.assertIsNone(forecast._solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0]))
        self.assertIsNotNone(forecast._solve([[2.0, 0.0], [0.0, 2.0]], [2.0, 4.0]))

    def test_haversine_against_known_distances(self):
        self.assertAlmostEqual(forecast._haversine_km(34.0, -111.0, 34.0, -111.0), 0.0)
        # one degree of latitude is ~111.19 km anywhere
        self.assertAlmostEqual(forecast._haversine_km(34.0, -111.0, 35.0, -111.0), 111.19, places=1)
        self.assertTrue(forecast._haversine_km(0, 0, 0, 180) > 20000)     # antipodal-ish

    def test_fisher_z_is_finite_at_the_extremes(self):
        for r in (-1.0, 1.0, -1.5, 1.5):
            self.assertTrue(abs(forecast._fisher_z(r)) < 10)

    def test_median(self):
        self.assertEqual(forecast._median([3, 1, 2]), 2)
        self.assertEqual(forecast._median([4, 1, 3, 2]), 2.5)

    def test_survival_note_thresholds(self):
        self.assertIn("no raw signal", forecast.survival_note(0.01, 0.01))
        self.assertIn("survives", forecast.survival_note(0.50, 0.40))
        self.assertIn("real signal remains", forecast.survival_note(0.50, 0.25))
        self.assertIn("seasonal artifact", forecast.survival_note(0.50, 0.05))


class TestWindowSum(unittest.TestCase):
    def setUp(self):
        start = date(2024, 1, 1)
        n = 100
        self.idx = forecast.build_precip_index({"daily": {
            "time": [(start + timedelta(days=i)).isoformat() for i in range(n)],
            "precipitation_sum": [1.0] * n,
        }})

    def test_simple_window(self):
        self.assertAlmostEqual(forecast.window_sum(self.idx, date(2024, 2, 1), 10), 10.0)

    def test_window_reaching_before_the_series_start_clamps(self):
        total = forecast.window_sum(self.idx, date(2024, 1, 5), 365)
        self.assertAlmostEqual(total, 5.0)               # only 5 days exist

    def test_end_past_the_series_end_clamps(self):
        a = forecast.window_sum(self.idx, date(2030, 1, 1), 10)
        b = forecast.window_sum(self.idx, self.idx["last"], 10)
        self.assertAlmostEqual(a, b)

    def test_none_values_read_as_zero(self):
        idx = forecast.build_precip_index({"daily": {
            "time": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "precipitation_sum": [1.0, None, 2.0],
        }})
        self.assertAlmostEqual(forecast.window_sum(idx, date(2024, 1, 3), 3), 3.0)


# =========================================================================== #
# Antecedent rain vs the site's own climatology -- issue #8
# =========================================================================== #
def year_index(first=2007, last=2026, daily=lambda y: 1.0):
    """A precip index whose every day in year Y holds daily(Y), so a window's sum
    is a known multiple of the year's value and percentiles can be reasoned about
    by hand."""
    days, vals = [], []
    d, end = date(first, 1, 1), date(last, 12, 31)
    while d <= end:
        days.append(d.isoformat())
        vals.append(daily(d.year))
        d += timedelta(days=1)
    return forecast.build_precip_index({"daily": {"time": days,
                                                  "precipitation_sum": vals}})


class TestRainPercentiles(unittest.TestCase):
    ASOF = date(2026, 7, 13)

    def test_the_wettest_run_up_on_record_is_the_top(self):
        idx = year_index(daily=lambda y: float(y - 2006))      # each year wetter
        got = forecast.rain_percentiles(idx, self.ASOF)["30d"]
        self.assertEqual(got["pct"], 100.0)
        self.assertAlmostEqual(got["inches"], 30 * 20.0)

    def test_the_driest_is_the_bottom(self):
        idx = year_index(daily=lambda y: float(2027 - y))       # each year drier
        self.assertEqual(forecast.rain_percentiles(idx, self.ASOF)["30d"]["pct"], 0.0)

    def test_a_perfectly_normal_year_lands_mid_scale(self):
        """Every year identical: midrank puts it at 50, not 0 or 100. This is the
        desert case -- a dry window in a place where most years are dry."""
        idx = year_index(daily=lambda y: 1.0)
        got = forecast.rain_percentiles(idx, self.ASOF)["30d"]
        self.assertEqual(got["pct"], 50.0)
        self.assertAlmostEqual(got["median_in"], got["inches"])

    def test_the_as_of_year_is_not_in_its_own_comparison(self):
        idx = year_index(2007, 2026)                            # 20 years of record
        self.assertEqual(forecast.rain_percentiles(idx, self.ASOF)["30d"]["n_years"], 19)

    def test_long_windows_are_ranked_against_fewer_years(self):
        """A 365d window ending in July 2007 would reach into 2006, which the record
        does not have -- so that year cannot be a comparison, and n_years says so."""
        rain = forecast.rain_percentiles(year_index(2007, 2026), self.ASOF)
        self.assertEqual(rain["30d"]["n_years"], 19)
        self.assertEqual(rain["365d"]["n_years"], 18)

    def test_no_comparison_years_at_all_is_none_not_a_crash(self):
        rain = forecast.rain_percentiles(year_index(2026, 2026), self.ASOF)
        self.assertIsNone(rain["30d"]["pct"])
        self.assertIsNone(rain["30d"]["median_in"])
        self.assertEqual(rain["30d"]["n_years"], 0)
        self.assertGreater(rain["30d"]["inches"], 0)            # the sum is still real

    def test_every_window_is_covered(self):
        rain = forecast.rain_percentiles(year_index(), self.ASOF)
        self.assertEqual(sorted(int(w[:-1]) for w in rain), sorted(forecast.WINDOWS))

    def test_leap_day_does_not_explode(self):
        """29 Feb has no counterpart in most years; one day's shift is far inside
        the noise of a 30-day window, and raising would be absurd."""
        self.assertEqual(forecast._same_day_of_year(date(2024, 2, 29), 2023),
                         date(2023, 2, 28))
        rain = forecast.rain_percentiles(year_index(), date(2024, 2, 29))
        self.assertEqual(rain["30d"]["n_years"], 19)

    def test_percentile_midranks_ties(self):
        self.assertEqual(forecast._percentile_of(5, [1, 2, 3, 4]), 100.0)
        self.assertEqual(forecast._percentile_of(0, [1, 2, 3, 4]), 0.0)
        self.assertEqual(forecast._percentile_of(3, [1, 2, 4, 5]), 50.0)
        self.assertEqual(forecast._percentile_of(2, [1, 2, 2, 5]), 50.0)

    def test_ordinals(self):
        got = [forecast._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 100)]
        self.assertEqual(got, ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th",
                               "21st", "22nd", "100th"])


class TestNeighborsOf(unittest.TestCase):
    """The selection rule alone, on synthetic rows -- no precip, no fixtures (#8)."""
    def row(self, name, lon, n=10, type_="Flashy (needs recent rain)", pred=0.5):
        return {"name": name, "lat": 34.09, "lon": lon, "n": n,
                "type": type_, "pct_dry": 20, "pred": pred}

    def test_only_reported_sources_qualify(self):
        pin = self.row("Pin", -111.47, n=0)
        rows = [pin, self.row("Reported", -111.48), self.row("OtherPin", -111.46, n=0)]
        got = forecast.neighbors_of(pin, rows, 25.0)
        self.assertEqual([o["name"] for o in got], ["Reported"])

    def test_a_source_is_never_its_own_neighbour(self):
        a = self.row("A", -111.47)
        self.assertEqual(forecast.neighbors_of(a, [a], 25.0), [])

    def test_two_sources_at_one_spot_are_both_neighbours(self):
        """Distinct names at the same coordinate are legal (the loader allows it)."""
        pin = self.row("Pin", -111.47, n=0)
        rows = [pin, self.row("A", -111.48), self.row("B", -111.48)]
        self.assertEqual(len(forecast.neighbors_of(pin, rows, 25.0)), 2)

    def test_the_radius_is_inclusive(self):
        pin = self.row("Pin", -111.47, n=0)
        other = self.row("Edge", -111.48)
        km = forecast._haversine_km(34.09, -111.47, 34.09, -111.48)
        self.assertEqual(len(forecast.neighbors_of(pin, [pin, other], km)), 1)
        self.assertEqual(len(forecast.neighbors_of(pin, [pin, other], km * 0.99)), 0)

    def test_the_verdict_is_the_neighbours_own_phrase(self):
        pin = self.row("Pin", -111.47, n=0)
        rows = [pin, self.row("Wet", -111.48, pred=0.95)]
        got = forecast.neighbors_of(pin, rows, 25.0)[0]
        self.assertEqual(got["verdict"], forecast.running_phrase(0.95))
        self.assertEqual(got["predicted_flow"], 0.95)


class TestTrimDaily(unittest.TestCase):
    def series(self, n=10):
        start = date(2024, 1, 1)
        return {"daily": {
            "time": [(start + timedelta(days=i)).isoformat() for i in range(n)],
            "precipitation_sum": [float(i) for i in range(n)]}}

    def test_trims_to_the_requested_end(self):
        out = forecast._trim_daily(self.series(), date(2024, 1, 5))
        self.assertEqual(out["daily"]["time"][-1], "2024-01-05")
        self.assertEqual(len(out["daily"]["time"]), len(out["daily"]["precipitation_sum"]))

    def test_shorter_series_is_untouched(self):
        s = self.series()
        self.assertIs(forecast._trim_daily(s, date(2030, 1, 1)), s)

    def test_does_not_mutate_its_input(self):
        s = self.series()
        forecast._trim_daily(s, date(2024, 1, 3))
        self.assertEqual(len(s["daily"]["time"]), 10)


# =========================================================================== #
# Pooling -- invariants, since the point is HOW MUCH is borrowed and why
# =========================================================================== #
class TestPooling(unittest.TestCase):
    def test_a_lone_source_borrows_nothing(self):
        out = forecast._pool_window([(0.5, 40)])
        self.assertEqual(out, [(0.5, 0.0)])

    def test_a_coherent_group_pools_harder_than_a_split_one(self):
        agree = forecast._pool_window([(0.50, 20), (0.52, 20), (0.48, 20)])
        differ = forecast._pool_window([(0.90, 20), (0.05, 20), (-0.40, 20)])
        self.assertGreater(agree[0][1], differ[0][1])

    def test_small_n_borrows_more_than_data_rich(self):
        out = forecast._pool_window([(0.5, 200), (0.5, 100), (0.5, 12)])
        borrowed = [b for _, b in out]
        self.assertLess(borrowed[0], borrowed[1])
        self.assertLess(borrowed[1], borrowed[2])

    def test_borrowed_fraction_stays_a_fraction(self):
        for items in ([(0.5, 200), (0.5, 12)], [(0.9, 20), (-0.4, 15), (0.1, 300)]):
            for _, b in forecast._pool_window(items):
                self.assertGreaterEqual(b, 0.0)
                self.assertLessEqual(b, 1.0)

    def test_pooled_r_lands_between_own_and_the_group(self):
        items = [(0.20, 10), (0.60, 200)]
        out = forecast._pool_window(items)
        self.assertGreater(out[0][0], 0.20)              # small-n pulled up toward .6
        self.assertLess(out[0][0], 0.60)

    def test_tau2_never_collapses_to_all_or_nothing(self):
        """The k=3 lumpiness fix: borrowing must be graded, not 0% / 100%."""
        out = forecast._pool_window([(0.50, 160), (0.46, 58), (0.40, 15)])
        for _, b in out:
            self.assertGreater(b, 0.0)
            self.assertLess(b, 1.0)

    def test_pool_controlled_is_order_independent(self):
        """It must read each source's own `ctrl`, never a neighbour's pooled result.

        Compared to a tolerance, not exactly: reversing the list reverses the order
        the weighted sums accumulate in, and floating-point addition is not
        associative, so the last bit legitimately moves (this failed on Python
        3.9-3.11 asserting equality, and passed on 3.12+, which is a good sign it
        was the assertion at fault rather than the engine). Genuine order
        dependence -- pooling that reads a neighbour's already-pooled value --
        moves the result far more than this, and is still caught."""
        def bases():
            return [{"name": n, "lat": la, "lon": lo, "n": nn,
                     "ctrl": {f"{w}d": r for w in forecast.WINDOWS},
                     "pooled_ctrl": {}, "borrowed": {}}
                    for n, la, lo, nn, r in [
                        ("A", 34.090, -111.450, 160, 0.55),
                        ("B", 34.091, -111.470, 58, 0.45),
                        ("C", 34.092, -111.490, 15, 0.20)]]
        fwd = bases()
        forecast.pool_controlled(fwd, forecast.POOL_RADIUS_KM)
        rev = list(reversed(bases()))
        forecast.pool_controlled(rev, forecast.POOL_RADIUS_KM)
        rev_by_name = {b["name"]: b for b in rev}
        for b in fwd:
            other = rev_by_name[b["name"]]
            self.assertEqual(sorted(b["pooled_ctrl"]), sorted(other["pooled_ctrl"]))
            for w, r in b["pooled_ctrl"].items():
                self.assertAlmostEqual(r, other["pooled_ctrl"][w], places=12,
                                       msg=f"{b['name']} {w} depends on processing order")
                self.assertAlmostEqual(b["borrowed"][w], other["borrowed"][w], places=12)

    def test_sources_outside_the_radius_are_not_neighbours(self):
        far = [{"name": "A", "lat": 34.0, "lon": -111.0, "n": 20,
                "ctrl": {f"{w}d": 0.6 for w in forecast.WINDOWS},
                "pooled_ctrl": {}, "borrowed": {}},
               {"name": "B", "lat": 44.0, "lon": -121.0, "n": 20,
                "ctrl": {f"{w}d": -0.6 for w in forecast.WINDOWS},
                "pooled_ctrl": {}, "borrowed": {}}]
        forecast.pool_controlled(far, forecast.POOL_RADIUS_KM)
        for b in far:
            self.assertEqual(b["group_n"], 1)
            self.assertEqual(b["borrowed"]["30d"], 0.0)
            self.assertAlmostEqual(b["pooled_ctrl"]["30d"], b["ctrl"]["30d"])


# =========================================================================== #
# Verdict / classification / the analog read
#
# These three encode judgement calls -- where "marginal" ends, what counts as
# buffered, how many past reports the current read averages. Mutation testing
# found them pinned only by the golden payload, which tells you SOMETHING moved
# but not what, so they get named tests of their own.
# =========================================================================== #
class TestVerdicts(unittest.TestCase):
    def test_running_phrase_boundaries(self):
        self.assertEqual(forecast.running_phrase(0.00), "Likely DRY")
        self.assertEqual(forecast.running_phrase(0.099), "Likely DRY")
        self.assertIn("Marginal", forecast.running_phrase(0.10))
        self.assertIn("Marginal", forecast.running_phrase(0.299))
        self.assertIn("Probably has water", forecast.running_phrase(0.30))
        self.assertIn("Probably has water", forecast.running_phrase(0.499))
        self.assertIn("moderate", forecast.running_phrase(0.50))
        self.assertIn("moderate", forecast.running_phrase(0.699))
        self.assertIn("flowing well", forecast.running_phrase(0.70))
        self.assertIn("flowing well", forecast.running_phrase(1.00))

    def test_classify(self):
        self.assertIn("Reliable", forecast.classify(0, 365))
        self.assertIn("Reliable", forecast.classify(10, 30))    # dryness wins first
        self.assertIn("Flashy", forecast.classify(11, 90))
        self.assertIn("Intermediate", forecast.classify(11, 180))


class TestNearestAnalog(OfflineTestCase):
    def test_averages_exactly_the_five_closest_analogs(self):
        """Precip climbs steadily, so every window sum climbs with the date: the
        closest analogs to an as-of at the end are simply the latest reports. Score
        the last five 1.0 and everything earlier 0.0, and the read is 1.0 only if
        exactly five went into it (seven would give 5/7)."""
        start, n = date(2024, 1, 1), 400
        series = {"daily": {
            "time": [(start + timedelta(days=i)).isoformat() for i in range(n)],
            "precipitation_sum": [round(i * 0.001, 4) for i in range(n)]}}
        forecast.PRECIP_PROVIDER = lambda lat, lon, end, use_cache=True: series

        report_days = list(range(100, 400, 10))                 # 30 reports
        reports = [(start + timedelta(days=d), 1.0 if d >= report_days[-5] else 0.0)
                   for d in report_days]
        asof = start + timedelta(days=n - 1)
        a = forecast.analyze(source(reports=reports), asof)
        self.assertAlmostEqual(a["pred"], 1.0)
        self.assertEqual(a["n"], 30)

    def test_the_read_uses_the_best_window(self):
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        for s in json.loads(out)["sources"]:
            self.assertEqual(s["best"]["days"], int(s["best"]["window"][:-1]))
            self.assertGreaterEqual(s["precip_in"], 0.0)


# =========================================================================== #
# The analog pool at low n -- issue #39
#
# `[:ANALOG_K]` creates a boundary nothing in the payload used to mention: once a
# source has ANALOG_K reports or fewer, the "nearest" analogs are all of them, the
# sort selects nothing, and the read is the source's mean flow on every date --
# a verdict that rain cannot move, printed in the same shape as one that responds.
# The number is not wrong. It is a different kind of statement, and these tests
# pin the two keys that say which kind it is.
# =========================================================================== #
class TestDegenerateAnalogPool(OfflineTestCase):
    """A climbing precip series: every window sum rises with the date, so two
    as-ofs far apart genuinely differ in antecedent rain. Any source that can
    respond to rain must read differently at the two; one that cannot, cannot."""
    START, DAYS = date(2024, 1, 1), 700

    def setUp(self):
        super().setUp()
        series = {"daily": {
            "time": [(self.START + timedelta(days=i)).isoformat() for i in range(self.DAYS)],
            "precipitation_sum": [round(i * 0.002, 4) for i in range(self.DAYS)]}}
        forecast.PRECIP_PROVIDER = lambda lat, lon, end, use_cache=True: series

    def read(self, n_reports, asof_day):
        """A source with n_reports spread over the record, read at one as-of.

        Scores rise with the date, alongside the rain. A source that reads its
        nearest analogs therefore MUST answer differently early and late; equality
        is then evidence about the pool, not a coincidence of the scores."""
        reports = [(self.START + timedelta(days=100 + i * 40),
                    round(i / max(1, n_reports - 1), 4)) for i in range(n_reports)]
        return forecast.analyze(source(reports=reports),
                                self.START + timedelta(days=asof_day))

    def test_at_or_below_ANALOG_K_the_read_cannot_respond_to_rain(self):
        for n in range(1, forecast.ANALOG_K + 1):
            dry, wet = self.read(n, 300), self.read(n, 699)
            self.assertLess(dry["curval"], wet["curval"], f"n={n}: rain did not differ")
            self.assertEqual(dry["pred"], wet["pred"], f"n={n}: identical read expected")
            self.assertTrue(dry["pred_is_constant"], f"n={n}")

    def test_above_ANALOG_K_it_does(self):
        dry, wet = self.read(12, 300), self.read(12, 699)
        self.assertNotEqual(dry["pred"], wet["pred"])
        self.assertFalse(dry["pred_is_constant"])

    def test_the_boundary_sits_exactly_at_ANALOG_K(self):
        self.assertTrue(self.read(forecast.ANALOG_K, 699)["pred_is_constant"])
        self.assertFalse(self.read(forecast.ANALOG_K + 1, 699)["pred_is_constant"])

    def test_analog_n_is_what_the_average_actually_drew_on(self):
        for n in (1, 3, forecast.ANALOG_K, forecast.ANALOG_K + 1, 20):
            a = self.read(n, 699)
            self.assertEqual(a["analog_n"], min(n, forecast.ANALOG_K), f"n={n}")

    def test_a_constant_read_is_the_mean_of_every_score(self):
        """Not an approximation of one: when the pool is the whole history the
        analog average IS the mean, which is the thing worth disclosing."""
        a = self.read(3, 699)
        self.assertAlmostEqual(a["pred"], a["mean"])

    def test_ANALOG_K_drives_the_slice_rather_than_a_literal(self):
        """The point of naming it (#39, option C): the width is one place now."""
        original = forecast.ANALOG_K
        try:
            forecast.ANALOG_K = 3
            self.assertEqual(self.read(10, 699)["analog_n"], 3)
            self.assertTrue(self.read(3, 699)["pred_is_constant"])
        finally:
            forecast.ANALOG_K = original

    def test_small_n_does_not_already_say_this(self):
        """n < 25 marks a read as coarse. It does not separate coarse from
        structurally constant, which is why a second key was needed."""
        payload = forecast.run([source(reports=[
            (self.START + timedelta(days=100 + i * 40), (i % 3) / 2) for i in range(10)])],
            self.START + timedelta(days=699))
        s = payload["sources"][0]
        self.assertTrue(s["small_n"])
        self.assertFalse(s["pred_is_constant"])

    def test_the_payload_carries_both_keys(self):
        payload = forecast.run([source(reports=[(self.START + timedelta(days=200), 0.4)])],
                               self.START + timedelta(days=699))
        s = payload["sources"][0]
        self.assertEqual(s["analog_n"], 1)
        self.assertTrue(s["pred_is_constant"])
        self.assertEqual(s["predicted_flow"], 0.4)

    def test_a_source_with_no_reports_gets_null_not_false(self):
        """`false` would assert something about a read that does not exist. n == 0
        keeps every key null, the way #8 established."""
        s = forecast.run(forecast.load_sources_from(
            [io.StringIO("source,lat,lon,date,score\nPin,34.09,-111.47,,\n")]),
            self.START + timedelta(days=699))["sources"][0]
        self.assertIsNone(s["analog_n"])
        self.assertIsNone(s["pred_is_constant"])

    def test_the_text_report_says_so_beside_the_verdict(self):
        csv = ("source,lat,lon,date,score\n"
               "One,34.09,-111.47,2024-04-10,0.4\n")
        _, out, _ = run_cli(["-", "--asof", "2025-12-01", "--radar", "none"],
                            stdin_text=csv)
        self.assertIn("VERDICT:", out)
        self.assertIn("every report this source has (1)", out)
        self.assertIn("the record, not a forecast", out)

    def test_and_stays_quiet_where_the_read_is_a_real_one(self):
        reports = "".join(f"One,34.09,-111.47,{(self.START + timedelta(days=100 + i * 40)).isoformat()},{(i % 3) / 2}\n"
                          for i in range(12))
        _, out, _ = run_cli(["-", "--asof", "2025-12-01", "--radar", "none"],
                            stdin_text="source,lat,lon,date,score\n" + reports)
        self.assertIn("VERDICT:", out)
        self.assertNotIn("the record, not a forecast", out)

    def test_the_summary_table_stars_a_constant_read(self):
        """The table sorts by %dry, so one non-dry report ranks FIRST -- the least
        evidenced read presented as the most reliable. The star is the correction."""
        csv = ("source,lat,lon,date,score\n"
               "Once,34.09,-111.47,2024-04-10,0.6\n"
               + "".join(f"Often,34.09,-111.45,{(self.START + timedelta(days=100 + i * 40)).isoformat()},{(i % 3) / 2}\n"
                         for i in range(12)))
        _, out, _ = run_cli(["-", "--asof", "2025-12-01", "--radar", "none"],
                            stdin_text=csv)
        table = out[out.index("SUMMARY"):]
        starred = [ln for ln in table.splitlines() if ln.rstrip().endswith(" *")]
        self.assertEqual(len(starred), 1)
        self.assertTrue(starred[0].startswith("Once"))
        self.assertIn("rain cannot move it", table)


# =========================================================================== #
# Report accounting -- issue #10
# =========================================================================== #
class TestReportAccounting(OfflineTestCase):
    def src_with(self, dates):
        return source(reports=[(d, 0.5) for d in dates])

    def test_counts_reports_that_predate_the_record(self):
        b = forecast.analyze_base(self.src_with(
            [date(1999, 1, 1), date(2001, 2, 1), date(2024, 3, 1), date(2024, 6, 1)]), ASOF)
        self.assertEqual(b["n"], 2)
        self.assertEqual(b["n_total"], 4)
        self.assertEqual(b["n_early"], 2)
        self.assertEqual(b["n_late"], 0)

    def test_counts_reports_that_postdate_the_record(self):
        b = forecast.analyze_base(self.src_with(
            [date(2024, 3, 1), date(2024, 6, 1), date(2030, 1, 1)]), ASOF)
        self.assertEqual(b["n"], 2)
        self.assertEqual(b["n_late"], 1)

    def test_nothing_usable_returns_counts_not_none(self):
        b = forecast.analyze_base(self.src_with([date(1999, 1, 1), date(2001, 2, 1)]), ASOF)
        self.assertIsNotNone(b)
        self.assertEqual(b["n"], 0)
        self.assertEqual(b["n_early"], 2)

    def test_analyze_still_returns_none_when_nothing_is_usable(self):
        self.assertIsNone(forecast.analyze(self.src_with([date(1999, 1, 1)]), ASOF))

    def test_excluded_note_wording(self):
        b = forecast.analyze_base(self.src_with(
            [date(1999, 1, 1), date(2024, 3, 1), date(2024, 6, 1)]), ASOF)
        note = forecast.excluded_note(b)
        self.assertIn("2 of 3 reports usable", note)
        self.assertIn("1 predates", note)               # singular agrees
        b2 = forecast.analyze_base(self.src_with(
            [date(1999, 1, 1), date(2000, 1, 1), date(2024, 3, 1), date(2024, 6, 1)]), ASOF)
        self.assertIn("2 predate ", forecast.excluded_note(b2))     # plural agrees

    def test_no_note_when_nothing_was_excluded(self):
        b = forecast.analyze_base(self.src_with([date(2024, 3, 1), date(2024, 6, 1)]), ASOF)
        self.assertIsNone(forecast.excluded_note(b))

    def test_stats_describe_the_usable_reports(self):
        """pct_dry and mean are computed on survivors -- documented, so pin it."""
        src = source(reports=[(date(1999, 1, 1), 0.0), (date(2024, 3, 1), 1.0),
                              (date(2024, 6, 1), 1.0)])
        b = forecast.analyze_base(src, ASOF)
        self.assertEqual(b["n"], 2)
        self.assertEqual(b["pct_dry"], 0)               # the 0.0 was pre-record
        self.assertAlmostEqual(b["mean"], 1.0)


# =========================================================================== #
# Zero-report mode: rain context where there is no verdict -- issue #8
# =========================================================================== #
class TestZeroReportMode(OfflineTestCase):
    PIN = "source,lat,lon,date,score\nPin,34.09,-111.47,,\n"

    def payload(self, csv):
        return forecast.run(forecast.load_sources_from([io.StringIO(csv)]), ASOF)

    def test_a_pin_reaches_the_payload_instead_of_vanishing(self):
        s = self.payload(self.PIN)["sources"][0]
        self.assertEqual(s["name"], "Pin")
        self.assertEqual(s["n"], 0)
        self.assertEqual(s["reports"]["total"], 0)

    def test_every_verdict_field_is_null_not_missing(self):
        """The site branches on n == 0; a half-filled verdict is what gets rendered
        as a real one by accident."""
        s = self.payload(self.PIN)["sources"][0]
        for k in ("pct_dry", "mean_flow", "type", "best", "precip_in",
                  "predicted_flow", "verdict"):
            self.assertIsNone(s[k], k)
        self.assertEqual(s["correlations"], [])
        self.assertEqual(s["mean_flow_by_month"], {})

    def test_the_keys_are_the_same_ones_a_reported_source_has(self):
        """Same shape, so a consumer never has to ask which keys exist."""
        both = self.payload(self.PIN + "S,34.09,-111.45,2024-01-01,1.0\n"
                            "S,34.09,-111.45,2024-06-01,0.0\n")["sources"]
        self.assertEqual(sorted(both[0]), sorted(both[1]))

    def test_rain_context_is_real_where_the_verdict_is_not(self):
        s = self.payload(self.PIN)["sources"][0]
        self.assertEqual(sorted(int(w[:-1]) for w in s["rain_percentiles"]),
                         sorted(forecast.WINDOWS))
        for v in s["rain_percentiles"].values():
            self.assertGreater(v["n_years"], 0)
            self.assertIsNotNone(v["pct"])
        self.assertGreater(s["annual_precip_in"], 0)     # precip, not reports

    def test_reported_sources_get_the_rain_context_too(self):
        """#8 explicitly: useful everywhere, not only where reports are missing."""
        payload = forecast.run(forecast.load_sources([EXAMPLE_CSV]), ASOF)
        for s in payload["sources"]:
            self.assertGreater(s["n"], 0)
            self.assertTrue(s["rain_percentiles"])

    def test_a_deliberate_pin_is_not_reported_as_a_skip(self):
        """Asking what rain alone can say is the feature working, not a failure --
        calling it a skip would teach everyone to ignore the word."""
        self.assertEqual(self.payload(self.PIN)["notes"], [])

    def test_but_reports_that_were_LOST_are_still_noted(self):
        payload = self.payload("source,lat,lon,date,score\n"
                               "Ancient,34.09,-111.47,1999-01-01,0.5\n")
        self.assertEqual(payload["notes"][0]["kind"], "skip")
        self.assertIn("predate", payload["notes"][0]["message"])
        self.assertIn("no flow verdict", payload["notes"][0]["message"])
        self.assertEqual(payload["sources"][0]["n"], 0)   # and it is still in there

    def test_input_order_survives_a_mix(self):
        payload = self.payload("source,lat,lon,date,score\n"
                               "First,34.09,-111.45,2024-01-01,1.0\n"
                               "First,34.09,-111.45,2024-06-01,0.0\n"
                               "Second,34.09,-111.47,,\n"
                               "Third,34.09,-111.49,2024-01-01,1.0\n"
                               "Third,34.09,-111.49,2024-06-01,0.0\n")
        self.assertEqual([s["name"] for s in payload["sources"]],
                         ["First", "Second", "Third"])

    def test_a_pin_does_not_disturb_its_neighbours_pooling(self):
        """It contributes no correlation, so it must not join a neighbourhood."""
        alone = forecast.run(forecast.load_sources([EXAMPLE_CSV]), ASOF)
        with open(EXAMPLE_CSV) as f:
            with_pin = forecast.run(forecast.load_sources_from(
                [io.StringIO(f.read() + "Pin,34.09,-111.47,,\n")]), ASOF)
        by_name = {s["name"]: s for s in with_pin["sources"]}
        for s in alone["sources"]:
            self.assertEqual(by_name[s["name"]], s)

    def test_analyze_still_returns_none_for_a_pin(self):
        """The documented single-source API is unchanged: no reports, no analysis."""
        self.assertIsNone(forecast.analyze(source(reports=[]), ASOF))

    # -- the text report ----------------------------------------------------- #
    def text(self, csv):
        code, out, err = run_cli(["-", "--asof", "2026-07-13"], stdin_text=csv)
        return out

    def test_the_report_says_there_is_no_verdict(self):
        out = self.text(self.PIN)
        self.assertIn("NO FLOW VERDICT", out)
        self.assertIn("ANTECEDENT RAIN", out)
        self.assertNotIn("VERDICT: ", out)               # never a running_phrase
        for phrase in ("Likely DRY", "Likely flowing", "Probably has water",
                       "Marginal"):
            self.assertNotIn(phrase, out)

    def test_the_rain_block_is_labelled_as_rain_not_flow(self):
        out = self.text(self.PIN)
        self.assertIn("RAIN, not flow", out)

    def test_a_pin_is_listed_under_the_summary_not_inside_it(self):
        out = self.text(self.PIN + "S,34.09,-111.45,2024-01-01,1.0\n"
                        "S,34.09,-111.45,2024-06-01,0.0\n")
        self.assertIn("NO VERDICT", out)
        summary = out[out.index("SUMMARY"):]
        table, listed = summary.split("NO VERDICT")
        self.assertIn("S ", table)                       # the reported one has a row
        self.assertNotIn("Pin", table)                   # the pin does not
        self.assertIn("Pin", listed)

    def test_a_report_of_only_pins_has_no_empty_table(self):
        out = self.text(self.PIN + "Pin2,34.09,-111.45,,\n")
        self.assertIn("NO VERDICT", out)
        self.assertNotIn("%DRY", out)                    # no header over no rows

    # -- where #8 meets #17 --------------------------------------------------- #
    def rows(self, csv):
        return forecast._analyse(
            forecast.load_sources_from([io.StringIO(csv)]), ASOF, True, 1, True,
            forecast.POOL_RADIUS_KM, lambda *a: None)

    def table(self, csv, precip):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            forecast.print_table(self.rows(csv), precip)
        return out.getvalue()

    def test_the_precip_footer_still_lands_beside_a_pin(self):
        """#17 made the footer name the product; #8 can leave the table empty. The
        caveat is about the rain percentiles too, so it must print either way."""
        mixed = self.table(self.PIN + "S,34.09,-111.45,2024-01-01,1.0\n"
                           "S,34.09,-111.45,2024-06-01,0.0\n", "iem:mrms")
        self.assertIn("iem:mrms", mixed)
        self.assertNotIn("Reminder: ERA5", mixed)

    def test_the_precip_footer_prints_even_with_no_verdicts_at_all(self):
        pins_only = self.table(self.PIN + "Pin2,34.09,-111.45,,\n", "iem:mrms")
        self.assertNotIn("%DRY", pins_only)              # no table
        self.assertIn("NO VERDICT", pins_only)
        self.assertIn("iem:mrms", pins_only)             # ...but the product is named

    def test_the_default_footer_survives_a_pin_too(self):
        pins_only = self.table(self.PIN, forecast.DEFAULT_PRECIP)
        self.assertIn("Reminder: ERA5", pins_only)

    # -- neighbor disclosure (#8, second half) -------------------------------- #
    # The pin sits at one end so the two neighbours are at unambiguous, different
    # distances (~1.8 km and ~3.7 km) -- putting it in the middle made them exactly
    # equidistant, and "nearest first" then depended on dict order.
    MIXED = ("source,lat,lon,date,score\n"
             "Pin,34.09,-111.45,,\n"
             "Buffered,34.09,-111.49,2024-01-01,1.0\n"
             "Buffered,34.09,-111.49,2024-06-01,1.0\n"
             "Flashy,34.09,-111.47,2024-01-01,1.0\n"
             "Flashy,34.09,-111.47,2024-06-01,0.0\n")

    def pin(self, csv=None):
        return self.payload(csv or self.MIXED)["sources"][0]

    def test_a_pin_is_told_who_is_nearby(self):
        got = self.pin()
        self.assertEqual([o["name"] for o in got["neighbors"]], ["Flashy", "Buffered"])
        self.assertEqual(got["n"], 0)

    def test_neighbors_are_nearest_first(self):
        km = [o["distance_km"] for o in self.pin()["neighbors"]]
        self.assertEqual(km, sorted(km))

    def test_a_neighbour_carries_its_own_read_under_its_own_name(self):
        o = self.pin()["neighbors"][0]
        self.assertEqual(sorted(o), sorted(["name", "distance_km", "n", "type",
                                            "pct_dry", "verdict", "predicted_flow"]))
        flashy = next(s for s in self.payload(self.MIXED)["sources"]
                      if s["name"] == "Flashy")
        self.assertEqual(o["verdict"], flashy["verdict"])       # quoted, not derived
        self.assertEqual(o["predicted_flow"], flashy["predicted_flow"])
        self.assertEqual(o["type"], flashy["type"])

    def test_nothing_is_transferred_onto_the_pin(self):
        """The whole design decision: neighbors are shown, never absorbed."""
        got = self.pin()
        for k in ("verdict", "predicted_flow", "type", "pct_dry", "best", "precip_in"):
            self.assertIsNone(got[k], k)

    def test_disagreement_is_reported_because_it_is_the_answer(self):
        got = self.pin()
        self.assertTrue(got["neighbors_disagree"])
        self.assertNotEqual(got["neighbors"][0]["type"], got["neighbors"][1]["type"])

    def test_agreeing_neighbours_do_not_raise_the_flag(self):
        csv = ("source,lat,lon,date,score\nPin,34.09,-111.47,,\n"
               "A,34.09,-111.49,2024-01-01,1.0\nA,34.09,-111.49,2024-06-01,1.0\n"
               "B,34.09,-111.45,2024-01-01,1.0\nB,34.09,-111.45,2024-06-01,1.0\n")
        got = self.pin(csv)
        self.assertEqual(len({o["type"] for o in got["neighbors"]}), 1)
        self.assertFalse(got["neighbors_disagree"])

    def test_one_neighbour_cannot_disagree_with_itself(self):
        csv = ("source,lat,lon,date,score\nPin,34.09,-111.47,,\n"
               "A,34.09,-111.49,2024-01-01,1.0\nA,34.09,-111.49,2024-06-01,0.0\n")
        got = self.pin(csv)
        self.assertEqual(len(got["neighbors"]), 1)
        self.assertFalse(got["neighbors_disagree"])

    def test_a_pin_is_not_a_neighbour_to_another_pin(self):
        """Two pins inform each other of nothing."""
        got = self.pin("source,lat,lon,date,score\n"
                       "Pin,34.09,-111.47,,\nPin2,34.09,-111.45,,\n")
        self.assertEqual(got["neighbors"], [])
        self.assertFalse(got["neighbors_disagree"])

    def test_the_radius_decides_who_counts_as_nearby(self):
        """~1.8 km and ~3.7 km out; a 2.5 km radius keeps only the closer one."""
        srcs = forecast.load_sources_from([io.StringIO(self.MIXED)])
        tight = forecast.run(srcs, ASOF, pool_radius_km=2.5)["sources"][0]
        wide = forecast.run(srcs, ASOF, pool_radius_km=25.0)["sources"][0]
        self.assertEqual([o["name"] for o in tight["neighbors"]], ["Flashy"])
        self.assertEqual([o["name"] for o in wide["neighbors"]], ["Flashy", "Buffered"])
        # and with only one left in range there is nothing to disagree with
        self.assertFalse(tight["neighbors_disagree"])
        self.assertTrue(wide["neighbors_disagree"])

    def test_reported_sources_carry_the_key_but_not_the_list(self):
        """Same shape everywhere; populated only where there is no verdict."""
        for s in self.payload(self.MIXED)["sources"]:
            self.assertIn("neighbors", s)
            if s["n"] > 0:
                self.assertEqual(s["neighbors"], [])
                self.assertFalse(s["neighbors_disagree"])

    def test_disclosure_survives_no_pool(self):
        """--no-pool turns off BORROWING. Saying what is nearby is not borrowing."""
        srcs = forecast.load_sources_from([io.StringIO(self.MIXED)])
        got = forecast.run(srcs, ASOF, pool=False)["sources"][0]
        self.assertEqual(len(got["neighbors"]), 2)

    def test_the_report_shows_the_neighbours_and_the_disagreement(self):
        out = self.text(self.MIXED)
        self.assertIn("NEARBY REPORTED SOURCES", out)
        self.assertIn("NOT this coordinate's", out)
        self.assertIn("safe stand-in", out)

    def test_the_report_says_so_when_there_is_nobody_nearby(self):
        out = self.text(self.PIN)
        self.assertIn("No reported sources nearby", out)

    def test_a_pin_gets_the_product_caveat_like_anything_else(self):
        """A rain-only source is still fit on whatever product answered, so the
        run-wide caveat has to reach a payload that contains nothing but pins."""
        forecast.PRECIP_PROVIDER = forecast.iem_prism_provider
        notes = []
        forecast._analyse([], ASOF, True, 1, True, 25.0,
                          lambda k, m, n=None: notes.append(k))
        self.assertEqual(notes, ["caveat"])


# =========================================================================== #
# CLI behaviour: exit codes, stream routing, JSON validity
# =========================================================================== #
class TestCLI(OfflineTestCase):
    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_success_exit_code_and_summary(self):
        code, out, err = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13"])
        self.assertEqual(code, 0)
        self.assertIn("SUMMARY", out)
        self.assertIn("Chilson Spring", out)
        self.assertEqual(err, "")

    def test_no_files_on_a_terminal_prints_usage(self):
        code, out, _ = run_cli([])
        self.assertEqual(code, 1)
        self.assertIn("Usage:", out)

    def test_no_files_with_piped_stdin_reads_stdin(self):
        csv = ("source,lat,lon,date,score\n"
               "S,34.09,-111.47,2024-01-01,1.0\nS,34.09,-111.47,2024-06-01,0.0\n")
        code, out, _ = run_cli(["--asof", "2026-07-13"], stdin_text=csv)
        self.assertEqual(code, 0)
        self.assertIn("S ", out)

    def test_bad_csv_exits_2(self):
        p = write_csv(self.tmp, "bad.csv", ["1,2"], header="a,b")
        code, _, _ = run_cli([p])
        self.assertEqual(code, 2)

    def test_bad_flag_exits_2_and_writes_to_stderr(self):
        code, out, err = run_cli([EXAMPLE_CSV, "--asof"])
        self.assertEqual(code, 2)
        self.assertIn("[error]", err)
        self.assertEqual(out, "")

    def test_json_stdout_is_pure_json(self):
        code, out, err = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)                        # raises if anything leaked
        self.assertEqual(len(payload["sources"]), 3)

    def test_json_stays_valid_when_a_source_is_skipped(self):
        p = write_csv(self.tmp, "mixed.csv", [
            "Ancient,34.09,-111.47,1999-01-01,0.5",
            "Ancient,34.09,-111.47,2001-01-01,0.5",
            "Good,34.09,-111.45,2024-01-01,1.0",
            "Good,34.09,-111.45,2024-06-01,0.0",
        ])
        code, out, err = run_cli([p, "--asof", "2026-07-13", "--format", "json"])
        payload = json.loads(out)
        # Since #8 the unusable source stays in the payload (with rain context and a
        # null verdict) rather than vanishing -- but it is still noted, and the note
        # still goes to stderr so stdout stays valid JSON.
        self.assertEqual([s["name"] for s in payload["sources"]], ["Ancient", "Good"])
        self.assertIsNone(payload["sources"][0]["verdict"])
        self.assertEqual(len(payload["notes"]), 1)
        self.assertEqual(payload["notes"][0]["kind"], "skip")
        self.assertIn("predate", payload["notes"][0]["message"])
        self.assertIn("[skip]", err)                     # and it reached stderr
        self.assertNotIn("[skip]", out)

    def test_json_stays_valid_when_the_csv_is_rejected(self):
        p = write_csv(self.tmp, "bad2.csv", ["1,2"], header="a,b")
        code, out, err = run_cli([p, "--format", "json"])
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["sources"], [])
        self.assertEqual(payload["notes"][0]["kind"], "error")

    def test_text_mode_diagnostics_go_to_stdout(self):
        """Deliberate asymmetry: only --format text writes diagnostics to stdout."""
        p = write_csv(self.tmp, "bad3.csv", ["1,2"], header="a,b")
        code, out, err = run_cli([p])
        self.assertIn("[error]", out)
        self.assertEqual(err, "")

    def test_no_pool_reports_no_borrowing(self):
        code, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json", "--no-pool"])
        payload = json.loads(out)
        self.assertFalse(payload["params"]["pool"])
        for s in payload["sources"]:
            self.assertEqual(s["best"]["borrowed"], 0.0)
            self.assertEqual(s["best"]["group_n"], 1)

    def test_params_echo_the_run(self):
        code, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json",
                                "--pool-radius", "15", "--harmonics", "2"])
        params = json.loads(out)["params"]
        self.assertEqual(params["pool_radius_km"], 15.0)
        self.assertEqual(params["harmonics"], 2)
        self.assertEqual(params["windows"], forecast.WINDOWS)


class TestRun(OfflineTestCase):
    """Issue #24: run() is main() minus the CLI, so a host doesn't reimplement it.

    A service that copied the three passes returned 500s when analyze_base() gained
    its n == 0 case and the copy still only checked `is None`. These tests exist so
    that class of drift fails here instead of in production."""
    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def sources(self, path=EXAMPLE_CSV):
        return forecast.load_sources([path])

    def test_matches_the_cli_json_exactly(self):
        """The parity check: same input, same answer, whichever entry point."""
        payload = forecast.run(self.sources(), ASOF)
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        self.assertEqual(payload, json.loads(out))

    def test_a_source_with_nothing_usable_gets_no_verdict_not_a_crash(self):
        """The exact production failure: zero usable reports used to reach
        finalize() and raise KeyError: 'ctrl'. Since #8 such a source reaches the
        payload deliberately -- with rain context and a null verdict -- so this
        also guards the path that now has to build one."""
        p = write_csv(self.tmp, "old.csv", [
            "Ancient,34.09,-111.47,1999-01-01,0.5",
            "Ancient,34.09,-111.47,2001-01-01,0.5",
            "Good,34.09,-111.45,2024-01-01,1.0",
            "Good,34.09,-111.45,2024-06-01,0.0",
        ])
        payload = forecast.run(self.sources(p), ASOF)
        self.assertEqual([s["name"] for s in payload["sources"]], ["Ancient", "Good"])
        ancient = payload["sources"][0]
        self.assertEqual(ancient["n"], 0)
        self.assertIsNone(ancient["verdict"])
        self.assertTrue(ancient["rain_percentiles"])         # ...but rain is there
        self.assertEqual(len(payload["notes"]), 1)
        self.assertEqual(payload["notes"][0]["kind"], "skip")
        self.assertEqual(payload["notes"][0]["source"], "Ancient")
        # and the note carries the real explanation, not the old bare wording
        self.assertIn("predate", payload["notes"][0]["message"])
        self.assertNotEqual(payload["notes"][0]["message"], "no reports within precip range")

    def test_skip_wording_matches_the_cli(self):
        p = write_csv(self.tmp, "old2.csv", ["A,34.09,-111.47,1999-01-01,0.5"])
        payload = forecast.run(self.sources(p), ASOF)
        _, _, err = run_cli([p, "--asof", "2026-07-13", "--format", "json"])
        self.assertIn(payload["notes"][0]["message"], err)

    def test_options_are_honoured(self):
        payload = forecast.run(self.sources(), ASOF, pool=False, harmonics=2,
                               pool_radius_km=15.0, use_cache=False)
        self.assertEqual(payload["params"], {"engine_version": forecast.__version__,
                                             "precip": "open-meteo", "radar": "none",
                                             "pool": False, "pool_radius_km": 15.0,
                                             "harmonics": 2, "cache": False,
                                             "windows": forecast.WINDOWS})
        for s in payload["sources"]:
            self.assertEqual(s["best"]["borrowed"], 0.0)

    def test_pooling_happens_by_default(self):
        payload = forecast.run(self.sources(), ASOF)
        self.assertTrue(any(s["best"]["borrowed"] > 0 for s in payload["sources"]))

    def test_notes_can_be_preseeded_by_the_caller(self):
        """A host that rejected one input before calling still reports it."""
        notes = [{"kind": "error", "source": None, "message": "second file unreadable"}]
        payload = forecast.run(self.sources(), ASOF, notes=notes)
        self.assertEqual(payload["notes"][0]["message"], "second file unreadable")

    def test_on_note_streams_notes_as_they_happen(self):
        seen = []
        p = write_csv(self.tmp, "old3.csv", ["A,34.09,-111.47,1999-01-01,0.5"])
        forecast.run(self.sources(p), ASOF, on_note=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["kind"], "skip")

    def test_asof_defaults_to_today(self):
        payload = forecast.run(self.sources())
        self.assertEqual(payload["asof"], date.today().isoformat())

    def test_a_failing_source_becomes_a_note_not_an_exception(self):
        boom = source(name="Boom", reports=[(date(2024, 1, 1), 1.0)])
        def bad(lat, lon, end_date, use_cache=True):
            if abs(lat - boom["lat"]) < 1e-9:
                raise RuntimeError("provider exploded")
            return load_fixture_series(lat, lon)
        forecast.PRECIP_PROVIDER = bad
        payload = forecast.run(self.sources() + [boom], ASOF)
        kinds = {n["source"]: n["kind"] for n in payload["notes"]}
        self.assertEqual(kinds.get("Boom"), "error")
        self.assertEqual(len(payload["sources"]), 3)      # the others still ran

    def test_the_whole_embedding_path_needs_no_private_api(self):
        """load_sources_from -> run, exactly as the README tells a host to do it."""
        with open(EXAMPLE_CSV) as f:
            body = f.read()
        payload = forecast.run(
            forecast.load_sources_from([io.StringIO(body)], labels=["<request>"]), ASOF)
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        self.assertEqual(payload, json.loads(out))



# =========================================================================== #
# Markdown export -- issue #20
# =========================================================================== #
class TestMarkdownExport(OfflineTestCase):
    """The summary table, pasteable.

    What these guard is not the pipes and dashes -- it is that the table stays
    readable AWAY from the run that produced it. Everything below is a way the
    export could silently become a table of numbers with no provenance and no
    caveat, which is the one shape #20 said it must never take."""
    ASOF = "2026-07-13"

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def md(self, args=()):
        code, out, err = run_cli([EXAMPLE_CSV, "--asof", self.ASOF,
                                  "--format", "markdown"] + list(args))
        self.assertEqual(code, 0, err)
        return out

    def test_it_is_a_markdown_table_with_a_row_per_scored_source(self):
        out = self.md()
        rows = [l for l in out.splitlines() if l.startswith("| ")]
        self.assertEqual(len(rows), 5)                 # header + separator + 3 sources
        self.assertTrue(rows[1].startswith("| --- |"))
        for name in ("Chilson Spring", "Castersen Seep"):
            self.assertTrue(any(name in r for r in rows[2:]), name)

    def test_the_columns_are_the_same_ones_the_text_table_has(self):
        """One column list, two emitters. A column added to the terminal report and
        not to the export is the drift this shares SUMMARY_COLUMNS to prevent."""
        header = next(l for l in self.md().splitlines() if l.startswith("| "))
        for col in forecast.SUMMARY_COLUMNS:
            self.assertIn(col.replace("*", "\\*"), header, col)

    def test_rows_are_ordered_most_reliable_first(self):
        rows = [l for l in self.md().splitlines() if l.startswith("| ")][2:]
        pct = [int(r.split("|")[3].strip().rstrip("%")) for r in rows]
        self.assertEqual(pct, sorted(pct))

    def test_names_are_not_truncated(self):
        """The text table clips to 26 columns because a terminal has 80. Trip notes
        do not, and "Big Kahuna Falls - Mazatza.." is a name you would have to come
        back to the tool to resolve."""
        out = self.md()
        self.assertIn("Big Kahuna Falls - Mazatzal Wilderness", out)
        self.assertNotIn("..", out.split("SOURCE")[1].split(">")[0])

    def test_it_carries_the_legend_and_the_caveat(self):
        """#20's closing note: a table pasted into notes with no ERA5/monsoon
        warning is the one most likely to be read months later, out of context."""
        out = self.md()
        self.assertIn("season-controlled Spearman", out)
        self.assertIn("ERA5 misses monsoon cells", out)

    def test_the_caveats_do_not_point_at_a_report_that_is_not_there(self):
        """Standalone output cannot say "above" -- there is nothing above it."""
        tail = self.md().split("SOURCE")[-1]
        for pointer in (" above", "in full above"):
            self.assertNotIn(pointer, tail)

    def test_the_asterisk_in_r_star_is_escaped(self):
        """r* appears twice in the legend, which is exactly enough for a renderer to
        italicize everything between them and eat both asterisks -- turning the
        definition of a column into a typographic accident."""
        legend = next(l for l in self.md().splitlines() if "Spearman" in l)
        self.assertIn("r\\*", legend)
        self.assertNotIn("r* ", legend)

    def test_a_pipe_in_a_source_name_cannot_break_the_table(self):
        p = write_csv(self.tmp, "pipe.csv",
                      ["A|B Spring,34.09,-111.47,2024-03-01,0.5",
                       "A|B Spring,34.09,-111.47,2024-06-01,0.2"])
        code, out, _ = run_cli([p, "--asof", self.ASOF, "--format", "markdown"])
        self.assertEqual(code, 0)
        row = next(l for l in out.splitlines() if "Spring" in l and l.startswith("| "))
        self.assertIn("A\\|B Spring", row)
        self.assertEqual(row.count(" | "), len(forecast.SUMMARY_COLUMNS) - 1)

    def test_it_states_the_run_it_came_from(self):
        """As-of date, precip product and engine version, because the premise of this
        format is that it is read where the run's parameters are long gone."""
        line = next(l for l in self.md().splitlines() if l.startswith("_As of"))
        self.assertIn(self.ASOF, line)
        self.assertIn("open-meteo", line)
        self.assertIn(forecast.__version__, line)

    def test_a_non_default_precip_product_is_named_in_the_caveat(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            forecast.print_markdown_table(self._rows(), "iem:mrms")
        self.assertIn("iem:mrms", out.getvalue())
        self.assertIn("NOT the default ERA5", out.getvalue())

    def test_one_source_still_gets_a_table(self):
        """The text report suppresses the summary for a single source -- there is a
        full block right above it. In Markdown the table IS the output, so
        suppressing it would make the command print nothing at all."""
        p = write_csv(self.tmp, "one.csv",
                      ["Solo,34.09,-111.47,2024-03-01,0.5",
                       "Solo,34.09,-111.47,2024-06-01,0.2"])
        code, out, _ = run_cli([p, "--asof", self.ASOF, "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertIn("| Solo |", out)

    def test_a_rain_only_source_is_listed_but_given_no_verdict(self):
        p = write_csv(self.tmp, "rainonly.csv", ["Nowhere,34.09,-111.47,,"])
        code, out, err = run_cli([p, "--asof", self.ASOF, "--format", "markdown"])
        self.assertEqual(code, 0, err)
        self.assertIn("No verdict", out)
        self.assertIn("Nowhere", out)
        self.assertIn("for this date", out)
        self.assertNotIn("| Nowhere |", out)      # never a row in the scored table

    def test_a_constant_read_keeps_its_star_and_its_legend_line(self):
        """#39's `*` reaches the export because it rides in summary_cells(), not in
        either emitter. This is the case the shared-content split exists for: the
        table sorts by %DRY, so a source with one non-dry report sorts to the TOP,
        and the pasted copy is the one read furthest from the evidence."""
        p = write_csv(self.tmp, "star.csv",
                      ["Solo Tank,34.09,-111.47,2024-03-01,0.8",
                       "Steady Creek,34.09,-111.45,2024-01-15,0.6",
                       "Steady Creek,34.09,-111.45,2024-03-20,0.4",
                       "Steady Creek,34.09,-111.45,2024-06-10,0.2",
                       "Steady Creek,34.09,-111.45,2024-09-05,0.5",
                       "Steady Creek,34.09,-111.45,2025-02-11,0.7",
                       "Steady Creek,34.09,-111.45,2025-05-19,0.3",
                       "Steady Creek,34.09,-111.45,2025-08-22,0.1"])
        code, out, err = run_cli([p, "--asof", self.ASOF, "--format", "markdown"])
        self.assertEqual(code, 0, err)
        solo = next(l for l in out.splitlines() if l.startswith("| Solo Tank |"))
        steady = next(l for l in out.splitlines() if l.startswith("| Steady Creek |"))
        self.assertTrue(solo.rstrip().endswith("\\* |"), solo)   # escaped, so it renders
        self.assertFalse(steady.rstrip().endswith("\\* |"), steady)
        self.assertIn("read off every report the source has", out)
        # n=1 with one non-dry report sorts ABOVE the source with seven of them.
        self.assertLess(out.index(solo), out.index(steady))

    def test_no_star_means_no_star_legend(self):
        """A legend entry for a mark nobody can see is noise; an unexplained mark is
        worse. The worked example has no source at n <= ANALOG_K."""
        out = self.md()
        self.assertNotIn("read off every report the source has", out)
        rows = [l for l in out.splitlines() if l.startswith("| ")][2:]
        self.assertTrue(rows)
        for r in rows:
            self.assertFalse(r.rstrip().endswith("\\* |"), r)

    def test_diagnostics_go_to_stderr_so_the_paste_is_clean(self):
        """A [skip] line interleaved with the table would travel into the trip notes
        looking like part of the reading."""
        p = write_csv(self.tmp, "old.csv", ["Ancient,34.09,-111.47,1999-01-01,0.5"])
        code, out, err = run_cli([p, "--asof", self.ASOF, "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertIn("[skip]", err)
        self.assertNotIn("[skip]", out)

    def test_a_source_whose_reports_all_predate_the_record_keeps_its_rain(self):
        """It is not dropped: with no usable reports it still gets the rain context
        #8 gives a bare coordinate, which is the whole reason it has no table row."""
        p = write_csv(self.tmp, "old.csv", ["Gone,34.09,-111.47,1999-01-01,0.5"])
        _, out, _ = run_cli([p, "--asof", self.ASOF, "--format", "markdown"])
        self.assertIn("No verdict", out)
        self.assertIn("- **Gone**", out)
        self.assertNotIn("| Gone |", out)

    def test_nothing_analysable_says_so_instead_of_printing_an_empty_table(self):
        """A bare header, a separator and no rows reads as "everything is fine and
        there is no water", which is the opposite of what happened."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            forecast.print_markdown_table([])
        self.assertNotIn("| ---", out.getvalue())
        self.assertIn("No sources could be read", out.getvalue())

    def _rows(self):
        """The analysed rows the CLI would render, without the CLI."""
        return forecast._analyse(forecast.load_sources([EXAMPLE_CSV]),
                                date.fromisoformat(self.ASOF), True, 1, True,
                                forecast.POOL_RADIUS_KM, lambda *a, **k: None)



class TestSummaryContentIsSharedByBothEmitters(OfflineTestCase):
    """The refactor #20 required: cells and prose built once, rendered twice."""
    def test_both_emitters_read_the_same_cells(self):
        rows = forecast._analyse(forecast.load_sources([EXAMPLE_CSV]),
                                 date(2026, 7, 13), True, 1, True,
                                 forecast.POOL_RADIUS_KM, lambda *a, **k: None)
        scored, _ = forecast.summary_sections(rows)
        text, md = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(text):
            forecast.print_table(rows)
        with contextlib.redirect_stdout(md):
            forecast.print_markdown_table(rows)
        for a in scored:
            # Every cell but the name, which the text emitter is allowed to clip.
            for cell in forecast.summary_cells(a)[1:]:
                self.assertIn(cell, text.getvalue(), cell)
                self.assertIn(cell.replace("*", "\\*"), md.getvalue(), cell)

    def test_the_caveat_switches_on_the_precip_product_in_both(self):
        self.assertIn("NOT the default ERA5",
                      " ".join(forecast.summary_caveats([], "iem:mrms")))
        self.assertIn("NOT the default ERA5",
                      " ".join(forecast.summary_caveats([], "iem:mrms", standalone=True)))
        self.assertIn("ERA5 misses monsoon cells",
                      " ".join(forecast.summary_caveats([], forecast.DEFAULT_PRECIP)))

class TestJsonSchema(OfflineTestCase):
    """The --format json payload is an API the site reads; renaming a field breaks it."""
    def setUp(self):
        super().setUp()
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        self.payload = json.loads(out)
        self.src = self.payload["sources"][0]

    def test_top_level_keys(self):
        self.assertEqual(sorted(self.payload), ["asof", "notes", "params", "sources"])

    def test_source_keys(self):
        self.assertEqual(sorted(self.src), sorted([
            "name", "lat", "lon", "n", "small_n", "reports", "pct_dry", "mean_flow",
            "annual_precip_in", "type", "mean_flow_by_month", "correlations", "best",
            "asof", "precip_in", "predicted_flow", "verdict", "harmonics",
            "analog_n", "pred_is_constant",
            "rain_percentiles", "neighbors", "neighbors_disagree",
            "radar_check"]))

    def test_rain_percentile_rows_cover_every_window(self):
        rain = self.src["rain_percentiles"]
        self.assertEqual(sorted(int(w[:-1]) for w in rain), sorted(forecast.WINDOWS))
        for w, v in rain.items():
            self.assertEqual(sorted(v), ["inches", "median_in", "n_years", "pct"])
            self.assertGreaterEqual(v["pct"], 0)
            self.assertLessEqual(v["pct"], 100)
            self.assertGreater(v["n_years"], 0)

    def test_best_keys(self):
        self.assertEqual(sorted(self.src["best"]), sorted([
            "window", "days", "r", "own_ctrl_r", "raw_r", "borrowed", "group_n",
            "signal_check"]))

    def test_reports_keys(self):
        self.assertEqual(sorted(self.src["reports"]), sorted([
            "total", "used", "excluded_before_precip", "excluded_after_precip",
            "precip_span"]))

    def test_correlation_rows_cover_every_window(self):
        got = sorted(c["days"] for c in self.src["correlations"])
        self.assertEqual(got, sorted(forecast.WINDOWS))
        for c in self.src["correlations"]:
            self.assertEqual(sorted(c), ["ctrl_r", "days", "raw_r", "window"])

    def test_sources_are_in_input_order(self):
        names = [s["name"] for s in self.payload["sources"]]
        want = [s["name"] for s in forecast.load_sources([EXAMPLE_CSV])]
        self.assertEqual(names, want)

    def test_reports_used_matches_n(self):
        for s in self.payload["sources"]:
            self.assertEqual(s["reports"]["used"], s["n"])

    def test_small_n_matches_the_threshold(self):
        for s in self.payload["sources"]:
            self.assertEqual(s["small_n"], s["n"] < 25)

    def test_json_is_serialisable_and_round_trips(self):
        self.assertEqual(json.loads(json.dumps(self.payload)), self.payload)


# =========================================================================== #
# Golden regression: the whole payload, on the worked example
# =========================================================================== #
class TestGolden(OfflineTestCase):
    """Real ERA5 fixture in, full --format json payload out, compared field by field.

    This is the test that catches silent numeric drift from a refactor -- the
    failure mode nobody can eyeball, in a tool whose wrong answers look just as
    plausible as its right ones. Regenerate deliberately (see tests/README.md)
    and read the diff before accepting it."""
    GOLDEN = os.path.join(FIXTURES, "golden-mazatzal.json")

    def test_matches_the_recorded_payload(self):
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        got = json.loads(out)
        with open(self.GOLDEN) as f:
            want = json.load(f)
        self.assertEqual(got["params"], want["params"])
        self.assertEqual(got["asof"], want["asof"])
        self.assertEqual(len(got["sources"]), len(want["sources"]))
        for g, w in zip(got["sources"], want["sources"]):
            self.assertEqual(g, w, f"payload drift for {w['name']}")
        self.assertEqual(got["notes"], want["notes"])

    def test_the_documented_headline_numbers(self):
        """Spelled out separately from the golden blob: these exact numbers appear
        in the README and in the PR history, so a change here is a docs change."""
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        by_name = {s["name"]: s for s in json.loads(out)["sources"]}
        kahuna = by_name["Big Kahuna Falls - Mazatzal Wilderness"]
        castersen = by_name["Castersen Seep"]
        chilson = by_name["Chilson Spring"]

        self.assertEqual(kahuna["n"], 160)
        self.assertEqual(castersen["n"], 15)
        self.assertEqual(chilson["n"], 58)

        self.assertEqual(kahuna["best"]["window"], "30d")
        self.assertEqual(castersen["best"]["window"], "60d")
        self.assertEqual(chilson["best"]["window"], "90d")

        # the hydrologic-memory spectrum: buffered spring, flashy falls
        self.assertEqual(chilson["type"], "Reliable (groundwater-buffered)")
        self.assertEqual(kahuna["type"], "Flashy (needs recent rain)")
        self.assertLessEqual(chilson["pct_dry"], 10)

        # small-n Castersen leans hardest on its neighbours; data-rich Kahuna least
        self.assertGreater(castersen["best"]["borrowed"], chilson["best"]["borrowed"])
        self.assertGreater(chilson["best"]["borrowed"], kahuna["best"]["borrowed"])
        self.assertTrue(castersen["small_n"])

        # season control collapsed Castersen's headline: raw 180d .72 -> ctrl ~.09
        c180 = next(c for c in castersen["correlations"] if c["window"] == "180d")
        self.assertGreater(c180["raw_r"], 0.70)
        self.assertLess(abs(c180["ctrl_r"]), 0.15)

        # #8, and quoted in the README: a dry winter (180d, 21st pct) sitting inside
        # an ordinary year (365d, 61st) -- the reading the verdict alone can't give.
        rain = castersen["rain_percentiles"]
        self.assertEqual(rain["180d"]["pct"], 21)
        self.assertEqual(rain["365d"]["pct"], 61)
        self.assertEqual(rain["180d"]["n_years"], 19)
        # all three share one ERA5 cell, so the rain context is identical
        self.assertEqual(chilson["rain_percentiles"], rain)


# =========================================================================== #
# It still runs as a script
# =========================================================================== #
class TestVersioning(OfflineTestCase):
    """The engine has an identity, and every payload carries it (#26)."""
    def test_version_is_a_sane_string(self):
        parts = forecast.__version__.split(".")
        self.assertEqual(len(parts), 3, forecast.__version__)
        self.assertTrue(all(p.isdigit() for p in parts), forecast.__version__)

    def test_version_flag(self):
        code, out, err = run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(forecast.__version__, out)
        self.assertEqual(err, "")

    def test_version_flag_short_circuits_before_reading_input(self):
        """--version must work with no CSV, and must not try to analyse one."""
        code, out, _ = run_cli(["--version", "/does/not/exist.csv"])
        self.assertEqual(code, 0)
        self.assertNotIn("[error]", out)

    def test_every_payload_is_stamped(self):
        payload = forecast.run(forecast.load_sources([EXAMPLE_CSV]), ASOF)
        self.assertEqual(payload["params"]["engine_version"], forecast.__version__)

    def test_the_cli_payload_is_stamped_too(self):
        _, out, _ = run_cli([EXAMPLE_CSV, "--asof", "2026-07-13", "--format", "json"])
        self.assertEqual(json.loads(out)["params"]["engine_version"], forecast.__version__)

    def test_pyproject_reads_the_version_from_the_module(self):
        """One source of truth: if pyproject ever hardcodes a number instead of
        reading __version__, the two can drift and a release lies about itself."""
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            pyproject = f.read()
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertIn('attr = "backcountry_water_oracle.__version__"', pyproject)
        self.assertNotRegex(pyproject, r'(?m)^version\s*=\s*"')

    def test_console_script_target_takes_no_arguments(self):
        """console_scripts calls its target with no args; main(argv) would
        TypeError. This is the bug that would greet the first person to install."""
        import inspect
        self.assertEqual(len(inspect.signature(forecast.cli).parameters), 0)
        entry = 'water-forecast = "backcountry_water_oracle:cli"'
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn(entry, f.read())


class TestRunsAsAScript(unittest.TestCase):
    def test_script_entry_point(self):
        """One subprocess, on a path that fails before any network call."""
        r = subprocess.run([sys.executable, os.path.join(ROOT, "forecast.py"), "--asof"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("[error]", r.stderr)
        self.assertIn("requires a value", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
