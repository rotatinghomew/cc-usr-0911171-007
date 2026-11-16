import decimal
import unittest
from talent_credentials.recognition.engine import RecognitionEngine

class DomainTests(unittest.TestCase):
    def setUp(self): self.engine=RecognitionEngine()

    def application(self,**changes):
        value={'id':'application-1','evidence':[{'type':'diploma','issuer':'university'},{'type':'experience','issuer':'employer'}]};value.update(changes);return value
    def context(self,**changes):
        value={'today':'2026-11-16','rules':[{'type':'diploma','minimum':1},{'type':'experience','minimum':1}]};value.update(changes);return value
    def test_satisfies_rules(self):self.assertTrue(self.engine.evaluate(self.application(),self.context()).accepted)
    def test_missing_evidence(self):self.assertIn('rule_unsatisfied:experience',self.engine.evaluate(self.application(evidence=[{'type':'diploma'}]),self.context()).reasons)
    def test_revoked_evidence(self):self.assertIn('rule_unsatisfied:diploma',self.engine.evaluate(self.application(evidence=[{'type':'diploma','revoked':True},{'type':'experience'}]),self.context()).reasons)
    def test_expired_evidence(self):self.assertIn('rule_unsatisfied:diploma',self.engine.evaluate(self.application(evidence=[{'type':'diploma','expires':'2026-01-01'},{'type':'experience'}]),self.context()).reasons)
    def test_completion_ratio(self):self.assertEqual(self.engine.evaluate(self.application(evidence=[{'type':'diploma'}]),self.context()).value,decimal.Decimal('0.5'))
    def test_rank_complete_first(self):self.assertTrue(self.engine.rank([self.application(id='partial',evidence=[{'type':'diploma'}]),self.application(id='complete')],self.context())[0].accepted)

