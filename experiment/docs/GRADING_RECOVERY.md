# Grader-only infrastructure recovery

`scripts/evaluate_wildclaw.py` freezes solver evidence before the first grading
attempt and can later recover a failed grader without invoking the solver.
Importing the module has no model/container side effects.

After `backend.run` has stopped the whole solver container, the controller calls:

```python
from experiment.scripts.evaluate_wildclaw import freeze_solver_evidence

freeze_solver_evidence(
    runtime,
    transcript_path=execution.transcript_path,
    transcript_loader=repo / "src/utils/transcript_loader.py",
    task_source=task_file,
    source_sha256=metadata["source_sha256"],
)
# Only then call runtime.grade(...).
```

The private `run_dir/solver-evidence/` contains independent copies of the entire
solver workspace, normalized transcript, official loader, frozen task/rubric,
verifier gold, and captured Social audit when required. `manifest.json` records
artifact, transcript, rubric and source lineage hashes. Workspace symlinks are
preserved without following their targets; regular files are copied, never
hardlinked. The controller's project-local `gt` root staging symlink is resolved
inside the project before copying. Verifier/controller files are excluded from
the solver artifact snapshot. Incomplete snapshots cannot be recovery sources.

To retry after repairing an identified grading dependency:

```bash
python3 -B experiment/scripts/evaluate_wildclaw.py \
  --run-dir experiment/runs/formal/FORMAL_RUN_ID \
  --reason 'Describe the verified grading infrastructure repair'
```

The default verifier image matches the source run. `--image` may specify a
reviewed dependency fix, recorded beside the original image. `--docker-host`
supports a dedicated daemon and `--grading-timeout` sets the grading wall clock.
No task warmup or solver is executed. The fresh, owned, read-only container gets
only copied evidence and approved proxy infrastructure. It has no model auth,
DCI service or standalone CLI mount. Gold and unchanged scoring code enter only
the grading phase. Verifier files use `docker exec` stdin copies because Docker
`cp` cannot write these read-only-root containers.

For Social tasks the original frozen audit is replayed. Only its container
ownership envelope is rebound to the new verifier; response bodies and their
hashes stay identical. Missing original in-memory state cannot be recreated.

Only explicit grading infrastructure failures are eligible: judge errors,
degraded fallback, grader errors/timeouts, invalid grader output, or restore
failures with a valid frozen audit. A valid `graded` result, including zero,
cannot be retried. The first successful recovered grade also blocks further
attempts, even if its outer controller record was interrupted. Unknown errors
and legacy runs without a pre-grading snapshot require separate review.

Each attempt has a unique `run_dir/grade-recovery/regrade-.../` with its own
`attempt.json`, runtime logs, raw score, evaluation and cleanup record. Failed
attempts remain intact. Original result/evaluation files and frozen solver
evidence are hashed before and after and are never overwritten. Proxy/judge
errors and incomplete requests are correlated by that attempt's registered random
client hash and grading interval before a score can be accepted. The recovery
opens `inference_client` for the attempt, passes its private key only to the
verifier, and removes the lease on exit.

`evaluation_recovery.json` points to the latest candidate with `adopted=false`.
This script does not change the original result or automatically choose scores.
The controller explicitly adopts the first valid recovery with:

```bash
python3 -B experiment/scripts/evaluate_wildclaw.py \
  --run-dir experiment/runs/formal/FORMAL_RUN_ID \
  --adopt --reason 'Reviewed infrastructure repair and unchanged solver evidence'
```

Adoption validates the candidate/attempt hashes, original record hash, complete
frozen evidence, zero solver invocations, unchanged raw official score, and a
finite graded value in [0, 1]. A legitimate zero is eligible. An original valid
grade or an earlier valid recovery blocks selecting a later score. The command
atomically creates `evaluation_adoption.json` and never overwrites the original
result/evaluation, candidate, attempt, or an existing adoption.

The read-only reporting API is:

```python
from experiment.scripts.evaluate_wildclaw import effective_evaluation

evaluation, recovery = effective_evaluation(project_root, run_dir, original_record)
```

Without an adoption it returns the original evaluation and `None`. With an
adoption it revalidates the complete provenance chain and returns the adopted
evaluation plus attempt/adoption hashes, explicit reason, recovery client hash
and grading time window. Pass the unmodified `result.json` record. A missing,
tampered or inconsistent adoption chain raises `ValueError`; readiness and
summaries must mark `evidence_error` and exclude that score instead of silently
falling back. The reader writes no files and makes no inference/service calls.

Synthetic tests cover independent snapshots, safe symlink handling, source
integrity, Social replay, recovery after a failed provider, retained attempts,
valid-zero protection, and absence of any solver call. Run:

```bash
python3 -B -m pytest -q experiment/tests/test_grading_recovery.py \
  --basetemp experiment/.cache/tmp/grading-recovery-tests \
  -o cache_dir=experiment/.cache/pytest
```
