#!/usr/bin/env python3
"""Read-only Codex JSONL timing audit; exports metadata, never message/argument bodies."""
import argparse
import bisect
import collections
import csv
from datetime import datetime, timezone
from html import escape
import itertools
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

TYPES = {
    'Reasoning': 'Reasoning', 'ContextCompaction': 'Context compaction',
    'AgentMessage': 'Agent messages', 'CommandExecution': 'Command execution',
    'McpToolCall': 'MCP tool calls', 'FileChange': 'File changes',
}
CATEGORIES = list(TYPES.values()) + ['Other timed activity', 'Concurrent activity',
                                       'Tool overhead / other tool wait', 'In-flight tool call',
                                       'Unclassified active time']
COLORS = ['#7764c6', '#d9993c', '#4c86bd', '#319581', '#456bb0', '#87a747',
          '#8f7e69', '#c97196', '#8994a1', '#579fac', '#d0d4d8']
GAP_GROUPS = ('Model output → tool call', 'Tool result → recorded reasoning',
              'Tool result → next tool call', 'Before first recorded activity',
              'Last activity → turn end', 'Last activity → capture cutoff',
              'Other inter-event gaps')


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def number(x):
    return isinstance(x, (float, int)) and not isinstance(x, bool)


def merge(spans):
    result = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result


def intersect(first, second):
    first, second = merge(first), merge(second)
    result = []; i = j = 0
    while i < len(first) and j < len(second):
        a = max(first[i][0], second[j][0]); b = min(first[i][1], second[j][1])
        if b > a:
            result.append([a, b])
        if first[i][1] < second[j][1]: i += 1
        else: j += 1
    return result


def seconds(spans):
    return sum(b-a for a, b in merge(spans))


def partition(active, items, outer, pending=(), segments=None):
    """Exclusive wall-time buckets; cross-category overlaps get their own bucket."""
    events = []
    for a, b in active:
        events.extend([(a, 'active', 1), (b, 'active', -1)])
    for a, b, category in items:
        events.extend([(a, 'item:' + category, 1), (b, 'item:' + category, -1)])
    for a, b in outer:
        events.extend([(a, 'outer', 1), (b, 'outer', -1)])
    for a, b in pending:
        events.extend([(a, 'pending', 1), (b, 'pending', -1)])
    counts = collections.Counter(); totals = collections.Counter(); previous = None
    for at, batch in itertools.groupby(sorted(events), key=lambda e: e[0]):
        if previous is not None and counts['active']:
            kinds = [key[5:] for key, n in counts.items() if key.startswith('item:') and n > 0]
            if len(kinds) > 1: category = 'Concurrent activity'
            elif kinds: category = kinds[0]
            elif counts['pending']: category = 'In-flight tool call'
            elif counts['outer']: category = 'Tool overhead / other tool wait'
            else: category = 'Unclassified active time'
            totals[category] += at-previous
            if segments is not None and at > previous:
                segments.append([previous, at, category])
        for _, key, delta in batch:
            counts[key] += delta
        previous = at
    return {key: totals.get(key, 0.0) for key in CATEGORIES}


def gap_context(segments, active, items, outer):
    """Locate uninstrumented gaps by adjacent events, without inferring internal work."""
    totals = collections.Counter()
    for a, b, category in segments:
        if category != 'Unclassified active time': continue
        boundary = next(lo for lo, hi in active if lo <= a < hi)
        anchors = [(end, 'tool') for start, end in outer if boundary <= end <= a]
        anchors += [(end, kind) for start, end, kind in items if boundary <= end <= a]
        if not anchors:
            label = 'Before first recorded activity'
        else:
            _, previous = max(anchors, key=lambda x: x[0])
            if previous == 'tool': label = 'After a tool result, before next recorded activity'
            elif previous in ('Reasoning', 'Agent messages'):
                label = 'After model output, before next recorded activity'
            elif previous == 'Context compaction':
                label = 'After compaction, before next recorded activity'
            else: label = 'Between other recorded activities'
        totals[label] += b-a
    return dict(totals)


def gap_labels(active, items, outer, pending, cutoff, completed_ends=()):
    """Index actual event boundaries once; label gaps before any chart-bin splitting.

    Pass original outer/pending intervals, not their merged/clipped unions: clipping
    can invent call/result timestamps and merging loses nested call boundaries.
    Zero-duration events are anchors, although they contribute no elapsed time.
    """
    ends = collections.defaultdict(set)
    instant = collections.defaultdict(set)
    begins = collections.defaultdict(set)
    for a, b in outer:
        begins[a].add('Tool call'); ends[b].add('Tool result')
        if a == b: instant[b].add('Tool result')
    for a, b in pending:
        begins[a].add('Tool call')  # The cutoff is not a result.
    for a, b, kind in items:
        begins[a].add(kind); ends[b].add(kind)
        if a == b: instant[b].add(kind)
    anchor_times = sorted(ends)

    def display(kinds):
        names = {'Reasoning': 'Recorded reasoning', 'Agent messages': 'Agent message'}
        return ' + '.join(sorted(names.get(k, k) for k in kinds))

    def label(a, b):
        lo, hi = next((lo, hi) for lo, hi in active if lo <= a < hi)
        idx = bisect.bisect_right(anchor_times, a) - 1
        prior = ends[anchor_times[idx]] if idx >= 0 and anchor_times[idx] >= lo else set()
        if idx >= 0 and anchor_times[idx] == lo:
            prior = instant[lo]  # Positive activity ending at the window start is outside it.
        # An adjacent owned turn must not supply the next event for this turn's tail.
        following = begins.get(b, set()) if b < hi else set()
        terminal = ('Turn end' if b == hi and (b != cutoff or b in completed_ends)
                    else 'Capture cutoff' if b == cutoff else 'Other recorded boundary')
        if prior and prior <= {'Reasoning', 'Agent messages'} and 'Tool call' in following:
            group = GAP_GROUPS[0]
        elif 'Tool result' in prior and following == {'Reasoning'}:
            group = GAP_GROUPS[1]
        elif 'Tool result' in prior and 'Tool call' in following:
            group = GAP_GROUPS[2]
        elif not prior:
            group = GAP_GROUPS[3]
        elif not following and terminal == 'Turn end':
            group = GAP_GROUPS[4]
        elif not following and terminal == 'Capture cutoff':
            group = GAP_GROUPS[5]
        else:
            group = GAP_GROUPS[6]
        previous_label = display(prior) if prior else 'Active-window start'
        next_label = display(following) if following else terminal
        return group, previous_label + ' → ' + next_label

    return label


def gap_breakdown(segments, active, items, outer, pending, cutoff, completed_ends=()):
    """Return additive subdivisions of unclassified time, never new activity time."""
    locate = gap_labels(active, items, outer, pending, cutoff, completed_ends)
    groups = collections.Counter(); transitions = collections.Counter(); intervals = []
    boundaries = sorted({v for pair in list(active) + list(outer) + list(pending) for v in pair})
    for a, b, kind in segments:
        if kind != 'Unclassified active time': continue
        # Zero-time tool events may be absent from the merged accounting envelope.
        cuts = [a, *boundaries[bisect.bisect_right(boundaries, a):bisect.bisect_left(boundaries, b)], b]
        for lo, hi in zip(cuts, cuts[1:]):
            group, transition = locate(lo, hi)
            groups[group] += hi-lo; transitions[transition] += hi-lo
            intervals.append({'start': stamp(lo), 'end': stamp(hi), 'seconds': hi-lo,
                              'group': group, 'transition': transition,
                              'provisional': hi == cutoff and hi not in completed_ends})
    denom = seconds(active)
    total = sum(groups.values())

    def rows(values, key):
        return [{key: k, 'seconds': v, 'percentOfUnclassified': 100*v/total if total else 0,
                 'percentOfActiveTime': 100*v/denom if denom else 0}
                for k, v in values.items()]

    assert abs(sum(transitions.values()) - total) < .01, 'Gap accounting failed'
    return {'gapGroups': rows({k: groups[k] for k in GAP_GROUPS if k in groups}, 'group'),
            'gapTransitions': rows(dict(sorted(transitions.items())), 'transition'),
            'gapIntervals': intervals}


def resolve(thread_id, trace, codex_dir):
    if trace:
        return {'trace': str(Path(trace).expanduser().resolve()), 'id': thread_id,
                'created': None, 'source': 'explicit trace'}
    if not thread_id:
        raise ValueError('No current thread ID. Supply --thread or --trace; do not guess the newest session.')
    db = codex_dir / 'state_5.sqlite'
    if not db.exists():
        raise ValueError('Codex metadata database unavailable; supply --trace.')
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        fields = {r['name'] for r in conn.execute('PRAGMA table_info(threads)')}
        required = {'id', 'rollout_path', 'created_at'}
        if not required <= fields:
            raise ValueError('Unsupported Codex metadata schema; supply --trace.')
        names = ['id', 'rollout_path', 'created_at'] + [k for k in ['created_at_ms', 'model', 'reasoning_effort', 'name'] if k in fields]
        row = conn.execute('SELECT ' + ','.join(names) + ' FROM threads WHERE id=?', (thread_id,)).fetchone()
    if not row:
        raise ValueError('Specified thread not found.')
    row = dict(row)
    return {'trace': row['rollout_path'], 'id': row['id'],
            'created': row.get('created_at_ms') / 1000 if row.get('created_at_ms') else row['created_at'],
            'model': row.get('model'), 'reasoningEffort': row.get('reasoning_effort'),
            'name': row.get('name'), 'source': 'read-only local metadata'}


def parse(source, cutoff):
    path = Path(source['trace']); available = path.stat().st_size
    size = source.get('bytesReadBoundary', available)
    if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= available:
        raise ValueError('Frozen byte boundary is invalid or the trace has been truncated.')
    remaining = size; turns = {}; items = []; outer = []; calls = {}; seen = set()
    models = []; usage = []; seen_usage = set(); warnings = []; tool_ops = []
    created = source['created']; session_meta = None; last_event = None; record_count = 0
    current_turn = None; evidence = []
    with path.open('rb') as f:
        while remaining > 0:
            raw = f.readline(remaining); remaining -= len(raw); record_count += 1
            if not raw: break
            try: event = json.loads(raw)
            except ValueError:
                if remaining == 0 and not raw.endswith(b'\n'):
                    warnings.append('Ignored one incomplete trailing record at the frozen byte boundary.'); break
                raise ValueError(f'Malformed trace record at line {record_count}; audit not produced.')
            try: at = epoch(event['timestamp'])
            except (KeyError, ValueError, TypeError):
                warnings.append(f'Ignored untimestamped record at line {record_count}.'); continue
            payload = event.get('payload', {}); kind = event.get('type')
            if kind == 'session_meta':
                session_meta = {'id': payload.get('id'), 'timestamp': payload.get('timestamp')}
                if created is None and payload.get('timestamp'):
                    created = epoch(payload['timestamp'])
                if source['id'] and payload.get('id') != source['id']:
                    raise ValueError('Trace session identity does not match the requested thread.')
                if not source['id']: source['id'] = payload.get('id')
            if created is None:
                raise ValueError('Trace has no creation boundary. Resolve it through --thread or supply a complete trace.')
            if at < created - 2 or at > cutoff: continue
            last_event = max(last_event or at, at)
            ptype = payload.get('type')
            if kind == 'turn_context':
                pair = {'model': payload.get('model'), 'reasoningEffort': payload.get('effort', payload.get('reasoning_effort'))}
                if not models or pair != {k: models[-1][k] for k in pair}:
                    models.append({'at': at, **pair})
            elif kind == 'token_usage_record' and payload.get('response_id') not in seen_usage:
                seen_usage.add(payload.get('response_id'))
                v = payload.get('usage', {})
                usage.append({'at': at, 'turnId': payload.get('turn_id'),
                              'outputTokens': v.get('output_tokens', 0),
                              'reasoningTokens': v.get('reasoning_output_tokens', 0)})
            elif kind == 'response_item':
                cid = payload.get('call_id')
                evidence_turn = current_turn
                if cid and ptype in ('function_call', 'custom_tool_call'):
                    calls[cid] = (at, current_turn)
                elif cid and ptype in ('function_call_output', 'custom_tool_call_output') and cid in calls:
                    a, evidence_turn = calls.pop(cid)
                    if at >= a: outer.append([a, at])
                if cid and ptype in ('function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output'):
                    evidence.append({'at': at, 'turnId': evidence_turn, 'type': 'response_item/' + ptype})
            if kind != 'event_msg': continue
            tid = payload.get('turn_id')
            if ptype == 'task_started':
                start = payload.get('started_at', at)
                if number(start) and start >= created - 2:
                    # Integer started_at is rounded; the task-start record has millisecond precision.
                    precise = at if abs(at-start) < 2 else start
                    current_turn = tid
                    turns[tid] = {'id': tid, 'start': max(created, precise), 'rawStart': precise,
                                  'end': None, 'status': 'running'}
            elif ptype in ('task_complete', 'task_interrupted', 'turn_aborted'):
                start = payload.get('started_at')
                if tid not in turns and number(start) and start >= created - 2:
                    turns[tid] = {'id': tid, 'start': max(created, start), 'rawStart': start, 'end': None}
                if tid in turns:
                    t = turns[tid]; duration = payload.get('duration_ms')
                    end = t['rawStart'] + duration/1000 if number(duration) else payload.get('completed_at', at)
                    t.update(end=min(cutoff, end), status='completed' if ptype == 'task_complete' else 'interrupted',
                             timeToFirstTokenMs=payload.get('time_to_first_token_ms'))
            elif ptype == 'item_completed':
                item = payload.get('item', {}); item_type = item.get('type')
                if payload.get('thread_id') and payload['thread_id'] != source['id']: continue
                identity = (tid, item.get('id'), item_type)
                if identity in seen: continue
                seen.add(identity)
                a, b = payload.get('started_at_ms'), payload.get('completed_at_ms')
                if not number(a) or not number(b) or b < a:
                    warnings.append(f'Untimed or invalid {item_type} item at line {record_count}.'); continue
                a, b = max(created, a/1000), min(cutoff, b/1000)
                if b < a: continue
                # Zero-time notifications and user messages are not execution categories.
                if item_type in ('UserMessage', 'FunctionCallOutput'): continue
                category = TYPES.get(item_type, 'Other timed activity')
                items.append({'start': a, 'end': b, 'category': category, 'turnId': tid, 'line': record_count})
                if item_type == 'McpToolCall':
                    tool_ops.append({'start': a, 'end': b, 'turnId': tid,
                                     'server': item.get('server', ''), 'tool': item.get('tool', '')})
            if ptype in ('task_started', 'task_complete', 'task_interrupted', 'turn_aborted', 'item_completed'):
                evidence.append({'at': at, 'turnId': tid, 'type': 'event_msg/' + ptype})
    if not turns:
        raise ValueError('No owned turn boundaries found. This trace format is unsupported or incomplete.')
    pending = [[a, cutoff] for a, tid in calls.values() if turns.get(tid, {}).get('status') == 'running']
    return {'sourceBytes': size, 'created': created, 'lastEvent': last_event, 'turns': list(turns.values()),
            'items': items, 'outer': outer, 'models': models, 'usage': usage, 'tools': tool_ops,
            'warnings': warnings, 'pendingCalls': len(calls), 'pendingIntervals': pending,
            'evidence': evidence, 'recordsRead': record_count}


def analyze(parsed, source, scope, turn_id, cutoff):
    turns = sorted(parsed['turns'], key=lambda t: t['start'])
    if turn_id:
        chosen = [t for t in turns if t['id'] == turn_id]
        if not chosen: raise ValueError('Requested turn ID has no owned boundary in this trace.')
    elif scope == 'last-completed':
        chosen = [t for t in turns if t['status'] == 'completed' and t['end'] is not None][-1:]
        if not chosen: raise ValueError('No completed turn exists in this session. Use --scope current for a provisional audit.')
    elif scope == 'current': chosen = turns[-1:]
    else: chosen = turns
    active = merge([[t['start'], t['end'] if t['end'] is not None else cutoff] for t in chosen])
    if not active: raise ValueError('Selected scope has no positive measured duration.')
    selected = {t['id'] for t in chosen}
    items = [[x['start'], x['end'], x['category']] for x in parsed['items'] if x['turnId'] in selected]
    outer = intersect(parsed['outer'], active)
    pending = intersect(parsed.get('pendingIntervals', []), active)
    segments = []
    totals = partition(active, items, outer, pending, segments); denom = seconds(active)
    gaps = gap_context(segments, active, items, outer)
    turn_boundaries = {v for t in chosen for v in (t['start'], t['end'] if t['end'] is not None else cutoff)}
    gap_windows = []
    for a, b in active:
        cuts = sorted({a, b, *(v for v in turn_boundaries if a < v < b)})
        gap_windows.extend([lo, hi] for lo, hi in zip(cuts, cuts[1:]))
    # Keep raw event boundaries for labels; merged unions are for elapsed accounting only.
    detail = gap_breakdown(segments, gap_windows, items, parsed['outer'],
                           parsed.get('pendingIntervals', []), cutoff,
                           {t['end'] for t in chosen if t['end'] is not None})
    assert abs(sum(x['seconds'] for x in detail['gapGroups']) - totals['Unclassified active time']) < .01, 'Gap accounting failed'
    assert abs(sum(totals.values()) - denom) < .01, 'Accounting failed'
    inclusive = {key: seconds(intersect([[a,b] for a,b,c in items if c == key], active)) for key in CATEGORIES[:7]}
    inventory = collections.Counter(x['category'] for x in parsed['items'] if x['turnId'] in selected)
    tools = collections.Counter(x['server'] + ':' + x['tool'] for x in parsed['tools'] if x['turnId'] in selected)
    tokens = [x for x in parsed['usage'] if x['turnId'] in selected]
    provisional = any(t['end'] is None for t in chosen)
    settings = [x for x in parsed['models'] if active[0][0] <= x['at'] <= active[-1][1]]
    prior = [x for x in parsed['models'] if x['at'] < active[0][0]]
    if prior: settings.insert(0, prior[-1])
    event_counts = collections.Counter(e['type'] for e in parsed.get('evidence', [])
                                      if e['turnId'] in selected and any(a <= e['at'] <= b for a, b in active))
    return {'schemaVersion': 3, 'source': {**source, 'bytesReadBoundary': parsed['sourceBytes']},
            'scope': scope if not turn_id else 'explicit-turn', 'cutoff': stamp(cutoff),
            'start': stamp(active[0][0]), 'end': stamp(active[-1][1]), 'provisional': provisional,
            'denominator': 'Union of selected owned active turn intervals; idle gaps excluded',
            'activeSeconds': denom, 'elapsedSpanSeconds': active[-1][1]-active[0][0],
            'idleBetweenTurnsSeconds': active[-1][1]-active[0][0]-denom,
            'secondsSinceLastTraceEvent': max(0,cutoff-parsed['lastEvent']) if provisional else None,
            'turns': [{k:v for k,v in t.items() if k != 'rawStart'} for t in chosen],
            'breakdown': [{'category':k,'seconds':totals[k], 'percent':100*totals[k]/denom} for k in CATEGORIES],
            'gapContext': [{'context':k,'seconds':v,'percentOfActiveTime':100*v/denom} for k,v in gaps.items()],
            **detail, 'supportingEventCounts': dict(event_counts),
            'gapContextInterpretation': 'Observed boundary locations, not measured internal model phases; these rows subdivide unclassified time.',
            'inFlightToolSeconds':seconds(pending),
            'inclusiveCategorySeconds': inclusive, 'itemCounts': dict(inventory), 'mcpOperationCounts':dict(tools),
            'modelSettingsObserved': settings,
            'outputTokens':sum(x['outputTokens'] for x in tokens), 'reasoningOutputTokens':sum(x['reasoningTokens'] for x in tokens),
            'usageRecordCount':len(tokens), 'warnings':parsed['warnings'],
            'limitations': ['Recorded reasoning intervals do not identify unnecessary thinking or all inference latency.',
                           'No recorded reasoning item means reasoning was not separately instrumented; it is not proof of no model computation. Check usage counts separately.',
                           'Gap context locates missing instrumentation between known events; it does not separate inference, queueing, prompt processing, and output preparation.',
                           'Concurrent activity holds overlaps between different categories. Inclusive category times overlap and must not be summed.',
                           'Command execution measures process intervals, not time spent designing tests or generating code.',
                           'File changes measure applying changes, not authoring effort. Zero-duration events still appear in counts.',
                           'Unclassified active time is owned active time without a timed item, paired tool envelope, or known in-flight call. Gap labels identify its surrounding events, not what caused it.',
                           'Model output groups recorded reasoning and agent messages. A logged tool call is not a confirmed tool receiver acknowledgement; these traces cannot split argument generation, dispatch, transport, and receiver delay.',
                           'A completed turn does not mean a completed engineering task. A live audit cannot include its own future completion.',
                           'Local trace schemas are private and may change; warnings or absent event types reduce coverage.']}


def write_report(result, output):
    output.mkdir(parents=True, exist_ok=True)
    names=['audit.json','breakdown.csv','report.md','breakdown.svg',
           'gap-groups.csv','gap-transitions.csv','gap-intervals.csv']
    if any((output/n).exists() for n in names): raise ValueError('Output already contains an audit. Choose a new output directory.')
    (output/'audit.json').write_text(json.dumps(result,indent=2)+'\n')
    with (output/'breakdown.csv').open('w') as f:
        writer=csv.DictWriter(f,['category','seconds','percent']);writer.writeheader();writer.writerows(result['breakdown'])
    for name, key, label in [('gap-groups.csv', 'gapGroups', 'group'),
                             ('gap-transitions.csv', 'gapTransitions', 'transition')]:
        with (output/name).open('w', newline='') as f:
            writer = csv.DictWriter(f, [label, 'seconds', 'percentOfUnclassified', 'percentOfActiveTime'])
            writer.writeheader(); writer.writerows(result[key])
    with (output/'gap-intervals.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, ['start', 'end', 'seconds', 'group', 'transition', 'provisional'])
        writer.writeheader(); writer.writerows(result['gapIntervals'])
    status='PROVISIONAL — includes unfinished work' if result['provisional'] else 'Completed-turn evidence'
    md=f"# Reasoning and execution audit\n\n**{status}**\n\nSession: `{result['source']['id']}`. Scope: `{result['scope']}`; {len(result['turns'])} turn(s).\n\nWindow: {result['start']} to {result['end']}. Cutoff: {result['cutoff']}.\n\nActive time: **{result['activeSeconds']/60:.2f} minutes**. Idle gaps between selected turns: {result['idleBetweenTurnsSeconds']/60:.2f} minutes, excluded from percentages.\n\n![Time breakdown](breakdown.svg)\n\n| Activity | Seconds | Share of active time |\n|---|---:|---:|\n"
    for row in result['breakdown']:
        md+=f"| {row['category']} | {row['seconds']:.2f} | {row['percent']:.2f}% |\n"
    md+='\nDifferent activity types running simultaneously are assigned to Concurrent activity, so percentages sum to 100% before rounding. Same-type overlaps count once.\n\n## Inclusive event durations\n\nThese include overlapping/background operations; do not sum them or add them to the table above.\n\n'
    for key,value in result['inclusiveCategorySeconds'].items():
        md+=f'- {key}: {value:.2f} seconds; {result["itemCounts"].get(key,0)} recorded items.\n'
    md+='\n## Where the unclassified time occurred\n\nUnclassified active time is elapsed time inside an owned active turn that has no covering timed item, paired tool envelope, or known in-flight call. It excludes idle gaps. The following views each subdivide the same unclassified total; do not add them together or stack them alongside their parent.\n\nModel output groups recorded reasoning and agent messages. These labels locate gaps; they do not measure internal phases or prove a cause.\n\n| Observed gap | Seconds | Share of unclassified time | Share of active time |\n|---|---:|---:|---:|\n'
    for row in result['gapGroups']:
        md+=f"| {row['group']} | {row['seconds']:.2f} | {row['percentOfUnclassified']:.2f}% | {row['percentOfActiveTime']:.2f}% |\n"
    md+='\n<details>\n<summary>Detailed event pairs</summary>\n\n| Preceding → following boundary | Seconds | Share of unclassified time | Share of active time |\n|---|---:|---:|---:|\n'
    for row in result['gapTransitions']:
        md+=f"| {row['transition']} | {row['seconds']:.2f} | {row['percentOfUnclassified']:.2f}% | {row['percentOfActiveTime']:.2f}% |\n"
    md+='\n</details>\n\nA tool-call boundary is when the client logs the invocation, not confirmation that a tool received it. The preceding gap can include generating arguments or client dispatch. Wait after that timestamp generally falls in timed tool activity or the tool envelope. Provider and client instrumentation is needed to separate those causes.\n\nIndividual gap timestamps and provisional cutoff tails are in gap-intervals.csv and audit.json.\n\n<details>\n<summary>Legacy preceding-boundary view</summary>\n\n| Observed gap | Seconds | Share of active time |\n|---|---:|---:|\n'
    for row in result['gapContext']:
        md+=f"| {row['context']} | {row['seconds']:.2f} | {row['percentOfActiveTime']:.2f}% |\n"
    md+='\n</details>\n\n## Supporting event counts\n\nCounts describe observed records, not additional elapsed time. Item-category counts are listed with inclusive durations above.\n\n'
    for key, count in result['supportingEventCounts'].items():
        md+=f'- {key}: {count}\n'
    md+=f'\nReasoning telemetry: **{result["itemCounts"].get("Reasoning",0)} completed reasoning items** and **{result["reasoningOutputTokens"]} reported reasoning tokens** across {result["usageRecordCount"]} usage records. A zero here is a telemetry result, not an absence of model computation.\n'
    first_token=[t['timeToFirstTokenMs'] for t in result['turns'] if number(t.get('timeToFirstTokenMs'))]
    if first_token:
        md+='\nTurn-level time to first token: '+', '.join(f'{x/1000:.3f} s' for x in first_token)+'. This combined latency does not separate prompt processing, queueing or inference.\n'
    md+='\n## Interpretation\n\n'+ '\n'.join('- '+s for s in result['limitations'])+'\n'
    if result['warnings']: md+='\n## Coverage warnings\n\n'+'\n'.join('- '+s for s in result['warnings'])+'\n'
    md+='\nNo reasoning text, prompts, command bodies or MCP arguments are included. See audit.json for source boundaries, turn IDs, model settings, usage counters and operation counts.\n'
    (output/'report.md').write_text(md)
    svg=['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="570" viewBox="0 0 1000 570">','<rect width="100%" height="100%" fill="#fafbf8"/>','<style>text{font-family:Arial,sans-serif;fill:#24332e}</style>', '<text x="24" y="36" font-size="23" font-weight="bold">Reasoning and execution time</text>', f'<text x="24" y="62" font-size="14">{escape(status)} · {result["activeSeconds"]/60:.2f} active minutes · idle excluded</text>']
    for i,(row,color) in enumerate(zip(result['breakdown'],COLORS)):
        y=94+i*36; w=row['percent']*4.9
        svg.extend([f'<text x="310" y="{y+17}" text-anchor="end" font-size="14">{escape(row["category"])}</text>',f'<rect x="326" y="{y}" width="490" height="24" rx="3" fill="#e9ece7"/>',f'<rect x="326" y="{y}" width="{w:.3f}" height="24" rx="3" fill="{color}"/>',f'<text x="828" y="{y+17}" font-size="14">{row["percent"]:.2f}%</text>'])
    svg.extend(['<text x="24" y="521" font-size="13">Time distribution is descriptive. A high reasoning share is not proof of overthinking.</text>','<text x="24" y="545" font-size="13">The report subdivides unclassified time by its location between known events.</text>','</svg>'])
    (output/'breakdown.svg').write_text('\n'.join(svg))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--thread')
    parser.add_argument('--trace',help='Explicit local JSONL; bypasses database discovery')
    parser.add_argument('--codex-dir',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))))
    parser.add_argument('--scope',choices=['session','last-completed','current'],default='last-completed')
    parser.add_argument('--turn-id'); parser.add_argument('--list-turns',action='store_true')
    parser.add_argument('--cutoff',help='Fixed timezone-aware ISO timestamp; defaults to now')
    parser.add_argument('--byte-boundary',type=int,help='Previously frozen source byte length for reproducible replay')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        cutoff=epoch(args.cutoff) if args.cutoff else time.time()
        if cutoff>time.time()+2: raise ValueError('Cutoff cannot be in the future.')
        if args.cutoff and datetime.fromisoformat(args.cutoff.replace('Z','+00:00')).tzinfo is None: raise ValueError('Cutoff requires a timezone.')
        thread=args.thread or (os.environ.get('CODEX_THREAD_ID') if not args.trace else None)
        source=resolve(thread,args.trace,args.codex_dir)
        if args.byte_boundary is not None: source['bytesReadBoundary']=args.byte_boundary
        parsed=parse(source,cutoff)
        if args.list_turns:
            print(json.dumps({'threadId':source['id'],'turns':parsed['turns']},indent=2));return
        result=analyze(parsed,source,args.scope,args.turn_id,cutoff)
        out=args.output or Path.cwd()/'outputs'/('audit-reasoning-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        write_report(result,out)
        print(json.dumps({'report':str((out/'report.md').resolve()),'scope':result['scope'],'provisional':result['provisional'],'activeSeconds':result['activeSeconds'],'breakdown':result['breakdown'],'gapContext':result['gapContext'],'gapGroups':result['gapGroups'],'gapTransitions':result['gapTransitions'],'supportingEventCounts':result['supportingEventCounts'],'reasoningTelemetry':{'completedItems':result['itemCounts'].get('Reasoning',0),'reportedTokens':result['reasoningOutputTokens'],'usageRecords':result['usageRecordCount']},'warnings':result['warnings']},indent=2))
    except (ValueError,OSError,sqlite3.Error) as error:
        parser.exit(2,'Audit unavailable: '+str(error)+'\n')

if __name__=='__main__': main()
