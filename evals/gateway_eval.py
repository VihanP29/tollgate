"""Reproduce the current-gateway fixture comparison without database/executor access."""
import argparse
import json
from pathlib import Path
from tollgate.evaluation.datasets import manifest, build_case, validate_manifest
from tollgate.evaluation.evaluator import evaluate_arm, hard_checks, verdict
from tollgate.evaluation.metrics import summarize
from tollgate.policy_config import migrate
from tollgate.semantic_policy import PRESETS
from tollgate.decision_context import identity


def run(split='development'):
    data=manifest();validate_manifest(data)
    policy=migrate(PRESETS[0]);rows=[];inputs={}
    for case in data['cases']:
        if case['split']!=split:continue
        value=build_case(case)
        inputs[case['id']]=value.model_dump(mode='json')
        for backend in ['hard_constraints','general_llm_unavailable','jev_fixture']:
            result=evaluate_arm(value,policy,backend)
            result.update(case_id=case['id'],family_id=case['family_id'],expected=case['expected'],adjudication=case['adjudication'],
                          arm=backend+':'+policy['id'],input_hash=value.input_hash,hard_pass=verdict(hard_checks(value,policy))!='deny')
            rows.append(result)
    frozen={'dataset_id':data['id'],'dataset_hash':data['manifest_hash'],'split':split,'backend':'offline-fixture-checks',
            'policy':policy,'inputs':inputs,'contracts':['minor-usd-1','finance-1','studio-1'],'admission_eligible':False}
    return {'schema_version':1,'manifest':frozen,'manifest_hash':identity(frozen),'rows':rows,'metrics':summarize(rows),
            'paid_calls':0,'live_verification':False,'deployment_verification':False,'cost_usd':None}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split',choices=['development','held_out'],default='development')
    parser.add_argument('--output',type=Path,default=Path('.runtime/gateway-fixtures.json'))
    args=parser.parse_args();result=run(args.split)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'rows':len(result['rows']),'paid_calls':0,'admission_eligible':False,'output':str(args.output)}))


if __name__=='__main__':main()
