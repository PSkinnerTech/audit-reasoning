import json
from pathlib import Path
import tempfile
import unittest
from audit_reasoning import parse, analyze, partition, write_report, gap_breakdown, gap_labels, intersect

BASE=1700000000

def line(at, kind, payload):
    from audit_reasoning import stamp
    return json.dumps({'timestamp':stamp(BASE+at),'type':kind,'payload':payload})+'\n'

def item(a,b,kind,tid='t1'):
    return line(b,'event_msg',{'type':'item_completed','thread_id':'s','turn_id':tid,
        'started_at_ms':(BASE+a)*1000,'completed_at_ms':(BASE+b)*1000,
        'item':{'id':kind,'type':kind,'raw_content':['DO_NOT_EXPORT_PRIVATE_BODY'],
                'command':['DO_NOT_EXPORT_COMMAND'], 'arguments':{'secret':'DO_NOT_EXPORT_ARGUMENT'}}})

class TimingTests(unittest.TestCase):
    def fixture(self,root):
        from audit_reasoning import stamp
        p=root/'trace.jsonl'
        p.write_text(''.join([
            line(10,'session_meta',{'id':'s','timestamp':stamp(BASE+10)}),
            line(1,'event_msg',{'type':'task_started','turn_id':'inherited','started_at':BASE+1}),
            line(5,'event_msg',{'type':'task_complete','turn_id':'inherited','started_at':BASE+1,'completed_at':BASE+5,'duration_ms':4000}),
            line(20,'event_msg',{'type':'task_started','turn_id':'t1','started_at':BASE+20}),
            item(22,26,'Reasoning'),item(24,28,'CommandExecution'),
            line(29,'response_item',{'type':'function_call','call_id':'c','name':'x','arguments':'DO_NOT_EXPORT_ARGUMENT'}),
            line(30,'response_item',{'type':'function_call_output','call_id':'c','output':'DO_NOT_EXPORT_OUTPUT'}),
            line(30,'event_msg',{'type':'task_complete','turn_id':'t1','started_at':BASE+20,'completed_at':BASE+30,'duration_ms':10000}),
            line(40,'event_msg',{'type':'task_started','turn_id':'audit','started_at':BASE+40})]))
        return {'trace':str(p),'id':'s','created':BASE+10}

    def test_completed_scope_overlap_and_privacy(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=self.fixture(root);parsed=parse(source,BASE+45)
            r=analyze(parsed,source,'last-completed',None,BASE+45)
            self.assertFalse(r['provisional']);self.assertEqual(r['activeSeconds'],10)
            self.assertEqual([t['id'] for t in r['turns']],['t1'])
            counts={x['category']:x['seconds'] for x in r['breakdown']}
            self.assertEqual(counts['Reasoning'],2)
            self.assertEqual(counts['Command execution'],2)
            self.assertEqual(counts['Concurrent activity'],2)
            self.assertEqual(counts['Tool overhead / other tool wait'],1)
            self.assertEqual(counts['Unclassified active time'],3)
            self.assertEqual(sum(x['percent'] for x in r['breakdown']),100)
            self.assertEqual(r['inclusiveCategorySeconds']['Reasoning'],4)
            for key in ('gapGroups', 'gapTransitions', 'gapIntervals'):
                self.assertEqual(sum(x['seconds'] for x in r[key]),3)
            self.assertEqual(r['supportingEventCounts']['response_item/function_call'],1)
            self.assertEqual(r['supportingEventCounts']['response_item/function_call_output'],1)
            write_report(r,root/'output')
            for path in (root/'output').iterdir():self.assertNotIn('DO_NOT_EXPORT',path.read_text())
            with self.assertRaises(ValueError):write_report(r,root/'output')

    def test_session_excludes_inheritance_and_idle(self):
        with tempfile.TemporaryDirectory() as folder:
            source=self.fixture(Path(folder));parsed=parse(source,BASE+45)
            r=analyze(parsed,source,'session',None,BASE+45)
            self.assertTrue(r['provisional']);self.assertEqual(r['activeSeconds'],15)
            self.assertEqual(r['idleBetweenTurnsSeconds'],10)
            r=analyze(parsed,source,'current',None,BASE+45)
            self.assertEqual(r['activeSeconds'],5);self.assertTrue(r['provisional'])

    def test_same_category_overlap_is_not_double_counted(self):
        r=partition([[0,10]],[[1,8,'Reasoning'],[3,9,'Reasoning']],[])
        self.assertEqual(r['Reasoning'],8);self.assertEqual(r['Concurrent activity'],0)

    def test_live_tool_is_not_unclassified_and_gaps_have_context(self):
        with tempfile.TemporaryDirectory() as folder:
            source=self.fixture(Path(folder));parsed=parse(source,BASE+29.5)
            r=analyze(parsed,source,'current',None,BASE+29.5)
            counts={x['category']:x['seconds'] for x in r['breakdown']}
            self.assertEqual(counts['In-flight tool call'],.5)
            self.assertEqual(counts['Unclassified active time'],3)
            self.assertAlmostEqual(sum(x['seconds'] for x in r['gapContext']),3)
            self.assertIn('Before first recorded activity',[x['context'] for x in r['gapContext']])

    def test_precise_turn_start_keeps_final_output_inside_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            source=self.fixture(Path(folder));p=Path(source['trace'])
            events=[json.loads(s) for s in p.read_text().splitlines()]
            from audit_reasoning import stamp
            for e in events:
                v=e['payload']
                if v.get('turn_id')=='t1' and v.get('type')=='task_started':e['timestamp']=stamp(BASE+20.125)
                if v.get('turn_id')=='t1' and v.get('type')=='task_complete':
                    e['timestamp']=stamp(BASE+30.125);v['time_to_first_token_ms']=1875
            events.insert(-2,json.loads(item(29.9,30.1,'AgentMessage')))
            p.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
            r=analyze(parse(source,BASE+45),source,'last-completed',None,BASE+45)
            self.assertEqual(r['activeSeconds'],10)
            self.assertEqual(r['turns'][0]['start'],BASE+20.125)
            self.assertEqual(r['turns'][0]['end'],BASE+30.125)
            self.assertAlmostEqual(r['inclusiveCategorySeconds']['Agent messages'],.2,places=5)
            self.assertEqual(r['turns'][0]['timeToFirstTokenMs'],1875)

    def test_trace_corruption_and_partial_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            source=self.fixture(Path(folder));p=Path(source['trace'])
            with p.open('a') as f:f.write('{"partial":')
            self.assertEqual(len(parse(source,BASE+45)['warnings']),1)
            with p.open('a') as f:f.write('\n')
            with self.assertRaises(ValueError):parse(source,BASE+45)

    def test_identity_and_missing_turn_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            source=self.fixture(Path(folder));parsed=parse(source,BASE+45)
            with self.assertRaises(ValueError):analyze(parsed,source,'current','unknown',BASE+45)
            source['id']='wrong'
            with self.assertRaises(ValueError):parse(source,BASE+45)

    def test_frozen_byte_boundary_replays_before_append(self):
        with tempfile.TemporaryDirectory() as folder:
            source=self.fixture(Path(folder));p=Path(source['trace'])
            source['bytesReadBoundary']=p.stat().st_size
            before=parse(source,BASE+50)
            with p.open('a') as f:
                f.write(line(46,'response_item',{'type':'function_call','call_id':'later'}))
            after=parse(source,BASE+50)
            self.assertEqual(before,after)
            source['bytesReadBoundary']=p.stat().st_size+1
            with self.assertRaises(ValueError):parse(source,BASE+50)

    def test_fork_and_idle_gaps_do_not_supply_event_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            source=self.fixture(Path(folder));parsed=parse(source,BASE+45)
            result=analyze(parsed,source,'session',None,BASE+45)
            current=[r for r in result['gapIntervals'] if r['provisional']]
            self.assertEqual(len(current),1)
            self.assertEqual(current[0]['seconds'],5)
            self.assertEqual(current[0]['transition'],'Active-window start → Capture cutoff')
            self.assertEqual(sum(r['seconds'] for r in result['gapGroups']),8)

    def test_custom_call_results_and_unmatched_notifications(self):
        with tempfile.TemporaryDirectory() as folder:
            from audit_reasoning import stamp
            p=Path(folder)/'trace.jsonl'
            p.write_text(''.join([
                line(0,'session_meta',{'id':'s','timestamp':stamp(BASE)}),
                line(0,'event_msg',{'type':'task_started','turn_id':'t1','started_at':BASE}),
                item(1,2,'AgentMessage'),
                line(4,'response_item',{'type':'custom_tool_call','call_id':'c','input':'DO_NOT_EXPORT_INPUT'}),
                line(6,'response_item',{'type':'custom_tool_call_output','call_id':'c','output':'DO_NOT_EXPORT_OUTPUT'}),
                line(7,'response_item',{'type':'custom_tool_call_output','call_id':'notification','output':'DO_NOT_EXPORT_NOTIFICATION'}),
                item(9,10,'Reasoning'),
                line(11,'event_msg',{'type':'task_complete','turn_id':'t1','started_at':BASE,'duration_ms':11000})]))
            source={'trace':str(p),'id':'s','created':BASE}
            parsed=parse(source,BASE+12);r=analyze(parsed,source,'session',None,BASE+12)
            self.assertEqual(parsed['outer'],[[BASE+4,BASE+6]])
            transitions={row['transition']:row['seconds'] for row in r['gapTransitions']}
            self.assertEqual(transitions['Agent message → Tool call'],2)
            self.assertEqual(transitions['Tool result → Recorded reasoning'],3)
            self.assertEqual(r['supportingEventCounts']['response_item/custom_tool_call_output'],2)
            write_report(r,Path(folder)/'report')
            for path in (Path(folder)/'report').iterdir():self.assertNotIn('DO_NOT_EXPORT',path.read_text())


class GapTests(unittest.TestCase):
    def breakdown(self, active, items=(), outer=(), pending=(), cutoff=100, completed_ends=()):
        segments=[]
        totals=partition(active,items,intersect(outer,active),intersect(pending,active),segments)
        before=dict(totals)
        result=gap_breakdown(segments,active,items,outer,pending,cutoff,completed_ends)
        self.assertEqual(totals,before)
        for key in ('gapGroups','gapTransitions','gapIntervals'):
            self.assertAlmostEqual(sum(x['seconds'] for x in result[key]),totals['Unclassified active time'])
        for key in ('gapGroups','gapTransitions'):
            self.assertAlmostEqual(sum(x['percentOfUnclassified'] for x in result[key]),
                                   100 if totals['Unclassified active time'] else 0)
        return result

    def test_both_boundaries_and_compaction(self):
        result=self.breakdown([[0,30]],[[1,3,'Reasoning'],[9,11,'Reasoning'],
                              [12,14,'Agent messages'],[20,22,'Context compaction']],
                              [[5,7],[16,18]],cutoff=40)
        transitions={r['transition']:r['seconds'] for r in result['gapTransitions']}
        self.assertEqual(transitions,{
            'Active-window start → Recorded reasoning':1,
            'Recorded reasoning → Tool call':2,
            'Tool result → Recorded reasoning':2,
            'Recorded reasoning → Agent message':1,
            'Agent message → Tool call':2,
            'Tool result → Context compaction':2,
            'Context compaction → Turn end':8})
        groups={r['group']:r['seconds'] for r in result['gapGroups']}
        self.assertEqual(groups['Model output → tool call'],4)
        self.assertEqual(groups['Tool result → recorded reasoning'],2)

    def test_pending_and_cutoff_do_not_predict_future_call(self):
        result=self.breakdown([[0,10]],[[0,2,'Reasoning']],cutoff=10)
        self.assertEqual(result['gapTransitions'][0]['transition'],'Recorded reasoning → Capture cutoff')
        self.assertTrue(result['gapIntervals'][0]['provisional'])
        result=self.breakdown([[0,10]],[[0,2,'Reasoning']],pending=[[6,10]],cutoff=10)
        self.assertEqual(result['gapGroups'][0]['seconds'],4)
        self.assertEqual(result['gapTransitions'][0]['transition'],'Recorded reasoning → Tool call')
        self.assertFalse(result['gapIntervals'][0]['provisional'])

    def test_completed_at_cutoff_is_turn_end(self):
        result=self.breakdown([[0,10]],[[0,2,'Reasoning']],cutoff=10,completed_ends={10})
        self.assertEqual(result['gapTransitions'][0]['transition'],'Recorded reasoning → Turn end')
        self.assertFalse(result['gapIntervals'][0]['provisional'])

    def test_zero_time_call_retains_both_sides(self):
        result=self.breakdown([[0,10]],[[0,2,'Reasoning']],outer=[[5,5]])
        transitions={r['transition']:r['seconds'] for r in result['gapTransitions']}
        self.assertEqual(transitions,{'Recorded reasoning → Tool call':3,'Tool result → Turn end':5})

    def test_simultaneous_boundaries_and_concurrent_tools(self):
        result=self.breakdown([[0,12]],[[3,6,'Command execution'],[4,6,'MCP tool calls'],
                                      [9,10,'Reasoning']],outer=[[2,6],[3,6]])
        row=next(r for r in result['gapTransitions'] if 'Tool result →' in r['transition'])
        self.assertEqual(row['transition'],'Command execution + MCP tool calls + Tool result → Recorded reasoning')
        self.assertEqual(row['seconds'],3)
        self.assertEqual(next(r for r in result['gapGroups'] if r['group']=='Tool result → recorded reasoning')['seconds'],3)

    def test_zero_duration_item_is_an_anchor(self):
        result=self.breakdown([[0,10]],[[4,4,'Context compaction']],outer=[[7,8]])
        transitions={r['transition']:r['seconds'] for r in result['gapTransitions']}
        self.assertEqual(transitions['Active-window start → Context compaction'],4)
        self.assertEqual(transitions['Context compaction → Tool call'],3)

    def test_clipped_window_and_touching_turns_reset_context(self):
        result=self.breakdown([[5,10]],[[1,5,'Reasoning'],[8,9,'Agent messages']])
        self.assertEqual(result['gapIntervals'][0]['transition'],'Active-window start → Agent message')
        result=self.breakdown([[0,5],[5,10]],[[1,2,'Reasoning'],[8,9,'Agent messages']])
        transitions={r['transition']:r['seconds'] for r in result['gapTransitions']}
        self.assertEqual(transitions['Recorded reasoning → Turn end'],3)
        self.assertEqual(transitions['Active-window start → Agent message'],3)

    def test_chart_bins_preserve_original_event_pair(self):
        active=[[0,3600]];items=[[0,1700,'Agent messages']];outer=[[1900,2000]]
        segments=[];partition(active,items,outer,segments=segments)
        label=gap_labels(active,items,outer,[],3600)
        a,b,kind=next(r for r in segments if r[0]==1700)
        original=label(a,b)
        # Consumers label the complete gap, then apportion its duration at bin edges.
        bins=[(max(a,lo),min(b,hi),original) for lo,hi in [[0,1800],[1800,3600]]]
        self.assertEqual([hi-lo for lo,hi,_ in bins],[100,100])
        self.assertTrue(all(pair==('Model output → tool call','Agent message → Tool call') for _,_,pair in bins))

    def test_verified_zero_unclassified_has_no_gap_rows(self):
        result=self.breakdown([[0,10]],[[0,10,'Reasoning']])
        self.assertEqual(result,{'gapGroups':[],'gapTransitions':[],'gapIntervals':[]})

if __name__=='__main__':unittest.main()
