#!/usr/bin/env python3
"""No-model dev-only check of original Social fixtures and mock state lifetime.

Uses the fixed construction-set Social task, original reviewed warmup, and a
synthetic grader that only verifies audit replay. No benchmark score is produced.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from experiment.shared.task_runtime import TaskRuntime, load_task_spec
from experiment.benchmarks.wildclaw_bench.src.runtime_support import make_runtime_config
from experiment.benchmarks.wildclaw_bench.src.mock_state import capture_mock_state, restore_mock_state, preflight_mock_state, canonical
from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import IMAGE, atomic_json
from experiment.benchmarks.wildclaw_bench.scripts.run_wildclaw import reviewed_warmup

TASK_ID = '03_Social_Interaction_task_4_chat_thread_consolidation'

PROBE = r'''
import json,os,signal,subprocess,sys,time,urllib.request,urllib.error,hashlib
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def request(path,body=None):
    req=urllib.request.Request('http://127.0.0.1:9110'+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json'},method='POST' if body is not None else 'GET')
    for attempt in range(10):
        try:
            with opener.open(req,timeout=3) as response: return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in (429,500,502,503,504) or attempt==9: raise
            time.sleep(0.2)
        except urllib.error.URLError:
            if attempt==9: raise
            time.sleep(0.5)
initial=request('/slack/audit')
assert initial['calls']==[] and initial['drafts']==[] and initial['sent_messages']==[]
messages=request('/slack/messages',{'days_back':7,'max_results':100})['messages']
assert messages
first=messages[0]['message_id']
request('/slack/messages/get',{'message_id':first})
request('/slack/drafts/save',{'to':'controller-lifecycle-probe','content':'Synthetic lifecycle validation draft','reply_to_message_id':first})
before=request('/slack/audit')
assert len(before['drafts'])==1 and not before['sent_messages']
# End an isolated dummy solver process group; never signal the warmup server.
child=subprocess.Popen([sys.executable,'-c',"import subprocess,time; subprocess.Popen(['sleep','120']); time.sleep(120)"],start_new_session=True)
time.sleep(0.2)
os.killpg(child.pid,signal.SIGTERM)
child.wait(timeout=5)
after=request('/slack/audit')
assert before==after
encoded=json.dumps(after,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
print(json.dumps({'initial_audit_empty':True,'survived_dummy_solver_group_exit':True,
 'message_ids':sorted(m['message_id'] for m in messages),'calls':len(after['calls']),
 'drafts':len(after['drafts']),'sent':len(after['sent_messages']),
 'audit_sha256':hashlib.sha256(encoded).hexdigest()}))
'''


def main():
    split=json.loads((ROOT/'experiment/benchmarks/wildclaw_bench/manifests/split.json').read_text())
    assert TASK_ID in split['dev_task_ids']
    metadata=next(t for t in json.loads((ROOT/'experiment/benchmarks/wildclaw_bench/manifests/task_manifest.json').read_text())['tasks'] if t['task_id']==TASK_ID)
    repo=ROOT/'experiment/benchmarks/wildclaw_bench/vendor/WildClawBench'
    original=load_task_spec(repo/metadata['source_path'],repo,workspace_root=ROOT/'experiment/benchmarks/wildclaw_bench/runtime/task_inputs')
    raw=ROOT/'experiment/benchmarks/wildclaw_bench/trajectory library/WildClawBench/task/hf_snapshot'/metadata['public_workspace_relpath']/'tmp/messages.json'
    staged=original.workspace_path/'tmp/messages.json'
    assert hashlib.sha256(raw.read_bytes()).hexdigest()==hashlib.sha256(staged.read_bytes()).hexdigest()
    run_id='social-lifecycle-'+str(time.time_ns())
    run_dir=ROOT/'experiment/benchmarks/wildclaw_bench/runs/smoke'/run_id
    inputs=ROOT/'experiment/benchmarks/wildclaw_bench/runtime/synthetic-social-lifecycle'/run_id
    inputs.mkdir(parents=True,exist_ok=False)
    shutil.copytree(original.exec_dir,inputs/'exec',symlinks=True)
    shutil.copytree(original.workspace_path/'tmp',inputs/'tmp',symlinks=True)
    (inputs/'skills').mkdir(); (inputs/'gt').mkdir()
    task=replace(original,workspace_path=inputs,skills_root=inputs/'skills',skills=(),prompt='Controller lifecycle validation; no solver model.',automated_checks='')
    warmup=reviewed_warmup(original)
    config=make_runtime_config(ROOT,IMAGE,warmup_override=warmup,
        before_freeze_hook=capture_mock_state,
        after_restart_hook=lambda runtime:restore_mock_state(runtime,runtime.freeze_hook_result))
    runtime=TaskRuntime(config,task,run_dir,run_id)
    report={'task_id':TASK_ID,'run_id':run_id,'solver_invocations':0,'grader_model_invocations':0,
            'warmup_changed':False,'warmup_sha256':hashlib.sha256(warmup.encode()).hexdigest(),
            'source_tmp_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'passed':False}
    try:
        runtime.prepare('')
        report['preflight']=preflight_mock_state(runtime)
        assert report['preflight']['status']=='mock_preflight_passed'
        assert not (runtime.staging/'mock_state.json').exists(), 'Preflight must not create final audit snapshot'
        report['preflight_has_no_final_snapshot']=True
        report['runtime_tmp_removed_after_warmup']=not (runtime.workspace/'tmp').exists()
        probe=json.loads(runtime.exec(['python3','-c',PROBE],timeout=90).stdout)
        fixture=json.loads(staged.read_text())
        messages=fixture['messages'] if isinstance(fixture,dict) else fixture
        expected_ids=sorted(message['message_id'] for message in messages)
        assert probe.pop('message_ids')==expected_ids, 'The server loaded a different scenario fixture'
        report.update(probe,correct_scenario_message_ids=True)
        runtime.freeze_agent()
        snapshot=json.loads(runtime.freeze_hook_result.read_text())
        assert snapshot['response_sha256']['slack']==probe['audit_sha256']
        report['authoritative_capture_hash_matches']=True
        expected=probe['audit_sha256']
        checks='''def grade(transcript,workspace_path):
    import hashlib,json,urllib.request
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:9110/slack/audit',timeout=5) as response:
        value=json.load(response)
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    assert hashlib.sha256(raw).hexdigest()==%r
    return {'overall_score':1.0}
''' % expected
        runtime.task=replace(runtime.task,automated_checks=checks)
        transcript=run_dir/'synthetic-transcript.jsonl'; transcript.write_text('')
        evaluation=runtime.grade(transcript_path=transcript,transcript_loader=repo/'src/utils/transcript_loader.py')
        report['synthetic_replay_verifier_passed']=evaluation['status']=='graded' and evaluation['overall_score']==1.0
        replay=json.loads((runtime.staging/'mock_state_replay.json').read_text())
        report['replay_status']=replay['status']
        report['passed']=bool(report['runtime_tmp_removed_after_warmup'] and report['synthetic_replay_verifier_passed'])
        if not report['passed']: raise RuntimeError('Mock lifecycle verification did not pass')
    except Exception as error:
        report.update(error_type=type(error).__name__,error_message=str(error))
        raise
    finally:
        runtime.cleanup()
        atomic_json(run_dir/'smoke_report.json',report)
        atomic_json(ROOT/'experiment/benchmarks/wildclaw_bench/manifests/social_lifecycle_smoke.json',report)
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    main()
