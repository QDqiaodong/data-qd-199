from fractions import Fraction
from .checks import fields,array,ident,unique,integer

def rational(value,positive=False):
    n,d=array(value,2,2);integer(n,0,100000);integer(d,1,10000)
    if positive and n==0:raise ValueError('positive duration required')
    return Fraction(n,d)

def parse(data):
    fields(data,('measures','events','transpose'));shift=integer(data['transpose'],-127,127)
    measures=[];offset=Fraction(0)
    for m in array(data['measures'],1,200):
        fields(m,('id','numerator','denominator'));key=ident(m['id']);n=integer(m['numerator'],1,32);d=integer(m['denominator'],1,64)
        if d not in (1,2,4,8,16,32,64):raise ValueError('unsupported meter denominator')
        length=Fraction(n*4,d);measures.append({'id':key,'offset':offset,'length':length});offset+=length
    unique([m['id'] for m in measures]);by_measure={m['id']:m for m in measures};events=[]
    for e in array(data['events'],0,2000):
        fields(e,('id','voice','measure','start','duration','pitches'),('tie_to',))
        key=ident(e['id']);voice=ident(e['voice']);measure=ident(e['measure'])
        if measure not in by_measure:raise ValueError('unknown measure')
        start=rational(e['start']);duration=rational(e['duration'],True)
        pitches=[integer(p,0,127) for p in array(e['pitches'],0,16)];unique(pitches)
        if pitches!=sorted(pitches):raise ValueError('pitches must be sorted')
        shifted=[p+shift for p in pitches]
        if any(p<0 or p>127 for p in shifted):raise ValueError('transposed pitch outside MIDI bounds')
        target=ident(e['tie_to']) if 'tie_to' in e else None
        events.append({'id':key,'voice':voice,'measure':measure,'start':start,'duration':duration,'absolute':by_measure[measure]['offset']+start,'pitches':shifted,'tie_to':target})
    unique([e['id'] for e in events]);by_id={e['id']:e for e in events};incoming=[]
    for e in events:
        if e['tie_to'] is not None:
            if e['tie_to'] not in by_id:raise ValueError('unknown tie target')
            incoming.append(e['tie_to'])
    unique(incoming)
    return measures,events
