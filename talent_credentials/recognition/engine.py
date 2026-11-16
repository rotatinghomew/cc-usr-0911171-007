from __future__ import annotations
from ..credential_review.evidence_registry import EvidenceRegistry
import datetime as dt
import decimal
import math
import statistics
import ast
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

class DomainError(RuntimeError):
    pass

@dataclass(frozen=True)
class AnalysisResult:
    key: str
    accepted: bool
    value: decimal.Decimal
    reasons: tuple[str,...]
    facts: Mapping[str,Any]=field(default_factory=dict)

class RecognitionEngineBase:
    algorithm='evidence'
    def normalize(self,rows):
        result=[];seen=set()
        for position,row in enumerate(rows):
            item=dict(row);key=str(item.get('id') or position)
            if key in seen:continue
            seen.add(key);item['_key']=key;item['_position']=position;result.append(item)
        return result
    def rank(self,subjects,context):return sorted([self.evaluate(item,context) for item in self.normalize(subjects)],key=lambda result:(not result.accepted,-result.value,result.key))
    def allocate(self,subjects,context):
        remaining=decimal.Decimal(str(context.get('capacity',100)))-decimal.Decimal(str(context.get('reserved',0)));accepted=[];rejected=[]
        by_key={str(item.get('id')):item for item in self.normalize(subjects)}
        for result in self.rank(subjects,context):
            demand=decimal.Decimal(str(by_key[result.key].get('demand',1)))
            if result.accepted and demand<=remaining:accepted.append(result);remaining-=demand
            else:rejected.append(AnalysisResult(result.key,False,result.value,result.reasons+('allocation_exhausted',),result.facts))
        return accepted,rejected
    def summarize(self,results):
        values=[float(result.value) for result in results];reasons=Counter(reason for result in results for reason in result.reasons)
        return {'count':len(results),'accepted':sum(result.accepted for result in results),'rejected':sum(not result.accepted for result in results),'mean':statistics.fmean(values) if values else 0.0,'median':statistics.median(values) if values else 0.0,'reasons':dict(sorted(reasons.items()))}
    def compare(self,left,right):
        a={item.key:item for item in left};b={item.key:item for item in right};changes=[]
        for key in sorted(set(a)|set(b)):
            old=a.get(key);new=b.get(key)
            if old is None:changes.append({'key':key,'change':'added','accepted':new.accepted})
            elif new is None:changes.append({'key':key,'change':'removed','accepted':old.accepted})
            elif old.accepted!=new.accepted or old.value!=new.value:changes.append({'key':key,'change':'changed','from':str(old.value),'to':str(new.value),'accepted_from':old.accepted,'accepted_to':new.accepted})
        return {'changes':changes,'change_count':len(changes)}

class RecognitionEngine(RecognitionEngineBase):
    def __init__(self): self.registry=EvidenceRegistry()
    def evaluate(self,item,context):
        if context.get('rulebook'):
            facts=self.registry.evaluate(item,context['rulebook'],dt.date.fromisoformat(str(context.get('today','2026-11-16')))); return AnalysisResult(str(item.get('id','unknown')),facts['accepted'],decimal.Decimal('1') if facts['accepted'] else decimal.Decimal('0'),tuple(facts['reasons']),facts)
        key=str(item.get('id','unknown'));evidence=item.get('evidence',[]);rules=context.get('rules') or [{'type':'diploma','minimum':1},{'type':'experience','minimum':1}];counts=Counter(entry.get('type') for entry in evidence if not entry.get('revoked'));reasons=[];satisfied=0
        now=dt.date.fromisoformat(str(context.get('today','2026-11-16')))
        for rule in rules:
            available=sum(1 for entry in evidence if entry.get('type')==rule['type'] and not entry.get('revoked') and (not entry.get('expires') or dt.date.fromisoformat(entry['expires'])>=now))
            if available<int(rule.get('minimum',1)):reasons.append('rule_unsatisfied:'+rule['type'])
            else:satisfied+=1
        value=decimal.Decimal(satisfied)/decimal.Decimal(max(1,len(rules)));return AnalysisResult(key,not reasons,value,tuple(reasons),{'counts':dict(counts),'satisfied_rules':satisfied,'total_rules':len(rules)})
