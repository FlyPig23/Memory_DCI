"""Serial V1.1 controller; completed solver runs are never repeated."""
import fcntl
import os
import traceback
from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json,sha256
from experiment.benchmarks.wildclaw_bench.scripts.service_control import process_identity
from . import runner

def report():
    try:
        from experiment.benchmarks.wildclaw_bench.reports.vanilla_sol.summarize import main
        main()
    except Exception:
        (runner.RUNS/'report_error.log').write_text(traceback.format_exc())

def main():
    runner.RUNS.mkdir(parents=True,exist_ok=True)
    with (runner.RUNS/'batch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan=runner.load(runner.PROTOCOL); runner.verify_plan(plan)
        state={'schema_version':1,'version':runner.VERSION,'condition':runner.CONDITION,
            'status':'running','started_at_utc':runner.now(),'updated_at_utc':runner.now(),
            'controller':process_identity(os.getpid()),'protocol_sha256':sha256(runner.PROTOCOL),
            'schedule_sha256':sha256(runner.SCHEDULE),'completed':[],'active':None}
        path=runner.RUNS/'batch.json'; atomic_json(path,state)
        try:
            for row in plan['tasks']:
                d=runner.RUNS/'formal'/row['run_id']
                if not d.exists():
                    state['active']={**row,'started_at_utc':runner.now()}
                    state['updated_at_utc']=runner.now(); atomic_json(path,state); report()
                    runner.run_one(row,plan)
                audited=runner.audit_result(row,plan)
                state['completed'].append(audited); state['active']=None
                state['updated_at_utc']=runner.now(); atomic_json(path,state); report()
                print(__import__('json').dumps(audited),flush=True)
            runner.verify_plan(plan)
            state.update(status='completed',finished_at_utc=runner.now(),updated_at_utc=runner.now())
            atomic_json(path,state); report()
            from experiment.benchmarks.wildclaw_bench.reports.vanilla_sol.summarize import finalize
            finalize()
            print('{"status":"completed","runs":24}',flush=True)
        except BaseException as error:
            state.update(status='needs_review',error={'type':type(error).__name__,'message':str(error)},updated_at_utc=runner.now())
            atomic_json(path,state); report()
            raise

if __name__=='__main__': main()
