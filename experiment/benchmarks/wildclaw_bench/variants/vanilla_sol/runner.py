"""Frozen V1.1 baseline: original A0 instructions, Sol/medium, no DCI library."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import time

from experiment.shared.codex_backend import CodexBackend, CodexSettings, build_prompt
from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json, sha256, workflow_config
from experiment.benchmarks.wildclaw_bench.src.inference_access import inference_client
from experiment.benchmarks.wildclaw_bench.src.mock_state import capture_mock_state, preflight_mock_state
from experiment.benchmarks.wildclaw_bench.src.runtime_support import make_runtime_config, copy_runtime_auth
from experiment.shared.task_runtime import Mount, load_task_spec
from experiment.benchmarks.wildclaw_bench.scripts.run_wildclaw import reviewed_warmup, restore_captured_mock, grading_setup, check_gateway_evaluation
from experiment.benchmarks.wildclaw_bench.scripts.evaluate_wildclaw import freeze_solver_evidence
from experiment.benchmarks.wildclaw_bench.src.solver_audit import audit_solver
from .runtime import VanillaRuntime

ROOT=Path(__file__).resolve().parents[5]
VARIANT=ROOT/'experiment/benchmarks/wildclaw_bench/variants/vanilla_sol'
RUNS=ROOT/'experiment/benchmarks/wildclaw_bench/runs/vanilla_sol'
SCHEDULE=ROOT/'experiment/benchmarks/wildclaw_bench/manifests/vanilla_sol_schedule.json'
PROTOCOL=VARIANT/'prepared/formal/protocol.json'
VERSION='V1.1'
CONDITION='VANILLA_SOL'

def now():
    return datetime.now(timezone.utc).isoformat()

def load(path):
    return json.loads(path.read_text())

def task_for(row,plan):
    metadata=next(x for x in load(ROOT/'experiment/benchmarks/wildclaw_bench/manifests/task_manifest.json')['tasks'] if x['task_id']==row['task_id'])
    repo=ROOT/'experiment/benchmarks/wildclaw_bench/vendor/WildClawBench'; source=repo/metadata['source_path']
    if sha256(source)!=metadata['source_sha256']:
        raise ValueError('Frozen task source changed')
    task=load_task_spec(source,repo,workspace_root=ROOT/'experiment/benchmarks/wildclaw_bench/runtime/task_inputs',timeout_seconds=plan['timeout_seconds'])
    task=replace(task,skills_root=ROOT/'experiment/benchmarks/wildclaw_bench/runtime/skills')
    if plan['official_task_skills']=='removed':
        task=replace(task,skills=())
    return task,source,metadata

def prepare(*,official_task_skills='preserved',write=True):
    if official_task_skills not in {'preserved','removed'}:
        raise ValueError('Unknown official task skill policy')
    config=workflow_config(ROOT)
    if PROTOCOL.exists() and load(PROTOCOL).get('workflow_revision') != config['workflow_revision']:
        raise ValueError('Historical V1.1 protocol is read-only; prepare a fresh checkout without saved runs/protocols')
    old=load(ROOT/'experiment/benchmarks/wildclaw_bench/manifests/formal_protocol.json')
    tasks=[{'index':i,'task_id':r['task_id'],'run_id':f'v11-formal-{i:02d}'}
           for i,r in enumerate(r for r in old['schedule'] if r['condition']=='A0')]
    if len(tasks)!=24 or {r['task_id'] for r in tasks}!=set(load(ROOT/'experiment/benchmarks/wildclaw_bench/manifests/split.json')['test_task_ids']):
        raise ValueError('Expected the same sealed 24 tasks')
    schedule={'schema_version':1,'version':VERSION,'condition':CONDITION,'tasks':tasks}
    if SCHEDULE.exists() and load(SCHEDULE)!=schedule:
        raise ValueError('Schedule changed')
    rg=config['rg_mount']
    if sha256(ROOT/rg['source']) != rg['sha256']:
        raise ValueError('Standard ripgrep binary differs from runtime recipe')
    paths=[ROOT/'experiment/shared/codex_backend.py', ROOT/'experiment/shared/task_runtime.py', *sorted((ROOT/'experiment/benchmarks/wildclaw_bench/src').glob('*.py')), *sorted(VARIANT.glob('*.py')),
        ROOT/'experiment/benchmarks/wildclaw_bench/manifests/workflow_config.json', ROOT/'experiment/benchmarks/wildclaw_bench/manifests/formal_protocol.json',
        ROOT/'experiment/benchmarks/wildclaw_bench/manifests/split.json', ROOT/'experiment/benchmarks/wildclaw_bench/manifests/task_manifest.json',
        ROOT/'experiment/benchmarks/wildclaw_bench/scripts/run_wildclaw.py', ROOT/'experiment/benchmarks/wildclaw_bench/scripts/evaluate_wildclaw.py',
        ROOT/'experiment/benchmarks/wildclaw_bench/scripts/service_control.py', ROOT/'experiment/benchmarks/wildclaw_bench/scripts/launch_vanilla_sol_batch.py',
        ROOT/rg['source']]
    plan={'schema_version':2,'workflow_revision':config['workflow_revision'],'version':VERSION,'condition':CONDITION,
        **{k:config[k] for k in ('model','reasoning_effort','judge_model','judge_reasoning_effort',
          'cli_version','solver_image_id','timeout_seconds','grading_timeout_seconds')},
        'condition_instructions':old['conditions']['A0'],'mcp_servers':[],
        'trajectory_library':None,'distilled_skill_library':None,
        'official_task_skills':official_task_skills,'rg_mount':rg,
        'concurrency':1,'rollouts_per_task':1,'tasks':tasks,
        'grading_lifecycle':'Stop solver, freeze evidence, recreate an isolated grader and replay mock state.',
        'retry_policy':'One solver rollout per task. Preserve completed outputs; grading recovery never replaces a valid score.',
        'hashes':{str(p.relative_to(ROOT)):sha256(p) for p in sorted(paths)}}
    if plan['model']!='gpt-5.6-sol' or plan['reasoning_effort']!='medium':
        raise ValueError('Unexpected requested model')
    if PROTOCOL.exists() and load(PROTOCOL)!=plan:
        raise ValueError('Frozen V1.1 protocol changed')
    if write:
        if not PROTOCOL.exists():
            if any((RUNS/'formal').glob('*')):
                raise ValueError('Existing V1.1 runs require their original saved protocol')
            atomic_json(PROTOCOL,plan)
        if not SCHEDULE.exists(): atomic_json(SCHEDULE,schedule)
    elif not PROTOCOL.exists() or not SCHEDULE.exists():
        raise ValueError('Run prepare before launching')
    return plan

def verify_plan(plan):
    if plan.get('workflow_revision') != workflow_config(ROOT)['workflow_revision']:
        raise ValueError('Historical protocol is for evidence review; use a fresh checkout to execute')
    for name,digest in plan['hashes'].items():
        if sha256(ROOT/name)!=digest: raise ValueError('Frozen input changed: '+name)
    if plan['condition_instructions']!=load(ROOT/'experiment/benchmarks/wildclaw_bench/manifests/formal_protocol.json')['conditions']['A0']:
        raise ValueError('Vanilla instructions differ from original A0')

def audit_result(row,plan):
    d=RUNS/'formal'/row['run_id']; r=load(d/'result.json')
    if r.get('status')!='finished' or r.get('task_id')!=row['task_id'] or r.get('run_id')!=row['run_id']:
        raise ValueError('Incomplete or mismatched V1.1 result')
    e=r.get('execution',{}); g=r.get('evaluation',{}); score=g.get('overall_score')
    if e.get('status') not in ('completed','timeout') or not e.get('usage',{}).get('total_tokens'):
        raise ValueError('No valid solver execution')
    if g.get('status')!='graded' or isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not 0<=score<=1:
        raise ValueError('No valid score; preserve solver output for grading recovery')
    if sha256(d/'protocol.json')!=r['protocol_sha256'] or load(d/'protocol.json')!=plan:
        raise ValueError('Per-task frozen protocol differs')
    boundary=load(d/'native_environment.json')
    if sha256(d/'native_environment.json')!=r['native_environment_sha256']:
        raise ValueError('Boundary evidence changed')
    if not all(boundary[k].get('resources_absent') for k in ('solver','grader')):
        raise ValueError('Historical resource isolation not verified')
    task,_,_=task_for(row,plan)
    if boundary['solver']['official_task_skills']!=list(task.skills):
        raise ValueError('Official task skill policy mismatch')
    if (d/'solver_prompt.txt').read_text()!=build_prompt(task,condition_instructions=plan['condition_instructions']):
        raise ValueError('Saved prompt differs from original A0 task construction')
    audit_solver(d,plan)
    return {'run_id':row['run_id'],'task_id':row['task_id'],'status':'valid',
        'overall_score':score,'execution_status':e['status'],'result_sha256':sha256(d/'result.json')}

def run_one(row,plan):
    verify_plan(plan)
    d=RUNS/'formal'/row['run_id']
    if d.exists(): raise ValueError('Existing rollout must not be rerun')
    task,source,metadata=task_for(row,plan)
    repo=ROOT/'experiment/benchmarks/wildclaw_bench/vendor/WildClawBench'
    d.mkdir(parents=True,exist_ok=False); atomic_json(d/'protocol.json',plan)
    r={'task_id':row['task_id'],'run_id':row['run_id'],'version':VERSION,'condition':CONDITION,
       'phase':'formal','started_at':time.time(),'status':'starting','protocol_sha256':sha256(d/'protocol.json')}
    atomic_json(d/'run.json',r); rt=None
    try:
        with inference_client(ROOT,row['run_id']) as gateway:
            r['gateway_authorization_hash']=gateway.authorization_sha256
            rg=plan['rg_mount']
            config=make_runtime_config(ROOT,plan['solver_image_id'],support_mounts=(Mount(ROOT/rg['source'],'/usr/local/bin/rg',True),),
                grading_timeout_seconds=plan['grading_timeout_seconds'],warmup_override=reviewed_warmup(task),
                before_freeze_hook=capture_mock_state,after_restart_hook=restore_captured_mock,
                environment={'OPENROUTER_API_KEY':gateway.api_key,'OPENROUTER_BASE_URL':'http://api.hangxiao.internal/v1'})
            rt=VanillaRuntime(config,task,d,row['run_id'])
            backend=CodexBackend(CodexSettings(model=plan['model'],reasoning_effort=plan['reasoning_effort'],cli_version=plan['cli_version'],mcp_servers=()))
            backend.prepare(rt); r['mock_preflight']=preflight_mock_state(rt)
            copy_runtime_auth(rt); r['preflight']=backend.preflight(rt)
            r['status']='solving'; atomic_json(d/'run.json',r)
            execution=backend.run(rt,condition_instructions=plan['condition_instructions'],verify_login=False)
            r['execution']={**asdict(execution),'transcript_path':str(execution.transcript_path.relative_to(ROOT))}
            if execution.status not in ('completed','timeout') or not execution.usage.get('total_tokens'):
                raise ValueError('Invalid solver execution: do not turn infrastructure failure into a zero score')
            private_source=rt.codex_home/'auth.json'; private_target=ROOT/'experiment/benchmarks/wildclaw_bench/runtime/codex_auth/auth.json'
            candidate,current=load(private_source),load(private_target)
            if candidate.get('tokens',{}).get('account_id')==current.get('tokens',{}).get('account_id') and str(candidate.get('last_refresh',''))>str(current.get('last_refresh','')):
                shutil.copyfile(private_source,private_target); os.chmod(private_target,0o600)
            evidence=freeze_solver_evidence(rt,transcript_path=execution.transcript_path,
                transcript_loader=repo/'src/utils/transcript_loader.py',task_source=source,source_sha256=metadata['source_sha256'])
            r['solver_evidence']=str(evidence.relative_to(ROOT)); r['solver_audit']=audit_solver(d,plan,write=True)
            r['status']='grading'; atomic_json(d/'run.json',r)
            started=time.time()
            r['evaluation']=rt.grade(transcript_path=execution.transcript_path,
                transcript_loader=repo/'src/utils/transcript_loader.py',grading_setup=grading_setup(task))
            check_gateway_evaluation(r,d,started); r['status']='finished'
    except Exception as error:
        r['status']='infrastructure_error'; r['error']={'type':type(error).__name__,'message':str(error)}
        raise
    finally:
        if rt is not None:
            try: rt.cleanup()
            except Exception as error: r['cleanup_error']=type(error).__name__
        if (d/'native_environment.json').exists(): r['native_environment_sha256']=sha256(d/'native_environment.json')
        r['finished_at']=time.time(); atomic_json(d/'result.json',r); atomic_json(d/'run.json',r)
    return r


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','run'))
    parser.add_argument('--index',type=int)
    args=parser.parse_args()
    if args.command=='prepare':
        plan=prepare()
        print(json.dumps({'version':VERSION,'tasks':len(plan['tasks'])}))
    else:
        if args.index is None: parser.error('--index required')
        plan=load(PROTOCOL)
        run_one(plan['tasks'][args.index],plan)


if __name__=='__main__': main()
