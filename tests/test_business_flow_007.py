import datetime as dt
import unittest
from talent_credentials.credential_review.evidence_registry import EvidenceRegistry
class EvidenceRegistryTests(unittest.TestCase):
 def setUp(self):self.r=EvidenceRegistry();self.app={'evidence':[{'type':'diploma','issuer':'u','serial':'1'},{'type':'experience','issuer':'c','serial':'2'}]};self.book={'version':'v1','rules':[{'type':'diploma'},{'type':'experience'}]}
 def test_accepts_complete_evidence(self):self.assertTrue(self.r.evaluate(self.app,self.book,dt.date(2026,11,16))['accepted'])
 def test_revocation_changes_decision(self):self.assertFalse(self.r.evaluate({'evidence':[{**self.app['evidence'][0],'revoked':True},self.app['evidence'][1]]},self.book,dt.date(2026,11,16))['accepted'])
 def test_expiry(self):self.assertFalse(self.r.evaluate({'evidence':[{**self.app['evidence'][0],'expires':'2026-01-01'},self.app['evidence'][1]]},self.book,dt.date(2026,11,16))['accepted'])
 def test_rulebook_replay(self):self.assertIn({'from':'v1','to':'v2','accepted_from':True,'accepted_to':False},self.r.changed_decisions(self.r.replay(self.app,[self.book,{'version':'v2','rules':[{'type':'diploma','minimum':2}]}],dt.date(2026,11,16))))
 def test_appeal_scope(self):self.assertFalse(self.r.appeal_scope(self.app['evidence'],self.app['evidence']+[{'type':'award','serial':'3'}])['requires_full_review'])
