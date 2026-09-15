# Measurement contract

## Inputs

The local Codex `state_5.sqlite` database resolves a specific thread ID to `rollout_path` and its creation timestamp. It is a private schema: validate needed columns and stop if unavailable, or use an explicit trace. The script reads no authentication files and does not connect to providers.

Freeze the JSONL file's byte count and an observation timestamp before parsing. A partial final line is a disclosed coverage warning; malformed interior lines stop the audit. Session metadata and turn starts exclude inherited pre-creation history. Each report includes source path, byte boundary, selected turn IDs, scope and cutoff for reproduction.

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

## Gap context (schema version 2)

Subdivide the unclassified intervals by the nearest preceding observed boundary in the active window: before first recorded activity, after a tool result, after model output, after compaction, or between other recorded activities. These subdivisions sum to the unclassified bucket and must not be added to the primary table. They describe location, not cause. A gap after a tool result could contain input processing, queueing, inference, tool-argument generation, or application overhead. Do not label it measured reasoning.

Use completed-turn evidence to interpret an early capture, preserving the original capture. A zero reasoning-item count plus zero reported reasoning tokens can be a legitimate telemetry result for a short response. Reports should explain that rather than promising delayed reasoning events will necessarily appear. In-flight operations only have an observed start and a provisional cutoff; final duration is unavailable until completion.

## Limits

Recorded Reasoning intervals measure instrumented reasoning emission, not every server-side inference phase. Public message emission excludes generating tool-call arguments. File-change application can be instantaneous even when writing the patch took minutes. Tests, review reasoning, code generation and reading results are different activities; native tags do not separate all of them. No native tag says Overthinking or Unnecessary review.

A high reasoning share can be appropriate for a difficult task. Assess usefulness against accepted artifacts, defects found, rework and completion behavior. Do not infer causation or promise speedups from a timing distribution alone.
