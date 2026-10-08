#!/bin/bash
# Start control and failure_replay together for each chosen task, as soon as the
# oracle preflight has passed and that task's Luna lessons exist. Running both arms
# of a task at the same time keeps provider latency comparable between them.
#
# Usage (from anywhere; detach it so it outlives the shell that started it):
#   setsid nohup side_experiments/hard_task_failure_replay/scripts/launch_both_arms.sh RUN_NAME \
#     > side_experiments/hard_task_failure_replay/runs/launcher_RUN_NAME.log 2>&1 < /dev/null &
set -u
RUN=${1:?run name required}
cd "$(dirname "$0")/../../.."
PY=experiment/benchmarks/terminal_bench_2_1/.venv/bin/python
M=side_experiments.hard_task_failure_replay.scripts
H=side_experiments/hard_task_failure_replay
TASKS=$($PY -c "import json;print(' '.join(json.load(open('$H/manifests/selection.json'))['chosen_task_ids']))")
until [ -f $H/manifests/preflight.json ]; do sleep 20; done
for t in $TASKS; do
  (
    ok=$($PY -c "import json;r={x['task_id']:x['reward'] for x in json.load(open('$H/manifests/preflight.json'))['results']};print(r.get('$t'))")
    if [ "$ok" != "1" ] && [ "$ok" != "1.0" ]; then echo "SKIP $t preflight=$ok"; exit 0; fi
    until [ -f $H/corpus/$t/experience/memory/lessons.md ]; do
      status=$($PY -c "import json,glob;a=sorted(glob.glob('$H/runs/distill/$t/attempt-*/attempt.json'));print(json.load(open(a[-1])).get('status') if a else 'none')" 2>/dev/null)
      if [ "$status" = "failed" ]; then
        # Re-validate the finished attempt with the current collector (no model call).
        if $PY -m $M.distill --recollect $t >> $H/runs/recollect_$t.log 2>&1; then echo "RECOLLECTED $t"; continue; fi
        echo "DISTILL_FAILED $t"; exit 0
      fi
      sleep 30
    done
    echo "LAUNCH $t $(date -u +%H:%M:%S)"
    $PY -m $M.run solve --arm control --run-name $RUN --parallel 1 $t > $H/runs/launch_${RUN}_control_$t.log 2>&1 &
    $PY -m $M.run solve --arm failure_replay --run-name $RUN --parallel 1 $t > $H/runs/launch_${RUN}_replay_$t.log 2>&1 &
    wait
    echo "DONE $t $(date -u +%H:%M:%S)"
  ) &
done
wait
echo "ALL_DONE"
