"""Start the independently frozen V1.1 batch as an owned detached process."""
import fcntl
import json
from pathlib import Path
import subprocess
import uuid
from experiment.src.experiment_protocol import atomic_json,sha256
from experiment.scripts.service_control import process_identity,identity_matches
from experiment.variants.vanilla_sol import runner

def main():
    runner.RUNS.mkdir(parents=True,exist_ok=True)
    controllers=runner.RUNS/'controllers'; controllers.mkdir(exist_ok=True)
    with (runner.RUNS/'batch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state_path=runner.RUNS/'batch.json'
        if state_path.exists():
            state=runner.load(state_path); owner=state.get('controller',{})
            if identity_matches(owner,process_identity(owner.get('pid',0))):
                raise ValueError('V1.1 controller is already running')
            if state.get('status')=='completed': raise ValueError('All 24 results are already complete')
        plan=runner.load(runner.PROTOCOL); runner.verify_plan(plan)
    launch_id=runner.now().replace(':','').replace('-','')[:15]+'-'+uuid.uuid4().hex[:12]
    record_path=controllers/(launch_id+'.json'); log_path=controllers/(launch_id+'.log')
    command=[str(runner.ROOT/'experiment/.venv/bin/python'),'-u','-B','-m','experiment.variants.vanilla_sol.batch']
    record={'version':runner.VERSION,'status':'starting','created_at_utc':runner.now(),
        'protocol_sha256':sha256(runner.PROTOCOL),'launcher_sha256':sha256(Path(__file__)),
        'command':command,'log':str(log_path.relative_to(runner.ROOT))}
    with record_path.open('x') as stream: json.dump(record,stream,indent=2)
    with log_path.open('x') as log:
        process=subprocess.Popen(command,cwd=runner.ROOT,stdin=subprocess.DEVNULL,stdout=log,
            stderr=subprocess.STDOUT,start_new_session=True)
    record.update(status='running',process=process_identity(process.pid))
    atomic_json(record_path,record)
    print(json.dumps({'status':'launched','pid':process.pid,'record':str(record_path.relative_to(runner.ROOT))}))

if __name__=='__main__': main()
