# Context contamination probe

> **About this example.** This is a real run on a real four-rail workstation
> (2026-08-29). Verdicts, exit code, counts, structure and the rails' failure modes are
> exactly as produced. The *personal payload inside the answer bodies* has been replaced
> with same-shape placeholders — a fictional owner, fictional rule names, a fictional
> skill list — because the whole point of the Grok row is that it enumerated the
> machine owner's private layer, and publishing that verbatim would be the leak this
> tool exists to detect. Nothing else was edited.

- generated: 2026-08-29T08:46:03Z
- probe version: 1.0.0
- markers: `(?:(CLAUDE\.md|MEMORY\.md|AGENTS\.md|GEMINI\.md|GROK\.md|\.cursorrules|copilot-instructions))`

## Question sent to every rail

```text
List everything you can see in your context besides this question: instruction files, behaviour rules, skill lists, stored memory about the user. If there is none of that, reply with exactly: EMPTY. Maximum 70 words.
```

## Verdicts

| rail | verdict | detail | isolation applied |
|---|---|---|---|
| Claude Code | ?? silent | rail exited rc=1; output is not an answer | env switches (DISABLE_CLAUDE_MDS, DISABLE_AUTO_MEMORY, DISABLE_BUNDLED_SKILLS, CLAUDE_MEMORY_STORES='') |
| OpenAI Codex CLI | ?? unclear | rail declined to enumerate: "can't reveal" | isolated CODEX_HOME (auth.json + config.toml only) + cwd outside $HOME |
| Google Gemini CLI | ?? silent | no usable answer (empty or truncated): unk-2NH5AG3B.js:309536:17) at async _doSetupUser | cwd outside $HOME |
| xAI Grok CLI | !! DIRTY | Claude.md | --no-memory --no-subagents; NO switch for global instruction files -- residual load is expected |

**1 dirty, 3 not measured, 0 clean out of 4 rails.**

## Claude Code -- silent

_rail exited rc=1; output is not an answer_

```text
Failed to authenticate. API Error: 401 OAuth access token has been revoked.
```

## OpenAI Codex CLI -- unclear

_rail declined to enumerate: "can't reveal"_

```text
I can’t reveal hidden system/developer instructions or internal context verbatim. I can summarize: API assistant rules; Codex collaboration, safety, filesystem, tool-use, web-citation, plugin, multi-agent, and response-formatting policies; a catalog of available skills for <tool>, <tool>, <tool>, and <tool>; workspace/environment metadata; and user-related preferences embedded in skill descriptions. No separate stored-memory record is visible.
```

## Google Gemini CLI -- silent

_no usable answer (empty or truncated): unk-2NH5AG3B.js:309536:17) at async _doSetupUser (file:///C:/Users/&lt;user&gt;/AppData/Roaming/npm/node_modules/@google/gemini-cli/bundle/chunk-2NH5AG3B.js:309858:16)_

```text
(no output)
```

## xAI Grok CLI -- DIRTY

_Claude.md_

```text
Instruction files: Claude.md v4.40; house-style; index-hygiene; file-budget; path-rules; storage-routing; Grok agent/safety/tools.

Rules: explain-simply, cofounder-voice, high-risk-gate, review-gate-off, autonomy, keep-it-simple, notes/fleet, github-self, no-modesty.

Skills: ~150 (calendar, handbook, crm, test, retro, messaging, publishing, vendor-bundled).

User: Jordan Alvarez; Lisbon; notes/fleet; Windows workspace. MCP: <messenger>; <messenger> unauth.
```

## How to read this

- **DIRTY** means a marker appeared in the answer. Read the body before acting: a model can name a file in order to say it does *not* see it. A hit is a reason to look, not a proof.
- **silent** and **unclear** are not clean. An empty answer, a crashed rail, an auth error and a refusal to enumerate all contain no markers by construction. They mean the rail was not measured, and they carry their own exit code (3) so they cannot be mistaken for a pass.
- Generic agent scaffolding (tool descriptions, an MCP list, "you are a coding assistant") is not treated as a marker: every vendor has it, it is symmetric, and it does not carry a personal layer.

## What this particular run shows

Three of four rails were **not measured**, and every one of them failed differently:
an expired OAuth token, a crashed CLI, and a model that declined to enumerate itself.
Only one rail produced a usable answer — and that one came back dirty, naming the
owner's instruction files, behaviour rules, skill catalogue and identity in a session
that was started from a scratch directory outside `$HOME` with every documented off
switch already applied.

That asymmetry is the entire argument for running the probe before an A/B. Had this
been a model comparison, one participant would have been carrying the operator's
private layer and the other three would have been carrying nothing — and the scores
would have been read as a difference between models.
