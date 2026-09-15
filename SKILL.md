---
name: audit-reasoning
description: Audit a local Codex session or response turn using trace timing events, reporting activity percentages and locating unclassified gaps between model output, tool calls, tool results and turn boundaries. Use for reasoning or execution-time breakdowns.
---

# Audit reasoning

Produce a reproducible timing breakdown from local Codex metadata and JSONL traces. Use the bundled parser instead of recreating timing logic. This is a descriptive audit, not an overthinking score or an automatic model-switching workflow.

## Select the scope

- **No scope specified / last prompt to completion:** `--scope last-completed`. Audits the most recent completed turn, excluding the current audit invocation. State which turn and window were selected.
- **Entire session:** `--scope session`. Includes owned turns through the capture cutoff, excludes inherited pre-creation history and idle gaps, and labels any unfinished turn provisional.
- **This response / since the latest prompt:** `--scope current`. Uses the latest turn through capture time. Its future completion cannot be included in its own audit.
- **Specific response:** `--turn-id ID`; use `--list-turns` to resolve an ambiguous turn. A steered follow-up inside one turn has no separate reliable completion boundary; explain that limitation rather than presenting turn timing as exact per-message timing.

Use `CODEX_THREAD_ID` for the calling session, or an explicitly identified `--thread ID`. Never select the most recently modified trace as a proxy for the current session. Do not expand a session audit to other agents unless requested. A fork may have no owned completed turn; in that case explain the scope issue and offer or use explicitly requested current/session scope, without silently auditing its parent.

## Execute

Resolve this skill's directory and run its script with Python 3 (standard library only):

```bash
python3 <skill-directory>/scripts/audit_reasoning.py --scope last-completed
python3 <skill-directory>/scripts/audit_reasoning.py --scope session
python3 <skill-directory>/scripts/audit_reasoning.py --thread THREAD_ID --turn-id TURN_ID
```

`--output DIRECTORY` chooses a new artifact directory. `--trace PATH` reads a supplied local trace without database discovery. `--cutoff ISO_TIMESTAMP` fixes the capture time; `--byte-boundary N` reuses a previously frozen source length. For a requested multi-agent audit, freeze a shared cutoff and every source's byte boundary before parsing any trace. The script refuses to overwrite an existing report. It opens the Codex database read-only and never edits sessions, model settings, engineering files, or remote systems.

Read the returned report and coverage warnings. Report active duration, selected scope, category percentages and material gaps; link the Markdown report and chart. Keep recommendations proportional to the evidence. A high reasoning percentage alone establishes neither wasted work nor reduced accuracy at a lower setting.

## Interpret the scale

The primary table is an **exclusive 0–100% distribution of active turn wall time**, not a score:

- Reasoning
- Context compaction
- Agent messages
- Command execution
- MCP tool calls
- File changes
- Other timed activity
- Concurrent activity
- Tool overhead / other tool wait
- In-flight tool call
- Unclassified active time

Different categories that overlap go into Concurrent activity; same-category overlaps count once. Separate inclusive event durations retain background process activity, but must not be summed. Do not combine exclusive and inclusive figures.

Command execution includes any shell process; a test filename does not make a read command testing. File-change duration measures applying edits, not generating code. MCP operation counts identify tools and servers but do not prove unnecessary repetition. Reasoning intervals exclude uninstrumented latency and are not evidence of private thought quality.

**Unclassified active time** is elapsed time inside selected owned active turns that is not covered by a timed item, paired tool envelope, or known in-flight call. Explain it using the report's **gap groups and detailed event pairs**: model output → tool call, tool result → recorded reasoning, tool result → next tool call, before first activity, and tails ending at turn completion or capture. “Model output” groups recorded reasoning and agent messages. Other transitions remain visible in the detailed table and timestamped gap export. Recompute the shares for each capture; never reuse a percentage from an earlier audit.

These labels establish **where a gap occurred, not what the agent was doing**. Each gap view subdivides the same unclassified total. Do not add it to the parent or relabel it as inference, queueing, reading, code generation or tool latency. A tool invocation timestamp means the client logged a call; it does not establish when the receiver accepted it. Use [the measurement reference](references/measurement.md) when asked whether model generation or tool delivery caused a gap.

Show in-flight tool time separately for live captures. Zero completed reasoning items and zero reported reasoning tokens mean no separately recorded reasoning, not no model computation. Missing telemetry is not verified zero activity. A current-response snapshot audits the audit invocation itself; it does not measure earlier skill construction or another response. Keep accepted outcomes, defects, rework and completion latency separate from activity shares; unmatched tasks cannot establish model or reasoning-setting speedups.

For trace fields, coverage, and schema maintenance, read [the measurement reference](references/measurement.md). Do not export reasoning content, prompts, command bodies, credentials, or MCP arguments to reports. The helper exports only selected metadata and timing.

## Invocation

The installed skill is named `audit-reasoning`, with the explicit skill invocation `$audit-reasoning`. It may appear in the client's skill/command picker after skills refresh. A literal `/audit-reasoning` slash alias is client-dependent; do not claim an app command was registered merely because the skill folder exists.
