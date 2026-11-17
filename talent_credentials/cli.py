from __future__ import annotations
import argparse
import json
from pathlib import Path
from .credential_review.impact_analysis import RuleImpactAnalyzer
from .reporting import Reporter
from .service import ApplicationService
from .storage import Database

def load_json(path:str):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def main(argv=None):
    parser=argparse.ArgumentParser(prog='talent_credentials')
    parser.add_argument('--database',default=':memory:')
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run'); run.add_argument('--fixture',required=True); run.add_argument('--output',required=True)
    impact=sub.add_parser('impact'); impact.add_argument('--fixture',required=True); impact.add_argument('--output',required=True)
    report=sub.add_parser('report'); report.add_argument('--kind')
    args=parser.parse_args(argv); database=Database(args.database)
    if args.command=='impact':
        fixture=load_json(args.fixture); context=fixture.get('context',{})
        analysis=RuleImpactAnalyzer().analyze(fixture.get('applications',[]),fixture.get('rulebooks',[]),context.get('today'),appeals=fixture.get('appeals',[]),baseline_version=context.get('baseline_version'),candidate_version=context.get('candidate_version'),as_of=context.get('as_of'))
        payload={'product':'人才认定证据版本与复核申诉注册库','analysis':'batch_rule_impact','report':analysis}
        Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(payload,ensure_ascii=False,sort_keys=True)); return 0
    if args.command=='run':
        fixture=load_json(args.fixture); service=ApplicationService(database); outputs=[]
        for index,item in enumerate(fixture.get('items',[])):
            outputs.append(service.process(item,fixture.get('context',{}),item.get('request_key',f'item-{index}')))
        payload={'product':'人才认定证据版本与复核申诉注册库','processed':len(outputs),'outputs':outputs,'report':Reporter(database).overview()}
        Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(payload,ensure_ascii=False,sort_keys=True)); return 0
    payload=Reporter(database).owner_breakdown(args.kind) if args.kind else Reporter(database).overview(); print(json.dumps(payload,ensure_ascii=False,sort_keys=True)); return 0
