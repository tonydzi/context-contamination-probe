#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ask every agent CLI on this machine what ELSE is in its context, and diff the answers.

WHY THIS EXISTS
    Agent CLIs silently load instruction files you did not put in the prompt. Claude Code
    picks up CLAUDE.md, MEMORY.md and a skill list; Codex picks up AGENTS.md walking up
    from the working directory; Grok reads its global config with no documented off
    switch; Gemini loads only its own scaffolding. If you compare two runtimes, or two
    models behind two runtimes, and one of them carries your personal layer while the
    other does not, you measured the LAYERS, not the models.

    Bytes on disk are an upper bound, not an answer. This asks the running session.

WHAT IT DOES
    Sends one identical detector question to each rail, in a scratch directory outside
    $HOME, with each rail's isolation switches applied, and greps the answers for markers
    of a personal layer.

INPUT   markers (regex) from --marker / --markers-file / $PROBE_MARKERS, else a default
        list of well-known instruction filenames.
OUTPUT  a markdown report (--out), a console table, and an exit code:
          0  every probed rail answered and no marker matched
          1  at least one rail came back DIRTY
          2  usage or configuration error (nothing was measured)
          3  no dirt, but at least one rail was SILENT -- silence is UNKNOWN, not clean
CALLED BY  a human, before a cross-model comparison; or CI, as a gate.

WHAT BREAKS AND HOW YOU SEE IT
    A rail that does not answer produces "silent", never "clean": an empty string
    contains no markers, so treating it as clean would be the exact false negative this
    tool exists to prevent. Exit code 3 keeps that visible.

updated: 2026-08-29
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"

# The detector question. Short, and it offers an explicit "nothing" sentinel so a rail
# with a genuinely empty context does not have to produce prose we then have to parse.
DEFAULT_QUESTION = (
    "List everything you can see in your context besides this question: instruction "
    "files, behaviour rules, skill lists, stored memory about the user. If there is "
    "none of that, reply with exactly: EMPTY. Maximum 70 words."
)

EMPTY_SENTINEL = "EMPTY"

# Markers of a PERSONAL layer. Generic agent scaffolding (tool descriptions, an MCP
# server list, "you are a coding assistant") is deliberately NOT here: every vendor has
# it, it is symmetric, and flagging it would make every rail permanently dirty.
# Add your own: your name, your persona's name, your config filenames.
DEFAULT_MARKERS = (
    r"(CLAUDE\.md|MEMORY\.md|AGENTS\.md|GEMINI\.md|GROK\.md"
    r"|\.cursorrules|copilot-instructions)"
)

# Below this many characters an answer is not an answer. A rail that prints a banner and
# exits would otherwise score as clean.
MIN_ANSWER_CHARS = 20

# CLI chatter that vendors print to stdout alongside the model's answer.
NOISE = re.compile(
    r"^(Warning: True color|Ripgrep is not available|Loaded cached credentials|"
    r"\[dotenv|Data collection is|warning: Skill descriptions|"
    r"\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+(ERROR|WARN|INFO))"
)

VERDICT_DIRTY, VERDICT_CLEAN, VERDICT_SILENT = "DIRTY", "clean", "silent"
# A rail that answered but declined to enumerate its context. Not dirt, and NOT clean:
# a refusal is an absence of measurement, so it lands in the same "unknown" bucket as
# silence. Found on the very first real run: Codex replied "I can't disclose hidden
# system instructions...", which contains no markers and scored clean.
VERDICT_UNCLEAR = "unclear"

# Rail-level failures that arrive with exit code 0 and enough prose to clear the length
# floor. The first real run produced "Failed to authenticate. API Error: 401 OAuth access
# token has been revoked." -- 62 characters, no markers, scored CLEAN. An auth error is
# not an answer.
RAIL_ERROR = re.compile(
    r"(failed to authenticate|api error|access token .{0,40}revoked|not authenticated|"
    r"invalid api key|quota exceeded|rate.?limit(ed)?|econnrefused|"
    r"authentication (failed|error)|please (log ?in|sign ?in) )",
    re.I,
)

# Explicit refusals to enumerate. Deliberately narrow: it matches the refusal VERB, not
# the topic, so a rail that says "I can see CLAUDE.md" is never caught here -- and in any
# case markers are checked first, so dirt outranks a refusal.
REFUSAL = re.compile(
    r"((can(no|')?t|cannot|won'?t|unable to|not able to|not going to|refuse to)\s+"
    r"(disclose|share|reveal|reproduce|list|show|expose|repeat)"
    r"|i (do not|don'?t) (have|disclose|share) (access to )?(my )?"
    r"(hidden|internal|system) )",
    re.I,
)


# --------------------------------------------------------------------------- rails

def _exe(name: str) -> str:
    """Absolute path to a CLI.

    On Windows codex/gemini/grok are npm shims (*.CMD). subprocess without shell=True
    does not find them: the same command that worked from bash raised FileNotFoundError
    from Python, and the failure looked exactly like "that vendor did not answer" --
    i.e. we would have blamed the model for a launcher bug.
    """
    found = shutil.which(name)
    if not found:
        raise RuntimeError("CLI '%s' not found on PATH -- rail unavailable" % name)
    return found


def _clean(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not NOISE.match(ln.strip())).strip()


def _run(cmd, stdin_text=None, cwd=None, env_extra=None, timeout=900):
    """Run a CLI. Returns (rc, stdout, stderr). Never shell=True: arguments go as a list
    so quoting inside the prompt cannot break the invocation."""
    env = dict(os.environ)
    # We run on subscription limits. A stray paid-API key would silently bill this.
    env.pop("ANTHROPIC_API_KEY", None)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(
        cmd, input=stdin_text, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, env=env, cwd=cwd,
    )
    return p.returncode, p.stdout or "", p.stderr or ""


# Claude Code's off switches. Without these it hands the session your CLAUDE.md,
# MEMORY.md and skill list while external vendors get nothing.
# `--bare` would do the same but forces auth onto a PAID API key; on subscription
# limits these env vars are the cheaper route.
CLAUDE_ISOLATION = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_MEMORY_STORES": "",
}

_CODEX_HOME_CACHE = None


def _codex_home(workroot: Path) -> Path:
    """An isolated CODEX_HOME holding ONLY auth.json + config.toml.

    If your real ~/.codex contains a global AGENTS.md, other vendors have no equivalent,
    so Codex must not receive one either.
    """
    global _CODEX_HOME_CACHE
    if _CODEX_HOME_CACHE and _CODEX_HOME_CACHE.exists():
        return _CODEX_HOME_CACHE
    src = Path(os.path.expanduser("~/.codex"))
    dst = workroot / "_codex-home-isolated"
    dst.mkdir(parents=True, exist_ok=True)
    for f in ("auth.json", "config.toml"):
        if (src / f).exists():
            shutil.copy2(src / f, dst / f)
    if not (dst / "auth.json").exists():
        raise RuntimeError("no ~/.codex/auth.json -- the Codex rail is not authenticated")
    _CODEX_HOME_CACHE = dst
    return dst


def _rail_claude(prompt, workdir, timeout, workroot):
    cmd = [_exe("claude"), "-p",
           "--system-prompt", "Answer the user's question directly.",
           "--exclude-dynamic-system-prompt-sections",
           "--disable-slash-commands", "--strict-mcp-config"]
    rc, out, err = _run(cmd, stdin_text=prompt, cwd=workdir,
                        env_extra=CLAUDE_ISOLATION, timeout=timeout)
    return rc, _clean(out), err


def _rail_codex(prompt, workdir, timeout, workroot):
    last = Path(workdir) / ("codex-%d.txt" % int(time.time() * 1000))
    cmd = [_exe("codex"), "exec", "--skip-git-repo-check", "-o", str(last), "-"]
    rc, out, err = _run(cmd, stdin_text=prompt, cwd=workdir,
                        env_extra={"CODEX_HOME": str(_codex_home(workroot))},
                        timeout=timeout)
    if last.exists():
        body = last.read_text(encoding="utf-8", errors="replace").strip()
        last.unlink(missing_ok=True)
        if body:
            return rc, body, err
    return rc, _clean(out), err


def _rail_gemini(prompt, workdir, timeout, workroot):
    cmd = [_exe("gemini"), "-p", "", "--approval-mode", "plan"]
    rc, out, err = _run(cmd, stdin_text=prompt, cwd=workdir, timeout=timeout)
    return rc, _clean(out), err


def _rail_grok(prompt, workdir, timeout, workroot):
    pf = Path(workdir) / ("grok-%d.txt" % int(time.time() * 1000))
    pf.write_text(prompt, encoding="utf-8")
    # --no-memory/--no-subagents cut what can be cut. Grok CLI reads its global
    # instruction files regardless -- there is no flag for it. That is MEASURED
    # contamination and belongs in the report, not hidden behind a nicer number.
    # --no-plan + empty tool set: without them Grok enters agent mode and answers
    # "let me check the format first" instead of the question.
    cmd = [_exe("grok"), "--prompt-file", str(pf), "--no-memory", "--no-subagents",
           "--no-plan", "--tools", ""]
    rc, out, err = _run(cmd, stdin_text=None, cwd=workdir, timeout=timeout)
    pf.unlink(missing_ok=True)
    return rc, _clean(out), err


RAILS = {
    "claude": {
        "label": "Claude Code",
        "run": _rail_claude,
        "isolation": ("env switches (DISABLE_CLAUDE_MDS, DISABLE_AUTO_MEMORY, "
                      "DISABLE_BUNDLED_SKILLS, CLAUDE_MEMORY_STORES='')"),
    },
    "codex": {
        "label": "OpenAI Codex CLI",
        "run": _rail_codex,
        "isolation": "isolated CODEX_HOME (auth.json + config.toml only) + cwd outside $HOME",
    },
    "gemini": {
        "label": "Google Gemini CLI",
        "run": _rail_gemini,
        "isolation": "cwd outside $HOME",
    },
    "grok": {
        "label": "xAI Grok CLI",
        "run": _rail_grok,
        "isolation": ("--no-memory --no-subagents; NO switch for global instruction "
                      "files -- residual load is expected"),
    },
}


# ------------------------------------------------------------------- classification

def classify(body, markers, min_chars=MIN_ANSWER_CHARS, rc=0):
    """(verdict, note, hits) for one rail's answer.

    Only three things count as a measurement: the sentinel, a marker hit, or a real
    enumeration. Everything else -- an empty string, a crash, an auth error, a refusal --
    is UNKNOWN. Every one of those contains no markers, so scoring any of them "clean"
    is the exact false negative this tool exists to stop.
    """
    body = (body or "").strip()
    # Typographic apostrophes and dashes must be folded before any prose matching.
    # Codex answered "I can't reveal..." with U+2019, the refusal regex expected U+0027,
    # and the refusal scored CLEAN. Markers are matched on the ORIGINAL body: a filename
    # never contains a curly quote, and folding it there would only add surprises.
    prose = body.replace("’", "'").replace("‘", "'").replace("–", "-")
    # The sentinel is checked BEFORE the length floor, and this ordering is load-bearing:
    # "EMPTY" is 5 characters, so a length-first check marked the one answer we explicitly
    # asked for as "silent" -- a rail that behaved perfectly read as unmeasured.
    if body.upper().rstrip(".") == EMPTY_SENTINEL:
        return VERDICT_CLEAN, "rail answered the '%s' sentinel" % EMPTY_SENTINEL, []
    if len(body) < min_chars:
        return VERDICT_SILENT, "no usable answer (empty or truncated)", []

    # Markers first: dirt outranks every excuse. A rail that refuses to enumerate AND
    # names an instruction file has still told us what we came to find out.
    hits = sorted({m.group(0) for m in markers.finditer(body)})
    if hits:
        return VERDICT_DIRTY, ", ".join(hits[:8]), hits

    if rc != 0:
        return VERDICT_SILENT, "rail exited rc=%s; output is not an answer" % rc, []
    err = RAIL_ERROR.search(prose)
    if err:
        return VERDICT_SILENT, "rail error in place of an answer: %r" % err.group(0), []
    ref = REFUSAL.search(prose)
    if ref:
        return VERDICT_UNCLEAR, "rail declined to enumerate: %r" % ref.group(0)[:60], []
    return VERDICT_CLEAN, "no personal-layer markers found", []


def exit_code(rows):
    """1 beats 3 beats 0. Dirt is louder than not-measured; not-measured beats clean."""
    if any(r["verdict"] == VERDICT_DIRTY for r in rows):
        return 1
    if any(r["verdict"] in (VERDICT_SILENT, VERDICT_UNCLEAR) for r in rows):
        return 3
    return 0


def load_markers(args):
    pats = list(args.marker or [])
    src = args.markers_file or os.environ.get("PROBE_MARKERS_FILE")
    if src:
        cfg = json.loads(Path(src).read_text(encoding="utf-8"))
        got = cfg.get("probe_markers")
        if isinstance(got, list):
            pats.extend(got)
        elif isinstance(got, str):
            pats.append(got)
    if not pats and os.environ.get("PROBE_MARKERS"):
        pats.append(os.environ["PROBE_MARKERS"])
    if not pats:
        pats.append(DEFAULT_MARKERS)
    return re.compile("|".join("(?:%s)" % p for p in pats), re.I)


def check_workroot(workroot):
    """Warn if the scratch dir sits inside $HOME.

    Agent CLIs look for their instruction files by walking UP from the working
    directory. A workroot under $HOME hands one vendor your personal layer and not the
    others -- which is precisely the bias being measured.
    """
    home = Path(os.path.expanduser("~")).resolve()
    try:
        resolved = workroot.resolve()
    except OSError:
        return None
    if home == resolved or home in resolved.parents:
        return ("workroot %s is inside $HOME; agent CLIs walk upwards and may pick up "
                "your personal instruction files. Pass --workroot outside $HOME."
                % workroot)
    return None


# ------------------------------------------------------------------------- report

ICON = {VERDICT_CLEAN: "OK", VERDICT_DIRTY: "!!", VERDICT_SILENT: "??",
        VERDICT_UNCLEAR: "??"}


def tally(rows):
    """(dirty, not_measured, clean). Silence and refusal are counted together: both mean
    the rail was not measured, and reporting them as anything else overstates coverage."""
    dirty = sum(1 for r in rows if r["verdict"] == VERDICT_DIRTY)
    unmeasured = sum(1 for r in rows
                     if r["verdict"] in (VERDICT_SILENT, VERDICT_UNCLEAR))
    return dirty, unmeasured, len(rows) - dirty - unmeasured


def render_report(rows, markers_src, question, stamp):
    icon = ICON
    dirty, unmeasured, clean = tally(rows)
    out = [
        "# Context contamination probe",
        "",
        "- generated: %s" % stamp,
        "- probe version: %s" % __version__,
        "- markers: `%s`" % markers_src,
        "",
        "## Question sent to every rail",
        "",
        "```text",
        question,
        "```",
        "",
        "## Verdicts",
        "",
        "| rail | verdict | detail | isolation applied |",
        "|---|---|---|---|",
    ]
    for r in rows:
        detail = r["note"].replace("|", "\\|")[:110]
        out.append("| %s | %s %s | %s | %s |"
                   % (r["label"], icon[r["verdict"]], r["verdict"], detail, r["isolation"]))
    out += ["",
            "**%d dirty, %d not measured, %d clean out of %d rails.**"
            % (dirty, unmeasured, clean, len(rows)),
            ""]
    for r in rows:
        out += ["## %s -- %s" % (r["label"], r["verdict"]), "",
                "_%s_" % r["note"], "",
                "```text", r["body"] or "(no output)", "```", ""]
    out += [
        "## How to read this",
        "",
        "- **DIRTY** means a marker appeared in the answer. Read the body before acting: "
        "a model can name a file in order to say it does *not* see it. A hit is a reason "
        "to look, not a proof.",
        "- **silent** and **unclear** are not clean. An empty answer, a crashed rail, an "
        "auth error and a refusal to enumerate all contain no markers by construction. "
        "They mean the rail was not measured, and they carry their own exit code (3) so "
        "they cannot be mistaken for a pass.",
        "- Generic agent scaffolding (tool descriptions, an MCP list, \"you are a coding "
        "assistant\") is not treated as a marker: every vendor has it, it is symmetric, "
        "and it does not carry a personal layer.",
        "",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------- main

def probe(rail_ids, markers, question, workroot, timeout, runner=None):
    """Run the probe. `runner` is injectable so the test grid never calls an LLM."""
    rows = []
    workroot.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ctx-probe-", dir=str(workroot)) as wd:
        for rid in rail_ids:
            spec = RAILS[rid]
            fn = runner or spec["run"]
            try:
                rc, body, err = fn(question, wd, timeout, workroot)
            except Exception as e:  # noqa: BLE001 -- a broken rail must not kill the run
                rows.append({"id": rid, "label": spec["label"], "verdict": VERDICT_SILENT,
                             "note": ("rail failed: %r" % e)[:200], "body": "",
                             "isolation": spec["isolation"], "hits": [], "rc": None})
                continue
            verdict, note, hits = classify(body, markers, rc=rc)
            if verdict == VERDICT_SILENT and err:
                note = "%s: %s" % (note, err.strip()[-160:])
            rows.append({"id": rid, "label": spec["label"], "verdict": verdict,
                         "note": note, "body": body, "isolation": spec["isolation"],
                         "hits": hits, "rc": rc})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="context_probe",
        description="Ask each agent CLI what else is in its context; "
                    "flag personal-layer leakage.")
    ap.add_argument("--rails", default="claude,codex,gemini,grok",
                    help="comma-separated rail ids (default: all four)")
    ap.add_argument("--marker", action="append", metavar="REGEX",
                    help="marker regex; repeatable. Also enables canary strings: "
                         "put a unique token in each candidate file and pass it here.")
    ap.add_argument("--markers-file", metavar="JSON",
                    help='JSON with {"probe_markers": [...]} or a single regex string')
    ap.add_argument("--question-file", metavar="TXT",
                    help="override the detector question")
    ap.add_argument("--out", default="contamination-probe.md", help="report path")
    ap.add_argument("--workroot", default=None,
                    help="scratch dir, MUST be outside $HOME (default: system temp)")
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("PROBE_TIMEOUT", "900")))
    ap.add_argument("--json", action="store_true", help="also print machine-readable JSON")
    ap.add_argument("--list-rails", action="store_true", help="print known rails and exit")
    a = ap.parse_args(argv)

    if a.list_rails:
        for rid, spec in RAILS.items():
            print("%-8s %-20s isolation: %s" % (rid, spec["label"], spec["isolation"]))
        return 0

    rail_ids = [r.strip() for r in a.rails.split(",") if r.strip()]
    unknown = [r for r in rail_ids if r not in RAILS]
    if unknown:
        print("[probe] unknown rail(s): %s. Known: %s"
              % (", ".join(unknown), ", ".join(RAILS)), file=sys.stderr)
        return 2
    if not rail_ids:
        print("[probe] no rails selected -- nothing was measured", file=sys.stderr)
        return 2

    try:
        markers = load_markers(a)
    except (OSError, ValueError, re.error) as e:
        print("[probe] cannot build markers: %s" % e, file=sys.stderr)
        return 2

    question = DEFAULT_QUESTION
    if a.question_file:
        try:
            question = Path(a.question_file).read_text(encoding="utf-8").strip()
        except OSError as e:
            print("[probe] cannot read question file: %s" % e, file=sys.stderr)
            return 2

    workroot = (Path(a.workroot) if a.workroot
                else Path(tempfile.gettempdir()) / "ctx-probe-root")
    warn = check_workroot(workroot)
    if warn:
        print("[probe] WARNING: %s" % warn, file=sys.stderr)

    rows = probe(rail_ids, markers, question, workroot, a.timeout)

    for r in rows:
        print("[%s] %-8s %-20s %-7s %s"
              % (ICON[r["verdict"]], r["id"], r["label"], r["verdict"], r["note"][:90]))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = render_report(rows, markers.pattern, question, stamp)
    dest = Path(a.out)
    if dest.parent != Path(""):
        dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report, encoding="utf-8")

    rc = exit_code(rows)
    dirty, unmeasured, _ = tally(rows)
    print("\ndirty %d/%d, not measured %d/%d -> %s (exit %d)"
          % (dirty, len(rows), unmeasured, len(rows), dest, rc))
    if a.json:
        print(json.dumps({"exit": rc, "generated": stamp,
                          "rows": [{k: v for k, v in r.items() if k != "body"}
                                   for r in rows]},
                         ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
