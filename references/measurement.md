# Measurement contract

## Inputs

The local Codex `state_5.sqlite` database resolves a specific thread ID to `rollout_path` and its creation timestamp. It is a private schema: validate needed columns and stop if unavailable, or use an explicit trace. The script reads no authentication files and does not connect to providers.

Freeze the JSONL file's byte count and an observation timestamp before parsing. For multiple sources, use one cutoff and freeze every byte count before parsing any trace. The CLI accepts `--byte-boundary N`; library callers may set `source['bytesReadBoundary']`. A boundary beyond the current file length fails instead of silently substituting a newer capture. A partial final line is a disclosed coverage warning; malformed interior lines stop the audit. Session metadata and turn starts exclude inherited pre-creation history. Each report includes source path, byte boundary, selected turn IDs, scope and cutoff for reproduction.

## Trace records

`event_msg` / `item_completed` supplies `thread_id`, `turn_id`, `started_at_ms`, `completed_at_ms`, and `item.type`. Recognized types are `Reasoning`, `ContextCompaction`, `AgentMessage`, `CommandExecution`, `McpToolCall`, and `FileChange`. Only identities, numeric timing and MCP server/tool names are retained. Reasoning/message/command/argument bodies are not exported.

`task_started` and `task_complete` supply turn boundaries. Use the millisecond task-start event timestamp when it agrees within two seconds with the rounded `started_at`; add completed duration milliseconds to that precise start. Using rounded seconds can exclude the end of a final message. Retain `time_to_first_token_ms` when available: this measures combined first-token latency, not a breakdown of queueing, prompt processing and inference. Interrupted boundaries are retained when present. Missing timing is not evidence of zero work; disclose warnings and avoid broad claims from partial traces. Current-turn results are provisional. The audit cannot observe its own later final response.

`response_item` tool-call/output pairs provide outer foreground tool intervals. Nested item intervals take precedence where available; uncovered outer time becomes Tool overhead / other tool wait. Unsolicited notifications without a matching invocation are not durations. Long-running processes can overlap model activity after their initial call yields.

`turn_context` supplies model and effort metadata. Unique-response `token_usage_record` usage supplies output and reasoning tokens; these are not latency measurements, quality scores, or a reason to enforce token budgets. Missing usage records are reported as a zero record count, not verified zero token consumption.

## Denominator and overlap

Denominator = union of selected owned active turn intervals. Session calendar span and idle gaps are reported separately. Intersect all item and tool intervals with that active set. For each segment:

1. Two or more distinct timed-item categories: Concurrent activity.
2. Exactly one timed-item category: that category.
3. No timed item, but a known tool invocation still in flight in a running turn at capture: In-flight tool call, bounded by the cutoff.
4. No timed item, but a paired outer tool call: Tool overhead / other tool wait.
5. Otherwise: Unclassified active time.

Primary percentages sum to 100% before rounding. Inclusive category durations are a second view: union each category's intervals within active time, allowing background overlaps across categories. Never sum inclusive durations as elapsed time. When comparing multiple agents, accumulated agent-hours overlap in calendar time.

This policy intentionally differs from the earlier one-off factory audit, which gave foreground calls priority and reported background execution separately. Recompute both samples with the same parser/policy before comparing percentages.

## Unclassified time and gap locations (schema version 3)

Unclassified active time is the remainder of owned active turn time after applying the exclusive accounting rules above. The active turn is observed; the activity occupying the remainder is not instrumented. Idle gaps and inherited history are outside this denominator.

Locate each gap using both surrounding boundaries. `gap_labels()` indexes completed item ends, tool results, item starts and logged invocations. `gap_breakdown()` emits the following groups:

| Group | Observable boundaries |
| --- | --- |
| Model output → tool call | Recorded Reasoning or AgentMessage ends; a tool invocation is next |
| Tool result → recorded reasoning | A paired result is followed by a Reasoning item start |
| Tool result → next tool call | A paired result is followed by another invocation |
| Before first recorded activity | No preceding activity endpoint within this active window |
| Last activity → turn end | A preceding activity ends with no later activity before a recorded turn end |
| Last activity → capture cutoff | A preceding activity ends with no later activity before an unfinished capture boundary |
| Other inter-event gaps | All remaining event pairs, shown individually in the detailed view |

Detailed transitions retain the actual types, such as `Recorded reasoning → Tool call`, `Agent message → Tool call`, or `Context compaction → Agent message`. An active window may begin at a reporting-window or ownership boundary inside a turn, so its label is **Active-window start**, not an inferred new turn. A window with no activity endpoints stays in Before first recorded activity, with its actual ending boundary in the transition. Never invent a future invocation after a capture cutoff.

Use original tool intervals for boundary labels: merging loses nested call boundaries, and clipping can invent invocation/result timestamps. Zero-duration items and paired calls remain boundary anchors despite contributing no time. Tied endpoint types are all retained in detailed labels; a tied tool result takes precedence for grouping. Positive activity ending exactly at the active-window start belongs outside that window. Split gaps at owned-turn boundaries, so adjacent turns do not borrow each other's events. An observed completed/interrupted end equal to the cutoff takes precedence over Capture cutoff.

For charts, **label the complete gap before splitting it across bin boundaries**. Apportion its seconds into each bin while preserving the original pair; a bin edge is not an event. Across parallel agents, agent-minutes can exceed the bin's calendar duration.

Schema 3 keeps the exclusive categories, overlap policy and legacy `gapContext` unchanged. It adds:

- `gapGroups` and `gapTransitions`: seconds, percent of unclassified time, and percent of active time. Each view sums to the existing unclassified total, with shares of unclassified time summing to 100% before rounding when that total is positive. A verified zero total has empty gap rows.
- `gapIntervals`: start, end, seconds, group, transition and a provisional flag for an unresolved cutoff tail. Individual timestamps support drill-down and bin aggregation; no event bodies are exported. The report-level provisional flag still identifies any included unfinished turn.
- `supportingEventCounts`: recognized task/item and tool-call/result record counts in the selected scope. Counts do not add elapsed time. Unmatched results are counted when observed in scope, but do not manufacture tool durations or result anchors.

The existing `gapContext` rows use only the preceding boundary: before first activity, after tool result, after model output, after compaction, or between other activities. They remain available for compatibility. All gap views are alternative subdivisions of the same unclassified total; never sum the views or stack them alongside their parent. Percentages come from the current capture, not a fixed historical split.

These are locations, not proven causes. A gap after a tool result could contain input processing, queueing, inference, tool-argument generation or application overhead. Do not label it measured reasoning.

Use completed-turn evidence to interpret an early capture, preserving the original capture. A zero reasoning-item count plus zero reported reasoning tokens can be a legitimate telemetry result for a short response. Reports should explain that rather than promising delayed reasoning events will necessarily appear. In-flight operations only have an observed start and a provisional cutoff; final duration is unavailable until completion.

## Model output to invocation: what can be attributed?

A `response_item` invocation timestamp is when the client logs the call, **not a confirmed tool receiver acknowledgement**. The preceding gap can include generating the tool name/arguments and client dispatch. It is not evidence that a fully generated call spent the entire gap waiting for a tool. Once the invocation is logged, observed wait generally falls inside the outer tool envelope, its nested timed execution, or provisional in-flight time. The trace alone does not precisely separate model generation, client overhead, transport and receiver delay.

To distinguish those causes, instrument correlated request/call IDs with:

1. Model request submission, provider receipt/queue exit where available, first and last streamed events, and tool-call argument completion.
2. Client call-ready, dispatch start/end, and approval/scheduler/retry wait spans where applicable.
3. Tool receiver acknowledgement, execution start/end, result sent, and client result received.

Use monotonic durations within each process and disclose clock uncertainty across hosts. Record observed model/effort and numeric token counters with these spans; do not log prompts, reasoning content, arguments, command bodies or credentials. Missing provider phases must remain unknown. First-token latency alone cannot identify their individual contributions.

## Limits

Recorded Reasoning intervals measure instrumented reasoning emission, not every server-side inference phase. Public message emission excludes generating tool-call arguments. File-change application can be instantaneous even when writing the patch took minutes. Tests, review reasoning, code generation and reading results are different activities; native tags do not separate all of them. No native tag says Overthinking or Unnecessary review.

A high reasoning share can be appropriate for a difficult task. Assess usefulness against accepted artifacts, defects found, rework and completion behavior. Do not infer causation or promise speedups from a timing distribution alone.
