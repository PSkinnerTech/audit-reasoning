# Audit Reasoning

A Codex skill and standalone Python CLI that audits where session time goes: recorded reasoning, context compaction, messages, commands, MCP calls, file changes, overlapping activity, and gaps in timing coverage.

The audit reads local traces and produces an exclusive breakdown of active response time that sums to 100%. It is a time distribution, **not an overthinking score**.

## Install as a Codex skill

Requires Python 3 and access to local Codex session traces. The parser uses only the Python standard library.

Clone this repository into your Codex skills directory, provided that `audit-reasoning` is not already installed:

```bash
git clone https://github.com/PSkinnerTech/audit-reasoning.git ~/.codex/skills/audit-reasoning
```

This repository is private, so GitHub authentication is required. If the skill is already installed, keep that copy and clone this repository into a separate development directory.

Refresh skills or restart the client, then invoke it explicitly:

```text
$audit-reasoning Audit the last completed response.
$audit-reasoning Audit this entire session.
$audit-reasoning Audit the current response so far.
```

A literal `/audit-reasoning` shortcut depends on the client; installing the folder does not register a slash command.

## Run the CLI

From the repository directory:

```bash
python3 scripts/audit_reasoning.py --scope last-completed
python3 scripts/audit_reasoning.py --scope session
python3 scripts/audit_reasoning.py --scope current
python3 scripts/audit_reasoning.py --thread THREAD_ID --list-turns
python3 scripts/audit_reasoning.py --thread THREAD_ID --turn-id TURN_ID
python3 scripts/audit_reasoning.py --trace /path/to/rollout.jsonl --scope session --output outputs/example
```

Without `--thread` or `--trace`, the script uses `CODEX_THREAD_ID`. It never guesses the current session from the newest trace. Use `--help` for all options, including `--cutoff` and `--codex-dir`.

| Scope | Window |
| --- | --- |
| `last-completed` (default) | Most recent completed response, excluding the current audit invocation |
| `session` | Owned turns through capture, excluding idle gaps and inherited history from before fork creation |
| `current` | Latest response through capture; provisional while that response is running |

## Reports

Each run writes a Markdown report, structured JSON, CSV breakdown, and SVG chart:

```text
report.md
audit.json
breakdown.csv
breakdown.svg
```

Existing report files are never overwritten. Reports include source boundaries, scope, timing coverage, exclusive category percentages, and separate inclusive durations. Overlapping categories are counted once in the exclusive distribution; inclusive durations must not be summed as elapsed time.

Unclassified time is further located by observable boundaries, such as before the first activity or after a tool result. Live snapshots separate in-flight tool calls. Reasoning item counts, reported reasoning tokens, and first-token latency are included when available.

## Interpretation and privacy

- Zero recorded reasoning items or tokens does **not** mean zero model computation. Instrumentation does not expose every inference phase.
- A gap after a tool result does not distinguish queueing, input processing, inference, tool-argument generation, or application overhead.
- File-change duration measures applying an edit, not the time spent generating its code. A large reasoning share alone does not establish wasted work or predict a benefit from lower reasoning settings.
- The parser opens the session database read-only and does not modify sessions or model settings, or contact providers.
- Exports omit reasoning content, prompts, message bodies, command bodies, and MCP arguments. They retain metadata such as local source paths, thread and turn IDs, model settings, and tool names; review reports before sharing them.
- Codex trace and database schemas are internal and may change. Coverage warnings and schema errors are surfaced rather than silently treated as zero work.

See [the measurement contract](references/measurement.md) for fields, accounting rules, and limitations, and [SKILL.md](SKILL.md) for agent instructions.

## Development

Run the synthetic regression suite without reading real session traces:

```bash
python3 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

Tests cover overlap accounting, scope boundaries, export privacy, unfinished tools, timestamp precision, malformed traces, identity checks, and overwrite protection. Keep real session traces, generated reports, and credentials out of commits.
