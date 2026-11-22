from .model import parse
from .audit import audit_measures,spans

def process(data):
    measures,events=parse(data);sounding,ties=spans(events);diagnostics=audit_measures(measures,events)+ties
    return {'spans':sounding,'diagnostics':diagnostics,'measures':[{'id':m['id'],'start':str(m['offset']),'length':str(m['length'])} for m in measures],'summary':{'events':len(events),'sounding_spans':len(sounding),'diagnostics':len(diagnostics)}}
