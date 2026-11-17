import json
import tempfile
import unittest
from pathlib import Path

from talent_credentials.cli import main
from talent_credentials.credential_review.impact_analysis import (
    CAUSE_EXPIRED, CAUSE_REVOKED, CAUSE_RULE_CHANGE, ImpactAnalysisError, RuleImpactAnalyzer)
from talent_credentials.storage import Database

V1 = {'version': 'v1', 'effective_from': '2026-01-01',
      'rules': [{'type': 'diploma', 'minimum': 1}, {'type': 'experience', 'minimum': 1}]}
V2 = {'version': 'v2', 'effective_from': '2026-12-01',
      'rules': [{'type': 'diploma', 'minimum': 1}, {'type': 'experience', 'minimum': 2}]}
TODAY = '2026-11-17'
FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'impact_analysis.json'


def application(identifier, decision='accepted', evidence=None, **extra):
    row = {'id': identifier, 'decision': decision, 'evidence': list(evidence) if evidence is not None else []}
    row.update(extra)
    return row


class RuleImpactAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = RuleImpactAnalyzer()

    def analyze(self, applications, appeals=(), **kwargs):
        return self.analyzer.analyze(applications, [V1, V2], TODAY, appeals=appeals, **kwargs)

    def test_rule_change_cause(self):
        report = self.analyze([application('p1', evidence=[
            {'type': 'diploma', 'serial': 'D1'}, {'type': 'experience', 'serial': 'E1'}])])
        self.assertEqual(report['impact']['total'], 1)
        self.assertEqual(report['impact']['by_cause'][CAUSE_RULE_CHANGE], 1)
        self.assertEqual(report['impact']['by_cause'][CAUSE_REVOKED], 0)
        self.assertEqual(report['impact']['by_cause'][CAUSE_EXPIRED], 0)
        case = report['cases'][0]
        self.assertEqual(case['causes'], [CAUSE_RULE_CHANGE])
        self.assertTrue(case['baseline_accepted'])
        self.assertFalse(case['candidate_accepted'])
        self.assertEqual(case['baseline_version'], 'v1')
        self.assertEqual(case['candidate_version'], 'v2')
        self.assertIn('规则改变', case['explanation'])

    def test_expired_material_cause(self):
        report = self.analyze([application('p1', evidence=[
            {'type': 'diploma', 'serial': 'D1', 'expires': '2026-06-01'},
            {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2'}])])
        case = report['cases'][0]
        self.assertEqual(case['causes'], [CAUSE_EXPIRED])
        self.assertEqual(report['impact']['by_evidence_type']['diploma']['distinct_materials'], 1)
        self.assertEqual(report['as_of'], '2026-12-01')

    def test_revoked_material_not_validated_by_old_snapshot(self):
        report = self.analyze([application('p1', evidence=[
            {'type': 'diploma', 'serial': 'D1', 'revoked': True},
            {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2'}])])
        case = report['cases'][0]
        self.assertEqual(case['causes'], [CAUSE_REVOKED])
        self.assertFalse(case['baseline_accepted'])
        self.assertEqual(case['evidence_state'], {'active': 2, 'expired': 0, 'revoked': 1})

    def test_mixed_causes(self):
        report = self.analyze([application('p1', evidence=[
            {'type': 'diploma', 'serial': 'D1'},
            {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2', 'revoked': True}])])
        case = report['cases'][0]
        self.assertEqual(case['causes'], [CAUSE_REVOKED, CAUSE_RULE_CHANGE])
        self.assertEqual(report['impact']['by_primary_cause']['multiple'], 1)

    def test_undetermined_when_evidence_missing(self):
        report = self.analyze([application('p1')])
        case = report['cases'][0]
        self.assertEqual(case['outcome'], 'pass_to_fail')
        self.assertTrue(case['undetermined'])
        self.assertEqual(case['causes'], [])
        self.assertEqual(report['impact']['undetermined'], 1)
        self.assertEqual(report['impact']['determined'], 0)
        self.assertEqual(report['impact']['by_cause'], {CAUSE_EXPIRED: 0, CAUSE_REVOKED: 0, CAUSE_RULE_CHANGE: 0})

    def test_undetermined_when_decision_missing(self):
        row = application('p1', evidence=[{'type': 'diploma', 'serial': 'D1'}])
        del row['decision']
        case = self.analyze([row])['cases'][0]
        self.assertEqual(case['outcome'], 'undetermined')
        self.assertIn('missing_decision', case['undetermined_reasons'])
        self.assertIsNone(case['baseline_accepted'])

    def test_undetermined_when_evidence_malformed(self):
        case = self.analyze([application('p1', evidence=[{'serial': 'X'}])])['cases'][0]
        self.assertEqual(case['outcome'], 'undetermined')
        self.assertIn('malformed_evidence', case['undetermined_reasons'])

    def test_baseline_follows_decision_time(self):
        recent = application('p1', evidence=[{'type': 'diploma', 'serial': 'D1'}, {'type': 'experience', 'serial': 'E1'}],
                             decided_at='2026-12-05')
        case = self.analyze([recent])['cases'][0]
        self.assertEqual(case['baseline_version'], 'v2')
        self.assertTrue(case['undetermined'])
        early = application('p2', evidence=[{'type': 'diploma', 'serial': 'D2'}, {'type': 'experience', 'serial': 'E2'}],
                            decided_at='2026-03-01')
        case = self.analyze([early])['cases'][0]
        self.assertEqual(case['baseline_version'], 'v1')
        self.assertEqual(case['causes'], [CAUSE_RULE_CHANGE])

    def test_materials_not_double_counted(self):
        shared = {'type': 'diploma', 'issuer': 'university', 'serial': 'D-shared', 'revoked': True}
        report = self.analyze([
            application('p1', evidence=[shared, {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2'}]),
            application('p2', evidence=[dict(shared), {'type': 'experience', 'serial': 'E3'}, {'type': 'experience', 'serial': 'E4'}])])
        self.assertEqual(report['impact']['total'], 2)
        diploma = report['impact']['by_evidence_type']['diploma']
        self.assertEqual(diploma['applications'], 2)
        self.assertEqual(diploma['distinct_materials'], 1)
        self.assertEqual(report['impact']['distinct_materials_implicated'], 1)

    def test_duplicate_rows_within_application_counted_once(self):
        report = self.analyze([application('p1', evidence=[
            {'type': 'diploma', 'serial': 'D1', 'revoked': True},
            {'type': 'diploma', 'serial': 'D1', 'revoked': True},
            {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2'}])])
        case = report['cases'][0]
        diploma = [rule for rule in case['unsatisfied_rules'] if rule['type'] == 'diploma'][0]
        self.assertEqual(len(diploma['revoked_materials']), 1)
        self.assertEqual(report['impact']['by_evidence_type']['diploma']['distinct_materials'], 1)

    def test_duplicate_applications_ignored(self):
        row = application('p1', evidence=[{'type': 'diploma', 'serial': 'D1'}, {'type': 'experience', 'serial': 'E1'}])
        report = self.analyze([row, dict(row)])
        self.assertEqual(report['totals']['applications'], 1)
        self.assertEqual(report['totals']['duplicates_ignored'], 1)

    def test_appeal_full_review_on_revoked(self):
        evidence = [{'type': 'diploma', 'serial': 'D1', 'revoked': True},
                    {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2'}]
        appeal = {'appeal_id': 'a1', 'application_id': 'p1', 'evidence_at_appeal': [
            {'type': 'diploma', 'serial': 'D1'},
            {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2'}]}
        item = self.analyze([application('p1', evidence=evidence)], appeals=[appeal])['appeals']['items'][0]
        self.assertEqual(item['scope'], 'full')
        self.assertTrue(item['re_review'])
        self.assertIn('evidence_removed', item['reasons'])
        self.assertIn('application_impacted', item['reasons'])
        self.assertEqual(len(item['invalidated_materials']), 1)

    def test_appeal_incremental_on_supplement(self):
        evidence = [{'type': 'diploma', 'serial': 'D1'},
                    {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2'}]
        appeal = {'appeal_id': 'a1', 'application_id': 'p1', 'evidence_at_appeal': [
            {'type': 'diploma', 'serial': 'D1'}, {'type': 'experience', 'serial': 'E1'}]}
        item = self.analyze([application('p1', evidence=evidence)], appeals=[appeal])['appeals']['items'][0]
        self.assertEqual(item['scope'], 'incremental')
        self.assertTrue(item['re_review'])
        self.assertEqual(item['reasons'], ['evidence_added'])
        self.assertEqual(len(item['added_materials']), 1)

    def test_appeal_rule_based_on_impact(self):
        evidence = [{'type': 'diploma', 'serial': 'D1'}, {'type': 'experience', 'serial': 'E1'}]
        appeal = {'appeal_id': 'a1', 'application_id': 'p1', 'evidence_at_appeal': list(evidence)}
        item = self.analyze([application('p1', evidence=evidence)], appeals=[appeal])['appeals']['items'][0]
        self.assertEqual(item['scope'], 'rule_based')
        self.assertTrue(item['re_review'])
        self.assertEqual(item['reasons'], ['application_impacted'])

    def test_appeal_none_when_unchanged(self):
        evidence = [{'type': 'diploma', 'serial': 'D1'},
                    {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2'}]
        appeal = {'appeal_id': 'a1', 'application_id': 'p1', 'evidence_at_appeal': list(evidence)}
        item = self.analyze([application('p1', evidence=evidence)], appeals=[appeal])['appeals']['items'][0]
        self.assertEqual(item['scope'], 'none')
        self.assertFalse(item['re_review'])

    def test_appeal_undetermined_without_snapshot(self):
        item = self.analyze([application('p1', evidence=[{'type': 'diploma', 'serial': 'D1'}, {'type': 'experience', 'serial': 'E1'}])],
                            appeals=[{'appeal_id': 'a1', 'application_id': 'p1'}])['appeals']['items'][0]
        self.assertEqual(item['scope'], 'undetermined')
        self.assertIsNone(item['re_review'])
        self.assertIn('missing_appeal_snapshot', item['reasons'])
        self.assertIn('application_impacted', item['reasons'])

    def test_appeal_unknown_application(self):
        item = self.analyze([], appeals=[{'appeal_id': 'a1', 'application_id': 'ghost'}])['appeals']['items'][0]
        self.assertEqual(item['scope'], 'undetermined')
        self.assertEqual(item['reasons'], ['unknown_application'])

    def test_fail_to_pass(self):
        case = self.analyze([application('p1', decision='rejected', evidence=[
            {'type': 'diploma', 'serial': 'D1'},
            {'type': 'experience', 'serial': 'E1'}, {'type': 'experience', 'serial': 'E2'}])])['cases'][0]
        self.assertEqual(case['outcome'], 'fail_to_pass')

    def test_read_only_and_deterministic(self):
        evidence = [{'type': 'diploma', 'serial': 'D1', 'revoked': True}, {'type': 'experience', 'serial': 'E1'}]
        apps = [application('p1', evidence=evidence)]
        first = self.analyze(apps)
        second = self.analyze(apps)
        self.assertEqual(first, second)
        self.assertNotIn('digest', evidence[0])
        self.assertTrue(evidence[0]['revoked'])

    def test_version_selection_errors(self):
        with self.assertRaises(ImpactAnalysisError):
            self.analyzer.analyze([], [V1, V2], TODAY, baseline_version='v9')
        with self.assertRaises(ImpactAnalysisError):
            self.analyzer.analyze([], [V1, V2], TODAY, candidate_version='v9')
        with self.assertRaises(ImpactAnalysisError):
            self.analyzer.analyze([], [], TODAY)
        with self.assertRaises(ImpactAnalysisError):
            self.analyzer.analyze([], [{'version': 'v1'}], TODAY)
        with self.assertRaises(ImpactAnalysisError):
            self.analyzer.analyze([], [{'version': 'v1', 'rules': [{'minimum': 1}]}], TODAY)
        with self.assertRaises(ImpactAnalysisError):
            self.analyzer.analyze([], [V1, dict(V1)], TODAY)

    def test_explicit_versions(self):
        report = self.analyzer.analyze(
            [application('p1', evidence=[{'type': 'diploma', 'serial': 'D1'}, {'type': 'experience', 'serial': 'E1'}])],
            [V1, V2], TODAY, baseline_version='v1', candidate_version='v2', as_of='2026-11-17')
        self.assertEqual(report['as_of'], '2026-11-17')
        self.assertEqual(report['rulebooks']['baseline'], 'v1')
        self.assertEqual(report['rulebooks']['candidate'], 'v2')

    def test_report_structure(self):
        report = self.analyze([application('p1', evidence=[
            {'type': 'diploma', 'serial': 'D1'}, {'type': 'experience', 'serial': 'E1'}])])
        self.assertEqual(report['analysis'], 'batch_rule_impact')
        self.assertTrue(report['decision_support_only'])
        self.assertIn('不改写既有认定结果', report['disclaimer'])
        self.assertIn('不代替人工复核', report['disclaimer'])
        self.assertEqual(set(report['outcomes']),
                         {'pass_to_fail', 'pass_to_pass', 'fail_to_pass', 'fail_to_fail', 'undetermined'})
        self.assertEqual(report['rulebooks'],
                         {'baseline': 'v1', 'candidate': 'v2',
                          'baseline_effective_from': '2026-01-01', 'candidate_effective_from': '2026-12-01'})
        self.assertEqual(report['cause_labels'][CAUSE_RULE_CHANGE], '规则改变')


class ImpactCliTests(unittest.TestCase):
    def test_impact_command(self):
        with tempfile.TemporaryDirectory() as folder:
            database_path = str(Path(folder) / 'impact.sqlite3')
            output = str(Path(folder) / 'impact.json')
            code = main(['--database', database_path, 'impact', '--fixture', str(FIXTURE), '--output', output])
            self.assertEqual(code, 0)
            payload = json.loads(Path(output).read_text(encoding='utf-8'))
            report = payload['report']
            self.assertEqual(payload['analysis'], 'batch_rule_impact')
            self.assertEqual(report['impact']['total'], 5)
            self.assertEqual(report['impact']['determined'], 4)
            self.assertEqual(report['impact']['undetermined'], 1)
            self.assertEqual(report['impact']['by_cause'],
                             {CAUSE_EXPIRED: 1, CAUSE_REVOKED: 2, CAUSE_RULE_CHANGE: 2})
            self.assertEqual(report['impact']['by_primary_cause'],
                             {CAUSE_EXPIRED: 1, CAUSE_REVOKED: 1, CAUSE_RULE_CHANGE: 1, 'multiple': 1})
            self.assertEqual(report['impact']['distinct_materials_implicated'], 3)
            self.assertEqual(report['impact']['by_evidence_type']['diploma']['applications'], 2)
            self.assertEqual(report['impact']['by_evidence_type']['experience']['applications'], 2)
            self.assertEqual(report['outcomes'],
                             {'pass_to_fail': 5, 'pass_to_pass': 2, 'fail_to_pass': 1,
                              'fail_to_fail': 0, 'undetermined': 0})
            self.assertEqual(report['appeals']['requiring_re_review'], 3)
            self.assertEqual(report['appeals']['undetermined'], 1)
            self.assertEqual(report['appeals']['by_scope'],
                             {'full': 1, 'incremental': 1, 'rule_based': 1, 'none': 0, 'undetermined': 1})
            self.assertEqual(Database(database_path).counts(), {})
