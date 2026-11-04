"""Optional aggregate pilot logs. No respondent records or inferred venue incidents."""
import csv
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

FIELDS = {
 'sessions': 'event_id,date,audience,venue_code,configuration,facilitator_code,form_version,curriculum_version,faq_version,scenario_version,pilot_condition,attendance,planned_minutes,actual_minutes,interruption_count,minutes_lost,incident_category,affected_content,resolution,owner,essential_inaccessible,factual_error,missing_core_explanation,access_requirement'.split(','),
 'assessments': 'event_id,item_id,key_version,timepoint,offered,answered,correct,omitted,matched'.split(','),
 'actions': 'action_id,bucket,event_ids,evidence_reference,threshold_version,trigger_date,owner_role,action_taken,review_date,decision'.split(',')}
DEFAULT = {'version':'pilot-v1','correct_threshold':0.8,'minimum_answered':10,'window':3,'repeats':2,'lost_fraction':0.1,'maximum_omission_fraction':None}
BUCKETS = ('delivery','adaptation','structural')

def read_logs(folder):
    folder = Path(folder)
    config = dict(DEFAULT)
    cp = folder/'thresholds.json'
    if cp.exists():
        supplied=json.loads(cp.read_text())
        if set(supplied)-set(DEFAULT): raise ValueError('Unknown threshold setting')
        config.update(supplied)
    for key in ('correct_threshold','lost_fraction','maximum_omission_fraction'):
        v=config[key]
        if key=='maximum_omission_fraction' and v is None: continue
        if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or not 0<=v<=1: raise ValueError('Invalid threshold '+key)
    for key in ('minimum_answered','window','repeats'):
        if type(config[key]) is not int or config[key]<1: raise ValueError('Invalid threshold '+key)
    if config['repeats']>config['window'] or not isinstance(config['version'],str) or not config['version']: raise ValueError('Invalid threshold policy')
    logs={}
    for name,fields in FIELDS.items():
        path=folder/(name+'.csv')
        if not path.exists(): logs[name]=[]; continue
        with path.open(newline='',encoding='utf-8-sig') as f:
            reader=csv.DictReader(f)
            if reader.fieldnames!=fields: raise ValueError(f'{path}: use the template columns in order')
            logs[name]=list(reader)
            for row in logs[name]:
                if None in row or any(v is None for v in row.values()): raise ValueError(f'{path}: malformed row')
                for k in row: row[k]=row[k].strip()
    return validate(logs),config

def validate(logs):
    sessions={}
    def number(row,key,integer=False):
        try: v=float(row[key])
        except (ValueError,TypeError): raise ValueError('Invalid number: '+key)
        if not math.isfinite(v) or v<0 or (integer and v!=int(v)): raise ValueError('Invalid number: '+key)
        row[key]=int(v) if integer else v
    for r in logs['sessions']:
        if any(r.get(k) in (None, "") for k in FIELDS['sessions'] if k not in ('incident_category','affected_content','resolution','owner')): raise ValueError('Missing session field')
        if r['event_id'] in sessions: raise ValueError('Duplicate event ID')
        date.fromisoformat(r['date'])
        for k in ('attendance','interruption_count'): number(r,k,True)
        for k in ('planned_minutes','actual_minutes','minutes_lost'): number(r,k)
        if r['planned_minutes']<=0 or r['minutes_lost']>r['actual_minutes']: raise ValueError('Invalid teaching minutes')
        for k in ('essential_inaccessible','factual_error','missing_core_explanation','access_requirement'):
            if r[k] not in ('yes','no'): raise ValueError('Use yes/no for '+k)
        if (r['interruption_count'] or r['minutes_lost']) and not r['incident_category']: raise ValueError('Incident category required')
        sessions[r['event_id']]=r
    seen=set()
    for r in logs['assessments']:
        key=tuple(r[k] for k in ('event_id','item_id','key_version','timepoint'))
        if not all(key) or key in seen or r['event_id'] not in sessions: raise ValueError('Invalid/duplicate assessment or unknown event')
        seen.add(key)
        if r['timepoint'] not in ('before','after'): raise ValueError('Invalid timepoint')
        for k in ('offered','answered','correct','omitted'): number(r,k,True)
        if r['matched']!='':
            number(r,'matched',True)
            if r['matched']>r['answered']: raise ValueError('Invalid matched count')
        if r['correct']>r['answered'] or r['answered']+r['omitted']!=r['offered'] or r['offered']>sessions[r['event_id']]['attendance']: raise ValueError('Invalid assessment counts')
    seen=set()
    for r in logs['actions']:
        if not r['action_id'] or r['action_id'] in seen or r['bucket'] not in BUCKETS: raise ValueError('Invalid action ID or bucket')
        seen.add(r['action_id'])
        if any(x not in sessions for x in r['event_ids'].split('|')): raise ValueError('Unknown action event')
        if not r['owner_role'] or not r['evidence_reference'] or not r['threshold_version']: raise ValueError('Missing action provenance')
        date.fromisoformat(r['trigger_date'])
        if r['review_date']: date.fromisoformat(r['review_date'])
        if r['decision'] not in ('','in progress','reviewed'): raise ValueError('Invalid human decision')
        if r['decision']=='reviewed' and (not r['review_date'] or not r['action_taken']): raise ValueError('Review requires date and action')
    return logs

def evaluate(logs,config=DEFAULT,min_n=10):
    result={k:{'status':'awaiting measurement','details':[]} for k in BUCKETS}
    ss=logs['sessions']; byid={s['event_id']:s for s in ss}
    if ss:
        result['structural']['status']='insufficient evidence'
        for k in ('delivery',): result[k]['status']='proposed action'
    for s in ss:
        if s['essential_inaccessible']=='yes' or s['factual_error']=='yes' or s['missing_core_explanation']=='yes':
            result['delivery']['details'].append(f"{s['event_id']}: correct factual errors or missing core explanations, or restore access immediately.")
        if s['access_requirement']=='yes': result['structural']['details'].append(f"{s['event_id']}: meet documented access requirement before delivery.")
        if s['minutes_lost']/s['planned_minutes']>=config['lost_fraction']:
            result['structural']['details'].append(f"{s['event_id']}: lost {s['minutes_lost']/s['planned_minutes']:.0%} of planned time; coordinator debrief before next booking.")
    venues=defaultdict(list)
    for s in ss: venues[(s['venue_code'],s['configuration'])].append(s)
    for key,rows in venues.items():
        recent=sorted(rows,key=lambda x:(x['date'],x['event_id']))[-config['window']:]
        if len(recent)<config['window']: continue
        categories=set(c for r in recent for c in r['incident_category'].split('|') if c)
        for c in sorted(categories):
            if sum(c in r['incident_category'].split('|') for r in recent)>=config['repeats']:
                result['structural']['details'].append(f'{key[0]} / {key[1]}: recurring {c}; document equipment, venue or scheduling change.')
    groups=defaultdict(list)
    for a in logs['assessments']:
        if a['timepoint']!='after': continue
        s=byid[a['event_id']]
        key=tuple(s[k] for k in ('audience','curriculum_version','faq_version','scenario_version','pilot_condition'))+(a['item_id'],a['key_version'])
        groups[key].append(a)
    if groups: result['adaptation']['status']='insufficient evidence'
    for key,rows in groups.items():
        comparable = [s for s in ss if tuple(s[k] for k in ('audience','curriculum_version','faq_version','scenario_version','pilot_condition')) == key[:5]]
        latest = sorted(comparable, key=lambda s:(s['date'],s['event_id']))[-config['window']:]
        recent_ids = {s['event_id'] for s in latest}
        recent = [a for a in rows if a['event_id'] in recent_ids]
        eligible=len(latest)==config['window'] and len(recent)==config['window'] and all(a['answered']>=max(min_n,config['minimum_answered']) for a in recent)
        if eligible and sum(a['correct']/a['answered']<config['correct_threshold'] for a in recent)>=config['repeats']:
            result['adaptation']['details'].append(f"Item {key[-2]} / key {key[-1]}, scenario {key[3]}: repeated low comprehension; review explanation and student scenario.")
        elif eligible:
            result['adaptation']['details'].append(f"Item {key[-2]}: no comprehension trigger in latest comparable sessions; no success claim.")
        for a in recent:
            tolerance=config['maximum_omission_fraction']
            if tolerance is not None and a['offered'] and a['omitted']/a['offered']>tolerance:
                result['adaptation']['details'].append('Missingness exceeds the agreed tolerance; retention remains inconclusive.')
            if a['answered']>=min_n:
                result['adaptation']['details'].append(f"{a['event_id']} / {a['item_id']}: {a['correct']}/{a['answered']} correct; {a['omitted']}/{a['offered']} omitted.")
            else: result['adaptation']['details'].append('Assessment suppressed: fewer than minimum answered checks.')
    for k in ('delivery','structural'):
        if result[k]['details']: result[k]['status']='review triggered'
    if any('repeated low comprehension' in d for d in result['adaptation']['details']): result['adaptation']['status']='review triggered'
    # Human review is shown alongside calculated flags; never clears them automatically.
    return result
