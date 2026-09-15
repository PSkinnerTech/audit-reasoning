import json
from pathlib import Path
import tempfile
import unittest
from audit_reasoning import parse, analyze, partition, write_report

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

if __name__=='__main__':unittest.main()
