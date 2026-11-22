import copy,json,unittest
from pathlib import Path
from score_spans.service import process

class ScoreTests(unittest.TestCase):
    def setUp(self):self.data=json.loads(Path('fixtures/input.json').read_text())
    def test_tie_and_transpose(self):
        r=process(self.data);self.assertEqual(r['diagnostics'],[])
        self.assertEqual(r['spans'],[{'events':['n1','n2'],'voice':'v1','pitches':[62],'start':'0','end':'6'}])
    def test_invalid_tie_not_merged(self):
        self.data['events'][1]['pitches']=[61];r=process(self.data)
        self.assertEqual(len(r['spans']),2);self.assertIn('invalid_tie',[d['kind'] for d in r['diagnostics']])
    def test_gap_overlap_overrun(self):
        self.data['events'][0].pop('tie_to');self.data['events'][0].update(start=[1,1],duration=[4,1])
        self.data['events'][2]['start']=[1,1]
        self.assertEqual({d['kind'] for d in process(self.data)['diagnostics']},{'gap','overrun','overlap'})
    def test_exact_tuplets(self):
        self.data['measures']=self.data['measures'][:1]
        self.data['events']=[{'id':f'n{i}','voice':'v','measure':'m1','start':[i*4,3],'duration':[4,3],'pitches':[60,64]} for i in range(3)]
        r=process(self.data);self.assertEqual(r['diagnostics'],[]);self.assertEqual(r['spans'][-1]['end'],'4')
    def test_unknown_target_and_pitch_overflow(self):
        for mutate in (lambda d:d['events'][0].update(tie_to='absent'),lambda d:d.update(transpose=100),lambda d:d['events'][1].update(tie_to='n2')):
            d=copy.deepcopy(self.data);mutate(d)
            with self.assertRaises(ValueError):process(d)
    def test_rest_tie_and_empty_score(self):
        self.data['events'][0]['pitches']=[]
        self.assertIn('invalid_tie',[d['kind'] for d in process(self.data)['diagnostics']])
        self.data['events']=[];self.assertEqual(process(self.data)['summary']['sounding_spans'],0)
