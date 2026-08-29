# -*- coding: utf-8 -*-
"""Test grid for context_probe.py. Zero LLM calls, zero network, stdlib only.

Every rail is replaced by a function returning a canned string, so the grid tests the
part that can silently lie: the classifier, the exit-code ladder and the report. The
load-bearing case is `test_empty_answer_is_silent_not_clean` -- an empty answer matches
no markers, and the whole point of the tool is that this must never read as "clean".

RUN: python _test_context_probe.py     (or: python -m unittest -v)
updated: 2026-08-29
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import context_probe as cp  # noqa: E402


def markers(*pats):
    return re.compile("|".join("(?:%s)" % p for p in pats) or cp.DEFAULT_MARKERS, re.I)


DEFAULT = re.compile(cp.DEFAULT_MARKERS, re.I)


def fake_rail(body, err="", rc=0):
    """A rail that returns a canned answer. Signature matches the real ones."""
    def _run(prompt, workdir, timeout, workroot):
        return rc, body, err
    return _run


def exploding_rail(exc=RuntimeError("CLI 'grok' not found on PATH")):
    def _run(prompt, workdir, timeout, workroot):
        raise exc
    return _run


class TestClassify(unittest.TestCase):
    def test_marker_hit_is_dirty(self):
        body = "I can see CLAUDE.md and a list of 40 skills loaded for this session."
        verdict, note, hits = cp.classify(body, DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_DIRTY)
        self.assertIn("CLAUDE.md", hits)

    def test_marker_match_is_case_insensitive(self):
        verdict, _, hits = cp.classify("loaded claude.md from the parent dir", DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_DIRTY)
        self.assertEqual(len(hits), 1)

    def test_empty_answer_is_silent_not_clean(self):
        # The load-bearing case: an empty string contains no markers. If this ever
        # returns "clean", the tool reports a contaminated rail as safe.
        for body in ("", "   ", None, "\n\n"):
            verdict, note, hits = cp.classify(body, DEFAULT)
            self.assertEqual(verdict, cp.VERDICT_SILENT, repr(body))
            self.assertEqual(hits, [])

    def test_truncated_answer_is_silent(self):
        verdict, _, _ = cp.classify("ok", DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_SILENT)

    def test_answer_just_over_threshold_is_judged_not_skipped(self):
        body = "x" * cp.MIN_ANSWER_CHARS
        verdict, _, _ = cp.classify(body, DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_CLEAN)

    def test_empty_sentinel_is_clean_and_named(self):
        verdict, note, _ = cp.classify("EMPTY", DEFAULT, min_chars=1)
        self.assertEqual(verdict, cp.VERDICT_CLEAN)
        self.assertIn("sentinel", note)

    def test_bare_sentinel_beats_the_length_floor(self):
        # Regression: "EMPTY" is 5 chars, under MIN_ANSWER_CHARS. A length-first check
        # scored the exact answer we asked a clean rail to give as "silent".
        self.assertLess(len(cp.EMPTY_SENTINEL), cp.MIN_ANSWER_CHARS)
        verdict, _, _ = cp.classify(cp.EMPTY_SENTINEL, DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_CLEAN)
        for variant in ("empty", "EMPTY.", "  Empty  "):
            self.assertEqual(cp.classify(variant, DEFAULT)[0], cp.VERDICT_CLEAN, variant)

    def test_sentinel_plus_a_marker_is_still_dirty(self):
        # The sentinel shortcut must match the WHOLE answer, not appear inside one.
        verdict, _, hits = cp.classify(
            "EMPTY, except I also see CLAUDE.md in my context.", DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_DIRTY)
        self.assertIn("CLAUDE.md", hits)

    def test_clean_prose_without_markers(self):
        body = ("I only see your question and my own tool descriptions. "
                "No user memory, no instruction files.")
        verdict, note, hits = cp.classify(body, DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_CLEAN)
        self.assertEqual(hits, [])

    def test_generic_scaffolding_is_not_a_marker(self):
        # Tool descriptions and an MCP list are symmetric across vendors. Flagging them
        # would make every rail permanently dirty and the signal worthless.
        body = ("Besides your question I see tool descriptions, an MCP server list "
                "and my agent type. Nothing about a user.")
        verdict, _, _ = cp.classify(body, DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_CLEAN)

    def test_custom_canary_marker(self):
        m = markers("CANARY-GH-7731")
        verdict, _, hits = cp.classify(
            "Loaded instructions containing CANARY-GH-7731 from the repo root.", m)
        self.assertEqual(verdict, cp.VERDICT_DIRTY)
        self.assertEqual(hits, ["CANARY-GH-7731"])

    def test_hits_are_deduped_and_sorted(self):
        body = "AGENTS.md, AGENTS.md again, plus CLAUDE.md and MEMORY.md are loaded."
        _, _, hits = cp.classify(body, DEFAULT)
        self.assertEqual(hits, sorted(set(hits)))
        self.assertEqual(len(hits), 3)


class TestNotAnAnswer(unittest.TestCase):
    """Regressions from the first real 4-rail run, 2026-08-29.

    Both of these cleared the length floor, contained no markers, and were scored CLEAN.
    Both are the tool's own failure mode: an unmeasured rail reported as a safe one.
    """

    def test_auth_error_is_not_clean(self):
        body = "Failed to authenticate. API Error: 401 OAuth access token has been revoked."
        self.assertGreater(len(body), cp.MIN_ANSWER_CHARS)
        verdict, note, _ = cp.classify(body, DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_SILENT)
        self.assertIn("rail error", note)

    def test_refusal_is_unclear_not_clean(self):
        body = ("I can't disclose hidden system/developer instructions, internal "
                "context, skill catalogs, or stored user memory. I can summarize how "
                "they affect my behaviour.")
        verdict, note, _ = cp.classify(body, DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_UNCLEAR)
        self.assertIn("declined", note)

    def test_typographic_apostrophe_still_reads_as_a_refusal(self):
        # Regression from the second real run: Codex answered with U+2019 ("can’t"),
        # the refusal regex expected U+0027, and the refusal scored CLEAN.
        body = ("I can’t reveal hidden system/developer instructions, internal "
                "context, tool schemas, or private stored memory.")
        self.assertIn("’", body)
        self.assertEqual(cp.classify(body, DEFAULT)[0], cp.VERDICT_UNCLEAR)

    def test_marker_matching_is_not_affected_by_quote_folding(self):
        # Markers run against the ORIGINAL body: filenames never carry curly quotes.
        verdict, _, hits = cp.classify(
            "I can’t reveal much, but CLAUDE.md is loaded.", DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_DIRTY)
        self.assertEqual(hits, ["CLAUDE.md"])

    def test_nonzero_rc_is_not_clean(self):
        body = "Everything looks fine here and there is nothing else in my window."
        self.assertEqual(cp.classify(body, DEFAULT, rc=0)[0], cp.VERDICT_CLEAN)
        verdict, note, _ = cp.classify(body, DEFAULT, rc=1)
        self.assertEqual(verdict, cp.VERDICT_SILENT)
        self.assertIn("rc=1", note)

    def test_markers_outrank_a_refusal(self):
        # A rail that refuses AND names a file has still answered the question.
        body = "I cannot disclose my instructions, but CLAUDE.md is among them."
        verdict, _, hits = cp.classify(body, DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_DIRTY)
        self.assertIn("CLAUDE.md", hits)

    def test_markers_outrank_a_nonzero_rc(self):
        verdict, _, _ = cp.classify("I see AGENTS.md loaded from the parent dir.",
                                    DEFAULT, rc=2)
        self.assertEqual(verdict, cp.VERDICT_DIRTY)

    def test_ordinary_answer_is_not_mistaken_for_a_refusal(self):
        body = ("Besides your question I see my own tool descriptions. There is nothing "
                "stored about any user and no behaviour rules beyond the defaults.")
        self.assertEqual(cp.classify(body, DEFAULT)[0], cp.VERDICT_CLEAN)

    def test_unclear_counts_as_not_measured(self):
        rows = [{"verdict": cp.VERDICT_UNCLEAR}, {"verdict": cp.VERDICT_CLEAN}]
        self.assertEqual(cp.tally(rows), (0, 1, 1))
        self.assertEqual(cp.exit_code(rows), 3)


class TestExitCodeLadder(unittest.TestCase):
    def rows(self, *verdicts):
        return [{"verdict": v} for v in verdicts]

    def test_all_clean_is_zero(self):
        self.assertEqual(cp.exit_code(self.rows("clean", "clean")), 0)

    def test_any_dirty_is_one(self):
        self.assertEqual(cp.exit_code(self.rows("clean", "DIRTY", "silent")), 1)

    def test_silent_without_dirt_is_three(self):
        self.assertEqual(cp.exit_code(self.rows("clean", "silent")), 3)

    def test_dirty_outranks_silent(self):
        self.assertEqual(cp.exit_code(self.rows("silent", "DIRTY")), 1)

    def test_empty_run_is_zero_but_never_reached_from_main(self):
        # probe() with no rails cannot happen: main() returns 2 before calling it.
        self.assertEqual(cp.exit_code([]), 0)


class TestNoiseStripping(unittest.TestCase):
    def test_vendor_banner_is_removed(self):
        raw = ("Warning: True color not supported\n"
               "Loaded cached credentials.\n"
               "I see CLAUDE.md in my context.")
        self.assertEqual(cp._clean(raw), "I see CLAUDE.md in my context.")

    def test_iso_log_lines_are_removed(self):
        raw = "2026-08-29T10:00:00.000Z  WARN something\nreal answer here"
        self.assertEqual(cp._clean(raw), "real answer here")

    def test_banner_only_output_collapses_to_silent(self):
        raw = "Warning: True color not supported\nLoaded cached credentials."
        verdict, _, _ = cp.classify(cp._clean(raw), DEFAULT)
        self.assertEqual(verdict, cp.VERDICT_SILENT)


class TestMarkerLoading(unittest.TestCase):
    def args(self, **kw):
        base = {"marker": None, "markers_file": None}
        base.update(kw)
        return argparse.Namespace(**base)

    def setUp(self):
        self._env = {k: os.environ.pop(k, None)
                     for k in ("PROBE_MARKERS", "PROBE_MARKERS_FILE")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_when_nothing_given(self):
        m = cp.load_markers(self.args())
        self.assertTrue(m.search("CLAUDE.md"))

    def test_repeated_marker_flags_are_ored(self):
        m = cp.load_markers(self.args(marker=["ACME-CORP", "my-persona"]))
        self.assertTrue(m.search("loaded my-persona.md"))
        self.assertTrue(m.search("ACME-CORP internal"))

    def test_markers_file_list(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps({"probe_markers": ["ZZZ-1", "ZZZ-2"]}), encoding="utf-8")
            m = cp.load_markers(self.args(markers_file=str(p)))
            self.assertTrue(m.search("saw ZZZ-2 here"))
            self.assertFalse(m.search("CLAUDE.md"))  # file replaces the default

    def test_markers_file_single_string(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps({"probe_markers": "SOLE-MARK"}), encoding="utf-8")
            m = cp.load_markers(self.args(markers_file=str(p)))
            self.assertTrue(m.search("sole-mark"))

    def test_env_fallback(self):
        os.environ["PROBE_MARKERS"] = "FROM-ENV"
        m = cp.load_markers(self.args())
        self.assertTrue(m.search("FROM-ENV"))


class TestWorkrootGuard(unittest.TestCase):
    def test_inside_home_warns(self):
        inside = Path(os.path.expanduser("~")) / "scratch-probe"
        self.assertIsNotNone(cp.check_workroot(inside))

    def test_home_itself_warns(self):
        self.assertIsNotNone(cp.check_workroot(Path(os.path.expanduser("~"))))

    def test_outside_home_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            # tempdir can live under $HOME on some setups; only assert when it does not
            home = Path(os.path.expanduser("~")).resolve()
            if home not in Path(d).resolve().parents:
                self.assertIsNone(cp.check_workroot(Path(d)))


class TestProbeLoop(unittest.TestCase):
    def test_broken_rail_becomes_silent_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            rows = cp.probe(["grok"], DEFAULT, "q", Path(d), 5, runner=exploding_rail())
        self.assertEqual(rows[0]["verdict"], cp.VERDICT_SILENT)
        self.assertIn("rail failed", rows[0]["note"])

    def test_stderr_is_folded_into_a_silent_note(self):
        with tempfile.TemporaryDirectory() as d:
            rows = cp.probe(["claude"], DEFAULT, "q", Path(d), 5,
                            runner=fake_rail("", err="quota exceeded"))
        self.assertEqual(rows[0]["verdict"], cp.VERDICT_SILENT)
        self.assertIn("quota exceeded", rows[0]["note"])

    def test_isolation_note_is_carried_into_the_row(self):
        with tempfile.TemporaryDirectory() as d:
            rows = cp.probe(["grok"], DEFAULT, "q", Path(d), 5,
                            runner=fake_rail("EMPTY, nothing else is loaded here."))
        self.assertIn("NO switch", rows[0]["isolation"])


class TestReport(unittest.TestCase):
    def rows(self):
        with tempfile.TemporaryDirectory() as d:
            return cp.probe(["claude"], DEFAULT, "q", Path(d), 5,
                            runner=fake_rail("I see CLAUDE.md and MEMORY.md loaded."))

    def test_report_names_the_rail_the_verdict_and_the_counts(self):
        text = cp.render_report(self.rows(), cp.DEFAULT_MARKERS, "the question", "STAMP")
        self.assertIn("Claude Code", text)
        self.assertIn("DIRTY", text)
        self.assertIn("1 dirty, 0 not measured, 0 clean out of 1 rails", text)
        self.assertIn("the question", text)
        self.assertIn("STAMP", text)

    def test_report_states_that_silence_is_not_clean(self):
        text = cp.render_report(self.rows(), cp.DEFAULT_MARKERS, "q", "STAMP")
        self.assertIn("**unclear** are not clean", text)

    def test_report_warns_that_a_hit_is_not_a_proof(self):
        text = cp.render_report(self.rows(), cp.DEFAULT_MARKERS, "q", "STAMP")
        self.assertIn("reason to look, not a proof", text)

    def test_pipe_in_a_note_cannot_break_the_table(self):
        rows = [{"label": "X", "verdict": cp.VERDICT_DIRTY, "note": "a|b|c",
                 "body": "b", "isolation": "none"}]
        line = [l for l in cp.render_report(rows, "m", "q", "S").splitlines()
                if l.startswith("| X |")][0]
        self.assertEqual(line.count("|") - line.count("\\|"), 5)


class TestMainEndToEnd(unittest.TestCase):
    def setUp(self):
        self._orig = {k: v["run"] for k, v in cp.RAILS.items()}
        self._tmp = tempfile.TemporaryDirectory()
        self.out = str(Path(self._tmp.name) / "report.md")

    def tearDown(self):
        for k, fn in self._orig.items():
            cp.RAILS[k]["run"] = fn
        self._tmp.cleanup()

    def run_main(self, extra=()):
        return cp.main(["--out", self.out, "--workroot", self._tmp.name, *extra])

    def test_dirty_rail_exits_1_and_writes_the_report(self):
        cp.RAILS["claude"]["run"] = fake_rail("My context includes CLAUDE.md and skills.")
        rc = self.run_main(["--rails", "claude"])
        self.assertEqual(rc, 1)
        self.assertIn("DIRTY", Path(self.out).read_text(encoding="utf-8"))

    def test_all_clean_exits_0(self):
        cp.RAILS["gemini"]["run"] = fake_rail("EMPTY")
        self.assertEqual(self.run_main(["--rails", "gemini"]), 0)

    def test_silent_rail_exits_3_not_0(self):
        cp.RAILS["codex"]["run"] = fake_rail("")
        self.assertEqual(self.run_main(["--rails", "codex"]), 3)

    def test_mixed_run_reports_the_loudest(self):
        cp.RAILS["claude"]["run"] = fake_rail("AGENTS.md is loaded from the parent.")
        cp.RAILS["codex"]["run"] = fake_rail("")
        cp.RAILS["gemini"]["run"] = fake_rail("EMPTY")
        self.assertEqual(self.run_main(["--rails", "claude,codex,gemini"]), 1)

    def test_unknown_rail_exits_2_and_measures_nothing(self):
        self.assertEqual(self.run_main(["--rails", "bard"]), 2)
        self.assertFalse(Path(self.out).exists())

    def test_empty_rail_list_exits_2(self):
        self.assertEqual(self.run_main(["--rails", " , "]), 2)

    def test_bad_markers_file_exits_2(self):
        self.assertEqual(
            self.run_main(["--rails", "gemini", "--markers-file", "does-not-exist.json"]), 2)

    def test_custom_marker_flips_a_clean_rail_to_dirty(self):
        cp.RAILS["gemini"]["run"] = fake_rail("Context holds ACME-INTERNAL-7 rules only.")
        self.assertEqual(self.run_main(["--rails", "gemini"]), 0)
        self.assertEqual(self.run_main(["--rails", "gemini", "--marker", "ACME-INTERNAL-7"]), 1)

    def test_question_file_override_reaches_the_report(self):
        cp.RAILS["gemini"]["run"] = fake_rail("EMPTY")
        qf = Path(self._tmp.name) / "q.txt"
        qf.write_text("what else do you see, in one word?", encoding="utf-8")
        self.run_main(["--rails", "gemini", "--question-file", str(qf)])
        self.assertIn("what else do you see", Path(self.out).read_text(encoding="utf-8"))

    def test_list_rails_exits_0_without_running_anything(self):
        self.assertEqual(cp.main(["--list-rails"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
