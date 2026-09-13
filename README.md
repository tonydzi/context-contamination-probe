# Context contamination probe

**Your agent CLI is loading files you did not put in the prompt. Ask it, and diff the
answers across runtimes.**

One stdlib-only Python file. [context_probe.py](context_probe.py) has no dependencies, needs no API key, and puts no LLM in the test grid.

## The symptom

You compare two agent runtimes, or two models behind two runtimes, and one of them
answers with knowledge of your project, your conventions, or you. Nothing in your prompt
said any of that.

Agent CLIs load instruction files implicitly. Each vendor does it differently, and the
differences do not cancel out:

| runtime | what it picks up on its own | off switch |
|---|---|---|
| Claude Code | `CLAUDE.md`, `MEMORY.md`, the bundled skill list | yes, env vars |
| OpenAI Codex CLI | `AGENTS.md`, walking **up** from the working directory, plus `$CODEX_HOME/AGENTS.md` | partly: isolate `CODEX_HOME`, work outside `$HOME` |
| xAI Grok CLI | its global config directory | **no flag for it** |
| Google Gemini CLI | its own scaffolding only | n/a |

On the machine this was built on, `~/AGENTS.md` is **145,418 bytes**. In our own 2026 measurement, every Codex run started from anywhere under the home directory inherited all of it. The other three runtimes inherited none of it, which is the asymmetry [context_probe.py](context_probe.py) measures. Any A/B between them was measuring the layers, not the models — the failure mode that produced [persona-portability-benchmark](https://github.com/tonydzi/persona-portability-benchmark).

## Why bytes on disk are not the answer

The usual instinct is to measure the files: sum the sizes, set a budget, watch the number, and [context_probe.py](context_probe.py) deliberately does none of that. That gives you an **upper bound**, not a fact. It cannot tell you which layers a given runtime actually admitted into the window for a given working directory, after hierarchical lookups, imports, path-scoped rules and plugin instructions have all had their turn, which is exactly what [context_probe.py](context_probe.py) asks.

This asks the running session instead. [context_probe.py](context_probe.py) sends one identical question to each rail:

```text
List everything you can see in your context besides this question: instruction
files, behaviour rules, skill lists, stored memory about the user. If there is
none of that, reply with exactly: EMPTY. Maximum 70 words.
```

Then it greps each answer for the markers of a personal layer listed in [markers.example.json](markers.example.json), and prints a verdict per rail.

## Install and run

```bash
git clone https://github.com/tonydzi/context-contamination-probe
cd context-contamination-probe
python context_probe.py --workroot /var/tmp/ctx-probe
```

`--workroot` **must be outside `$HOME`**. Agent CLIs find their instruction files by walking upwards from the working directory; a scratch dir under your home directory hands one vendor your personal layer and not the others, which is the exact bias [context_probe.py](context_probe.py) is measuring. [context_probe.py](context_probe.py) warns when you do it anyway.

Probe one rail, or a subset:

```bash
python context_probe.py --rails claude,codex --workroot /var/tmp/ctx-probe
python context_probe.py --list-rails
```

## Reading the verdict

```
[!!] grok     xAI Grok CLI         DIRTY   Claude.md
[OK] gemini   Google Gemini CLI    clean   rail answered the 'EMPTY' sentinel
[??] claude   Claude Code          silent  rail error in place of an answer: 'API Error'
[??] codex    OpenAI Codex CLI     unclear rail declined to enumerate: "can't disclose"
```

| verdict | meaning |
|---|---|
| `DIRTY` | a marker appeared in the answer |
| `clean` | the rail enumerated its context and nothing personal was in it |
| `silent` | empty output, a crash, a non-zero exit, or an auth error |
| `unclear` | the rail answered but declined to enumerate |

| exit | meaning |
|---|---|
| `0` | every probed rail answered, no marker matched |
| `1` | at least one rail came back **DIRTY** |
| `2` | usage or configuration error, **nothing was measured** |
| `3` | no dirt, but at least one rail was **silent** or **unclear** |

Three rules [context_probe.py](context_probe.py) enforces so the number stays honest:

1. **Not measured is not clean**, and [context_probe.py](context_probe.py) will not pretend otherwise. An empty answer contains no markers — and so does a
   crash, an auth error, and "I can't disclose my instructions". If any of those scored as clean, the tool would report an unmeasured rail as a safe one: the exact false negative it exists to prevent, and [_test_context_probe.py](_test_context_probe.py) holds the line. They get their own exit code from [context_probe.py](context_probe.py) instead of collapsing into `0`. Both of those classes were found by the tool's own first real run in 2026, which scored an expired OAuth token and a polite refusal as two clean rails.
2. **A hit is a reason to look, not a proof.** A model can name `CLAUDE.md` in order to
   say it does *not* see it. Every report embeds the full answer body underneath the verdict, as in [examples/contamination-probe.example.md](examples/contamination-probe.example.md), because the last step is a human reading it.
3. **Generic scaffolding is not contamination**, and [markers.example.json](markers.example.json) draws that line. Tool descriptions, an MCP server list,
   "you are a coding assistant" — every vendor has these, they are symmetric, and
   flagging them would make every rail permanently dirty and the signal worthless. Only the markers of a *personal* layer in [markers.example.json](markers.example.json) count.

## Markers

The default set in [markers.example.json](markers.example.json) catches well-known instruction filenames:

```
CLAUDE.md  MEMORY.md  AGENTS.md  GEMINI.md  GROK.md  .cursorrules  copilot-instructions
```

That is a starting point, not a config: copy [markers.example.json](markers.example.json) and edit it. The markers that matter are **yours**, and they belong in your copy of [markers.example.json](markers.example.json): your name, your persona's name, your internal project codenames, your config filenames.

```bash
python context_probe.py --marker 'ACME-INTERNAL' --marker 'my-persona'
python context_probe.py --markers-file markers.example.json
```

### Canary mode (same flag, no extra code)

If you want to know *which specific file* reached the window, do not guess from sizes — use the canary mode of [context_probe.py](context_probe.py).
Put a unique token in each candidate file and probe for the tokens:

```bash
echo 'CANARY-GH-7731'    >> .github/AGENTS.md
echo 'CANARY-BUILD-4412' >> build/AGENTS.md
python context_probe.py --rails codex --workroot /var/tmp/ctx-probe \
       --marker 'CANARY-GH-7731' --marker 'CANARY-BUILD-4412'
```

Whichever canaries come back in the report named the layers that were actually loaded from that path, as [examples/contamination-probe.example.md](examples/contamination-probe.example.md) shows.
Remove them afterwards.

## Honest limits

- **Grok has no off switch.** `--no-memory --no-subagents` cut what can be cut; the Grok
  CLI reads its global instruction files regardless. We report that as measured
  contamination rather than hiding it behind a nicer number. If you need a clean Grok
  rail, you have to move its config directory, not pass a flag.
- **A rail can simply refuse.** On our own first run, Codex declined to enumerate its
  instructions at all. A runtime that will not describe itself cannot be cleared by the
  question method — that is what the `unclear` verdict is for. Use canary mode instead:
  it does not depend on the model's willingness to talk about its own context, only on
  whether a unique string comes back.
- **The instrument is a claim too.** This measures what the model *reports* seeing.
  A model that does not enumerate its own context faithfully will produce a clean-looking
  answer from a dirty session. Treat a clean verdict as evidence, not proof — and treat
  a silent rail as no evidence at all.
- **False positives are expected and visible.** Marker matching is a regex over prose.
  It is deliberately dumb, so the body goes in the report and a human makes the call.
- **Four rails are wired.** Adding a fifth is one entry in the `RAILS` dict: a label, an
  invocation, and an honest note about what its isolation does and does not cover.
- **One turn, not a session.** The probe measures the first turn from a scratch
  directory. A long session that later reads files into its own context is a different
  question.

## Test grid

```bash
python _test_context_probe.py
```

54 tests, zero LLM calls, zero network: every rail is replaced by a canned string, so
the grid exercises the part that can silently lie — the classifier, the exit-code
ladder, and the report. The load-bearing case is `test_empty_answer_is_silent_not_clean`.

Four bugs were caught before the first release, and all four were the same class —
*something that is not an answer scoring as a clean rail*:

- The length floor ran **before** the sentinel check, so a rail that answered `EMPTY` —
  the exact answer we ask a clean rail to give — was scored `silent`, because `EMPTY` is
  five characters and the floor is twenty. Caught by the grid.
- `Failed to authenticate. API Error: 401 OAuth access token has been revoked.` is 62
  characters and contains no markers, so it scored **clean**. Caught by the first real
  4-rail run.
- `I can't disclose hidden system instructions...` also contains no markers, so it also
  scored **clean**. Same run.
- The refusal detector then missed the *next* run's refusal, because Codex writes
  `can’t` with a typographic apostrophe (U+2019) and the pattern expected U+0027. Prose
  is now quote-folded before matching; markers still run against the original text,
  since a filename never carries a curly quote.

All three live findings are now regression tests in `TestNotAnAnswer`.

## Where this came from

Extracted from the harness of
[persona-portability-benchmark](https://github.com/tonydzi/persona-portability-benchmark),
where it exists to keep a cross-model comparison honest: same persona, same frozen
memory, seven models, and a probe run before the experiment to prove the rails were
comparable. The probe turned out to be the reusable half, so it now lives on its own.

MIT. Issues and rails for other runtimes welcome.
