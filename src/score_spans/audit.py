from fractions import Fraction

def audit_measures(measures,events):
    diagnostics=[]
    for voice in sorted({e['voice'] for e in events}):
        for m in measures:
            cursor=Fraction(0)
            selected=sorted((e for e in events if e['voice']==voice and e['measure']==m['id']),key=lambda e:(e['start'],e['id']))
            for e in selected:
                start=e['start'];end=start+e['duration']
                if start>cursor and cursor<m['length']:
                    diagnostics.append({'kind':'gap','voice':voice,'measure':m['id'],'start':str(cursor),'end':str(min(start,m['length']))})
                if start<cursor:diagnostics.append({'kind':'overlap','event':e['id'],'voice':voice,'measure':m['id']})
                if end>m['length']:diagnostics.append({'kind':'overrun','event':e['id'],'voice':voice,'measure':m['id']})
                cursor=max(cursor,end)
            if cursor<m['length']:diagnostics.append({'kind':'gap','voice':voice,'measure':m['id'],'start':str(cursor),'end':str(m['length'])})
    return diagnostics

def spans(events):
    by_id={e['id']:e for e in events};valid={};diagnostics=[]
    for e in events:
        if e['tie_to'] is None:continue
        target=by_id[e['tie_to']]
        if e['pitches'] and e['voice']==target['voice'] and e['pitches']==target['pitches'] and e['absolute']+e['duration']==target['absolute']:
            valid[e['id']]=target['id']
        else:diagnostics.append({'kind':'invalid_tie','from':e['id'],'to':target['id']})
    incoming=set(valid.values());result=[]
    for e in sorted(events,key=lambda e:(e['absolute'],e['voice'],e['id'])):
        if e['id'] in incoming or not e['pitches']:continue
        chain=[e['id']];last=e
        while last['id'] in valid:
            last=by_id[valid[last['id']]];chain.append(last['id'])
        result.append({'events':chain,'voice':e['voice'],'pitches':e['pitches'],'start':str(e['absolute']),'end':str(last['absolute']+last['duration'])})
    return result,diagnostics
