import copy
import csv
import tempfile
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import interventions as m

def fixture():
    sessions=[]; assessments=[]
    for i in range(3):
        s={k:'v1' for k in m.FIELDS['sessions']}
        s.update(event_id=str(i),date=f'2026-11-0{i+1}',attendance=20,planned_minutes=30,actual_minutes=30,interruption_count=0,minutes_lost=0,incident_category='',essential_inaccessible='no',factual_error='no',missing_core_explanation='no',access_requirement='no')
        sessions.append(s)
        assessments.append(dict(event_id=str(i),item_id='call',key_version='v1',timepoint='after',offered=20,answered=20,correct=16,omitted=0,matched=''))
    return dict(sessions=sessions,assessments=assessments,actions=[])

class Rules(unittest.TestCase):
    def test_missing(self):
        self.assertTrue(all(v['status']=='awaiting measurement' for v in m.evaluate({k:[] for k in m.FIELDS}).values()))
    def test_eighty_boundary(self):
        self.assertNotEqual(m.evaluate(fixture())['adaptation']['status'],'review triggered')
    def test_two_below(self):
        x=fixture()
        for r in x['assessments'][:2]:r['correct']=15
        self.assertEqual(m.evaluate(x)['adaptation']['status'],'review triggered')
    def test_inadequate(self):
        x=fixture()
        for r in x['assessments']:r.update(answered=9,correct=0,omitted=11)
        a=m.evaluate(x)['adaptation'];self.assertEqual(a['status'],'insufficient evidence');self.assertNotIn('0/9',str(a))
    def test_versions(self):
        x=fixture()
        for i,r in enumerate(x['assessments']):r.update(correct=0,key_version=str(i))
        self.assertEqual(m.evaluate(x)['adaptation']['status'],'insufficient evidence')
    def test_latest_window_not_cherrypicked(self):
        x=fixture()
        for r in x['assessments']:r['correct']=0
        x['assessments'][-1].update(answered=9,omitted=11)
        self.assertEqual(m.evaluate(x)['adaptation']['status'],'insufficient evidence')
    def test_missing_latest_assessment(self):
        x=fixture()
        for r in x['assessments']:r['correct']=0
        fourth=copy.deepcopy(x['sessions'][-1]);fourth.update(event_id='3',date='2026-11-04');x['sessions'].append(fourth)
        self.assertEqual(m.evaluate(x)['adaptation']['status'],'insufficient evidence')
    def test_template_roundtrip(self):
        logs,config=m.read_logs(Path(__file__).resolve().parents[1]/'measurement_templates')
        self.assertEqual(config['version'],'pilot-v1')
        self.assertEqual(len(logs['sessions']),0)
    def test_populated_csv_import(self):
        with tempfile.TemporaryDirectory() as folder:
            for name,rows in fixture().items():
                with (Path(folder)/(name+'.csv')).open('w',newline='') as f:
                    writer=csv.DictWriter(f,fieldnames=m.FIELDS[name]);writer.writeheader();writer.writerows(rows)
            logs,config=m.read_logs(folder)
            self.assertEqual(logs['assessments'][0]['correct'],16)
            self.assertNotEqual(m.evaluate(logs,config)['adaptation']['status'],'review triggered')
            (Path(folder)/'thresholds.json').write_text(json.dumps({'version':'v2','correct_threshold':.81}))
            logs,config=m.read_logs(folder)
            self.assertEqual(m.evaluate(logs,config)['adaptation']['status'],'review triggered')
            (Path(folder)/'assessments.csv').write_text('bad_header\n')
            with self.assertRaises(ValueError):m.read_logs(folder)
    def test_missingness_policy(self):
        x=fixture()
        for r in x['assessments']:r.update(answered=10,correct=10,omitted=10)
        result=m.evaluate(x,dict(m.DEFAULT,maximum_omission_fraction=.1))
        self.assertIn('Missingness exceeds',str(result))
        self.assertNotEqual(result['adaptation']['status'],'review triggered')
    def test_ten_percent_boundary(self):
        x=fixture();x['sessions'][0].update(minutes_lost=3,incident_category='AV',interruption_count=1)
        self.assertEqual(m.evaluate(x)['structural']['status'],'review triggered')
        x['sessions'][0]['minutes_lost']=2.99
        self.assertNotEqual(m.evaluate(x)['structural']['status'],'review triggered')
    def test_recurrence(self):
        x=fixture()
        for s in x['sessions'][:2]:s.update(incident_category='AV',interruption_count=1)
        self.assertEqual(m.evaluate(x)['structural']['status'],'review triggered')
        x['sessions'][1]['configuration']='different'
        self.assertNotEqual(m.evaluate(x)['structural']['status'],'review triggered')
    def test_access_and_fact(self):
        x=fixture();x['sessions'][0]['essential_inaccessible']='yes';x['sessions'][1]['access_requirement']='yes'
        self.assertEqual(m.evaluate(x)['delivery']['status'],'review triggered')
        self.assertEqual(m.evaluate(x)['structural']['status'],'review triggered')
    def test_invalid(self):
        for kind in ('duplicate','unknown','count','minutes','nan'):
            x=fixture()
            if kind=='duplicate': x['sessions'].append(copy.deepcopy(x['sessions'][0]))
            if kind=='unknown':x['assessments'][0]['event_id']='missing'
            if kind=='count':x['assessments'][0]['correct']=21
            if kind=='minutes':x['sessions'][0]['minutes_lost']=31
            if kind=='nan':x['sessions'][0]['actual_minutes']='nan'
            with self.assertRaises(ValueError):m.validate(x)
    def test_human_review_does_not_clear(self):
        x=fixture();x['sessions'][0]['factual_error']='yes'
        x['actions']=[dict(action_id='a',bucket='delivery',event_ids='0',evidence_reference='log',threshold_version='pilot-v1',trigger_date='2026-11-01',owner_role='lead',action_taken='corrected',review_date='2026-11-02',decision='reviewed')]
        m.validate(x)
        self.assertEqual(m.evaluate(x)['delivery']['status'],'review triggered')

if __name__=='__main__':unittest.main()
