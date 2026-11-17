from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from .evidence_registry import EvidenceRegistry


class ImpactAnalysisError(RuntimeError):
    pass


CAUSE_EXPIRED = 'evidence_expired'
CAUSE_REVOKED = 'evidence_revoked'
CAUSE_RULE_CHANGE = 'rule_change'
CAUSES = (CAUSE_EXPIRED, CAUSE_REVOKED, CAUSE_RULE_CHANGE)
CAUSE_LABELS = {CAUSE_EXPIRED: '材料过期', CAUSE_REVOKED: '材料撤销', CAUSE_RULE_CHANGE: '规则改变'}
DECISIONS = {'accepted', 'rejected'}
OUTCOMES = ('pass_to_fail', 'pass_to_pass', 'fail_to_pass', 'fail_to_fail', 'undetermined')
APPEAL_SCOPES = ('full', 'incremental', 'rule_based', 'none', 'undetermined')
DISCLAIMER = '本分析仅供复核组决策参考，不改写既有认定结果，不代替人工复核；证据不足的个案已明确标记为无法判断。'


class RuleImpactAnalyzer:
    """批量规则影响分析（只读，不改写既有认定结果）。

    基于证据登记（EvidenceRegistry）与规则版本回放能力，对一批已有认定结果的申请
    在候选规则版本下做批量影响分析：识别由通过变为不通过的个案，按影响原因
    （材料过期 / 材料撤销 / 规则改变）与材料类型汇总人数，并给出可回查的个案解释。

    原则：
    - 规则按生效时点解释：基准版本取申请认定时点生效的版本，候选版本取即将生效的版本。
    - 已撤销的材料在任何回放中都不被当作有效，不因旧快照或既有认定结果而复活。
    - 同一份材料按内容摘要去重，不在人数与材料统计中重复计数。
    - 证据不足（缺少认定结果、材料登记缺失或格式异常、原因无法追溯）时标记为无法判断。
    - 分析结果只供决策，不写入任何存储，也不代替人工复核。
    """

    def __init__(self, registry: EvidenceRegistry | None = None):
        self.registry = registry or EvidenceRegistry()

    # ---------- 基础工具 ----------
    def _date(self, value: Any, name: str) -> dt.date:
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        try:
            return dt.date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ImpactAnalysisError(f'{name} must be an ISO date') from exc

    def _effective_from(self, book: Mapping[str, Any]) -> dt.date:
        raw = book.get('effective_from')
        if raw in (None, ''):
            return dt.date.min
        return self._date(raw, 'effective_from')

    def _expired(self, entry: Mapping[str, Any], as_of: dt.date) -> bool:
        raw = entry.get('expires')
        return bool(raw) and self._date(raw, 'expires') < as_of

    # ---------- 规则版本选择（规则按生效时点解释） ----------
    def rulebook_at(self, rulebooks: Sequence[Mapping[str, Any]], moment: dt.date) -> Mapping[str, Any]:
        if not rulebooks:
            raise ImpactAnalysisError('at least one rulebook version is required')
        ordered = sorted(enumerate(rulebooks), key=lambda item: (self._effective_from(item[1]), item[0]))
        chosen = ordered[0][1]
        for _, book in ordered:
            if self._effective_from(book) <= moment:
                chosen = book
        return chosen

    def select_versions(self, rulebooks: Sequence[Mapping[str, Any]], today: dt.date,
                        baseline_version: str | None = None, candidate_version: str | None = None
                        ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        books = list(rulebooks)
        if not books:
            raise ImpactAnalysisError('at least one rulebook version is required')
        by_version: dict[str, Mapping[str, Any]] = {}
        for book in books:
            version = str(book.get('version', '')).strip()
            if not version:
                raise ImpactAnalysisError('rulebook version is required')
            if version in by_version:
                raise ImpactAnalysisError(f'duplicate rulebook version {version}')
            rules = book.get('rules')
            if not isinstance(rules, list):
                raise ImpactAnalysisError(f'rulebook {version} rules must be a list')
            for rule in rules:
                if not isinstance(rule, Mapping) or not str(rule.get('type', '')).strip():
                    raise ImpactAnalysisError(f'rulebook {version} has a rule without type')
                try:
                    int(rule.get('minimum', 1))
                except (TypeError, ValueError) as exc:
                    raise ImpactAnalysisError(f'rulebook {version} has an invalid minimum') from exc
            by_version[version] = book
        baseline = candidate = None
        if baseline_version is not None:
            baseline = by_version.get(str(baseline_version))
            if baseline is None:
                raise ImpactAnalysisError(f'unknown baseline version {baseline_version}')
        if candidate_version is not None:
            candidate = by_version.get(str(candidate_version))
            if candidate is None:
                raise ImpactAnalysisError(f'unknown candidate version {candidate_version}')
        if candidate is None:
            upcoming = [book for book in books if self._effective_from(book) > today]
            candidate = min(upcoming, key=self._effective_from) if upcoming else self.rulebook_at(books, dt.date.max)
        if baseline is None:
            cutoff = min(today, self._effective_from(candidate))
            earlier = [book for book in books if book is not candidate and self._effective_from(book) <= cutoff]
            pool = earlier or [book for book in books if book is not candidate] or books
            baseline = self.rulebook_at(pool, today)
        return baseline, candidate

    # ---------- 证据状态 ----------
    def _validate_entries(self, entries: Sequence[Any]) -> list[str]:
        problems = []
        for entry in entries:
            if not isinstance(entry, Mapping) or not str(entry.get('type', '')).strip():
                problems.append('malformed_evidence')
                break
            raw = entry.get('expires')
            if raw not in (None, ''):
                try:
                    self._date(raw, 'expires')
                except ImpactAnalysisError:
                    problems.append('malformed_expires')
                    break
        return problems

    def _partition(self, entries: Sequence[Mapping[str, Any]], as_of: dt.date):
        active, expired, revoked = [], [], []
        for entry in entries:
            if entry.get('revoked'):
                revoked.append(entry)
            elif self._expired(entry, as_of):
                expired.append(entry)
            else:
                active.append(entry)
        return active, expired, revoked

    def _classify_unsatisfied(self, rtype: str, minimum: int, previous_minimum: int,
                              entries: Sequence[Mapping[str, Any]], as_of: dt.date) -> dict[str, Any]:
        of_type = [entry for entry in entries if str(entry.get('type')) == rtype]
        active, expired, revoked = self._partition(of_type, as_of)
        factors = []
        # 规则改变：当前有效材料在旧门槛下本可通过，但在新门槛下不足（含新增材料类型要求）。
        if len(active) >= previous_minimum and len(active) < minimum:
            factors.append(CAUSE_RULE_CHANGE)
        # 材料撤销 / 材料过期：该类型下存在已撤销或已过期材料，与缺口直接相关。
        if revoked:
            factors.append(CAUSE_REVOKED)
        if expired:
            factors.append(CAUSE_EXPIRED)
        return {
            'type': rtype,
            'minimum': minimum,
            'previous_minimum': previous_minimum,
            'active': len(active),
            'revoked': len(revoked),
            'expired': len(expired),
            'factors': factors,
            'revoked_materials': sorted({self.registry.digest(entry) for entry in revoked}),
            'expired_materials': sorted({self.registry.digest(entry) for entry in expired}),
        }

    def _explain_impact(self, unsatisfied: Sequence[Mapping[str, Any]]) -> str:
        parts = []
        for rule in unsatisfied:
            head = f"材料类型「{rule['type']}」当前有效 {rule['active']} 份，低于候选规则要求的 {rule['minimum']} 份"
            if rule['factors']:
                labels = '、'.join(CAUSE_LABELS[factor] for factor in rule['factors'])
                parts.append(f'{head}，影响因素：{labels}')
            else:
                parts.append(f'{head}，但现有证据无法判断原因')
        if any(not rule['factors'] for rule in unsatisfied):
            return '认定结果将由通过变为不通过：' + '；'.join(parts) + '；存在无法判断的因素，需人工补充证据后再评估。'
        return '认定结果将由通过变为不通过：' + '；'.join(parts) + '。'

    # ---------- 个案分析 ----------
    def analyze_case(self, application: Mapping[str, Any], rulebooks: Sequence[Mapping[str, Any]],
                     baseline_book: Mapping[str, Any], candidate_book: Mapping[str, Any],
                     as_of: dt.date) -> dict[str, Any]:
        identifier = str(application.get('id') or application.get('application_id') or '').strip() or 'unknown'
        raw_decision = application.get('decision')
        decision = str(raw_decision).strip().lower() if raw_decision is not None else ''
        problems = []
        if not decision:
            problems.append('missing_decision')
        elif decision not in DECISIONS:
            problems.append('unknown_decision')
        raw_evidence = application.get('evidence')
        if raw_evidence is None:
            problems.append('missing_evidence')
            entries: list[Mapping[str, Any]] = []
        elif not isinstance(raw_evidence, list):
            problems.append('malformed_evidence')
            entries = []
        else:
            entries = list(raw_evidence)
            problems.extend(self._validate_entries(entries))
        case_baseline = baseline_book
        decided_at = application.get('decided_at')
        if decided_at not in (None, ''):
            try:
                case_baseline = self.rulebook_at(rulebooks, self._date(decided_at, 'decided_at'))
            except ImpactAnalysisError:
                problems.append('malformed_decided_at')
        versions = {'baseline_version': str(case_baseline.get('version')),
                    'candidate_version': str(candidate_book.get('version'))}
        state = None
        if not {'missing_evidence', 'malformed_evidence', 'malformed_expires'} & set(problems):
            active, expired, revoked = self._partition(entries, as_of)
            state = {'active': len(active), 'expired': len(expired), 'revoked': len(revoked)}
        if problems:
            return {
                'application_id': identifier,
                'outcome': 'undetermined',
                'recorded_decision': decision or None,
                **versions,
                'baseline_accepted': None,
                'candidate_accepted': None,
                'causes': [],
                'undetermined': True,
                'undetermined_reasons': sorted(set(problems)),
                'unsatisfied_rules': [],
                'evidence_state': state,
                'explanation': '证据不足，无法判断该申请受规则更新的影响（' + '、'.join(sorted(set(problems))) + '）。',
            }
        baseline_eval = self.registry.evaluate({'evidence': entries}, case_baseline, as_of)
        candidate_eval = self.registry.evaluate({'evidence': entries}, candidate_book, as_of)
        base = {
            'application_id': identifier,
            'recorded_decision': decision,
            **versions,
            'baseline_accepted': baseline_eval['accepted'],
            'candidate_accepted': candidate_eval['accepted'],
            'evidence_state': state,
        }
        if decision == 'accepted' and not candidate_eval['accepted']:
            outcome = 'pass_to_fail'
        elif decision == 'accepted':
            outcome = 'pass_to_pass'
        elif not candidate_eval['accepted']:
            outcome = 'fail_to_fail'
        else:
            outcome = 'fail_to_pass'
        if outcome != 'pass_to_fail':
            explanations = {
                'pass_to_pass': '候选规则版本下仍通过，不受本次更新影响。',
                'fail_to_pass': '候选规则版本下将由不通过变为通过。',
                'fail_to_fail': '候选规则版本下仍不通过。',
            }
            return {**base, 'outcome': outcome, 'causes': [], 'undetermined': False,
                    'undetermined_reasons': [], 'unsatisfied_rules': [], 'explanation': explanations[outcome]}
        previous_minimums = {str(rule.get('type')): int(rule.get('minimum', 1)) for rule in case_baseline.get('rules', [])}
        candidate_minimums = {str(rule.get('type')): int(rule.get('minimum', 1)) for rule in candidate_book.get('rules', [])}
        unsatisfied = []
        for reason in candidate_eval['reasons']:
            if not str(reason).startswith('rule_unsatisfied:'):
                continue
            rtype = str(reason).split(':', 1)[1]
            unsatisfied.append(self._classify_unsatisfied(
                rtype, candidate_minimums.get(rtype, 1), previous_minimums.get(rtype, 0), entries, as_of))
        causes = sorted({factor for rule in unsatisfied for factor in rule['factors']})
        factorless = [rule['type'] for rule in unsatisfied if not rule['factors']]
        return {
            **base,
            'outcome': 'pass_to_fail',
            'causes': causes,
            'undetermined': bool(factorless),
            'undetermined_reasons': ['cause_undetermined:' + rtype for rtype in factorless],
            'unsatisfied_rules': unsatisfied,
            'explanation': self._explain_impact(unsatisfied),
        }

    # ---------- 申诉重审范围 ----------
    def analyze_appeal(self, appeal: Mapping[str, Any], cases: Mapping[str, Mapping[str, Any]],
                       entries_by_application: Mapping[str, Sequence[Mapping[str, Any]]],
                       as_of: dt.date) -> dict[str, Any]:
        appeal_id = str(appeal.get('appeal_id') or appeal.get('id') or '').strip() or 'unknown'
        application_id = str(appeal.get('application_id') or '').strip()
        case = cases.get(application_id)
        if case is None:
            return {'appeal_id': appeal_id, 'application_id': application_id or None, 're_review': None,
                    'scope': 'undetermined', 'reasons': ['unknown_application'],
                    'added_materials': [], 'invalidated_materials': [],
                    'explanation': '申诉对应的申请不在本次分析范围内，无法判断。'}
        impacted = case['outcome'] == 'pass_to_fail'
        snapshot = appeal.get('evidence_at_appeal')
        if not isinstance(snapshot, list):
            reasons = ['missing_appeal_snapshot'] + (['application_impacted'] if impacted else [])
            return {'appeal_id': appeal_id, 'application_id': application_id, 're_review': None,
                    'scope': 'undetermined', 'reasons': reasons,
                    'added_materials': [], 'invalidated_materials': [],
                    'explanation': '缺少申诉时的材料快照，无法判断申诉范围' + ('；但申请本身受规则更新影响，建议人工关注。' if impacted else '。')}
        entries = entries_by_application.get(application_id, [])
        current_digests = {self.registry.digest(entry) for entry in entries}
        active, expired, _ = self._partition(entries, as_of)
        active_digests = {self.registry.digest(entry) for entry in active}
        expired_digests = {self.registry.digest(entry) for entry in expired}
        old_digests = {self.registry.digest(row) for row in snapshot if isinstance(row, Mapping)}
        added = sorted(active_digests - old_digests)
        removed = sorted(digest for digest in old_digests if digest not in current_digests)
        lapsed = sorted(digest for digest in old_digests & expired_digests)
        invalidated = sorted(set(removed) | set(lapsed))
        reasons = []
        if removed:
            reasons.append('evidence_removed')
        if lapsed:
            reasons.append('evidence_expired')
        if added:
            reasons.append('evidence_added')
        if impacted:
            reasons.append('application_impacted')
        # 补交材料只需增量审查；材料被撤销或失效则动摇申诉基础，需全面重审。
        if invalidated:
            scope, re_review = 'full', True
            explanation = '申诉所依据的材料已撤销或失效，需对申诉全面重审。'
        elif added:
            scope, re_review = 'incremental', True
            explanation = '申请人补交了新材料，仅需对补交材料做增量审查，无需全面重审。'
        elif impacted:
            scope, re_review = 'rule_based', True
            explanation = '申请受规则版本更新影响由通过变为不通过，申诉需按候选规则重新审。'
        else:
            scope, re_review = 'none', False
            explanation = '材料与规则影响均未发生变化，申诉无需重新审。'
        return {'appeal_id': appeal_id, 'application_id': application_id, 're_review': re_review,
                'scope': scope, 'reasons': reasons, 'added_materials': added,
                'invalidated_materials': invalidated, 'explanation': explanation}

    # ---------- 批量分析 ----------
    def analyze(self, applications: Sequence[Mapping[str, Any]], rulebooks: Sequence[Mapping[str, Any]],
                today: Any = None, appeals: Sequence[Mapping[str, Any]] = (),
                baseline_version: str | None = None, candidate_version: str | None = None,
                as_of: Any = None) -> dict[str, Any]:
        today_date = self._date(today, 'today') if today is not None else dt.date.today()
        books = list(rulebooks)
        baseline, candidate = self.select_versions(books, today_date, baseline_version, candidate_version)
        # 影响评估时点：候选规则尚未生效时按其生效日投影，否则按当前日期。
        as_of_date = self._date(as_of, 'as_of') if as_of is not None else max(today_date, self._effective_from(candidate))
        unique: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for position, application in enumerate(applications):
            row = dict(application)
            identifier = str(row.get('id') or row.get('application_id') or f'row-{position}').strip()
            if identifier in unique:
                duplicates += 1
                continue
            row['id'] = identifier
            unique[identifier] = row
        cases: dict[str, dict[str, Any]] = {}
        entries_by_id: dict[str, list[Mapping[str, Any]]] = {}
        for identifier, row in unique.items():
            cases[identifier] = self.analyze_case(row, books, baseline, candidate, as_of_date)
            raw = row.get('evidence')
            entries_by_id[identifier] = list(raw) if isinstance(raw, list) else []
        case_list = [cases[key] for key in sorted(cases)]
        outcomes = Counter(case['outcome'] for case in case_list)
        impacted = [case for case in case_list if case['outcome'] == 'pass_to_fail']
        determined = [case for case in impacted if not case['undetermined']]
        by_cause = {cause: 0 for cause in CAUSES}
        by_primary = {cause: 0 for cause in CAUSES} | {'multiple': 0}
        type_applications: dict[str, set] = defaultdict(set)
        type_materials: dict[str, set] = defaultdict(set)
        type_causes: dict[str, Counter] = defaultdict(Counter)
        for case in determined:
            for cause in case['causes']:
                by_cause[cause] += 1
            primary = case['causes'][0] if len(case['causes']) == 1 else 'multiple'
            by_primary[primary] += 1
            for rule in case['unsatisfied_rules']:
                rtype = rule['type']
                type_applications[rtype].add(case['application_id'])
                type_materials[rtype].update(rule['revoked_materials'])
                type_materials[rtype].update(rule['expired_materials'])
                for factor in rule['factors']:
                    type_causes[rtype][factor] += 1
        by_evidence_type = {}
        for rtype in sorted(type_applications):
            by_evidence_type[rtype] = {
                'applications': len(type_applications[rtype]),
                'distinct_materials': len(type_materials[rtype]),
                'causes': {cause: type_causes[rtype].get(cause, 0) for cause in CAUSES},
            }
        implicated: set = set()
        for digests in type_materials.values():
            implicated.update(digests)
        appeal_items = [self.analyze_appeal(appeal, cases, entries_by_id, as_of_date) for appeal in appeals]
        appeal_items.sort(key=lambda item: item['appeal_id'])
        appeal_scopes = Counter(item['scope'] for item in appeal_items)
        return {
            'analysis': 'batch_rule_impact',
            'decision_support_only': True,
            'today': today_date.isoformat(),
            'as_of': as_of_date.isoformat(),
            'rulebooks': {
                'baseline': str(baseline.get('version')),
                'candidate': str(candidate.get('version')),
                'baseline_effective_from': str(baseline['effective_from']) if baseline.get('effective_from') else None,
                'candidate_effective_from': str(candidate['effective_from']) if candidate.get('effective_from') else None,
            },
            'cause_labels': dict(CAUSE_LABELS),
            'totals': {'applications': len(case_list), 'duplicates_ignored': duplicates, 'appeals': len(appeal_items)},
            'outcomes': {outcome: outcomes.get(outcome, 0) for outcome in OUTCOMES},
            'impact': {
                'total': len(impacted),
                'determined': len(determined),
                'undetermined': len(impacted) - len(determined),
                'by_cause': by_cause,
                'by_primary_cause': by_primary,
                'by_evidence_type': by_evidence_type,
                'distinct_materials_implicated': len(implicated),
            },
            'appeals': {
                'total': len(appeal_items),
                'requiring_re_review': sum(1 for item in appeal_items if item['re_review']),
                'undetermined': sum(1 for item in appeal_items if item['re_review'] is None),
                'by_scope': {scope: appeal_scopes.get(scope, 0) for scope in APPEAL_SCOPES},
                'items': appeal_items,
            },
            'cases': case_list,
            'disclaimer': DISCLAIMER,
        }
