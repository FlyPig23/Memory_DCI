# Sonnet 5 on the V8 workflow: TB4 hard tasks, own-task failed trajectories + Opus failure analysis

- **Run dates:** smoke tests 2026-10-09; the counted run 2026-10-12 to 2026-10-20 (UTC). Three rollouts were reused from earlier runs under identical code, settings and inputs (Section 3).
- **What changed against V8:** only the solver. The V8 workflow is unchanged (same task packages, per-task query pools, Opus seeds, five MCP tools, start-up retrieval prompt, writable `/memory`, 180-second post-task review, official agent and verifier timeouts, separate verifier containers, Harbor 0.23.0, corpus `prepared/v8_corpus/final_20261008`).
- **Solver:** `claude-sonnet-5 / medium`, Claude Code CLI 2.1.292 (pinned native binary), driven by the new `engine/claude_engine.py` through `scripts/run_v8.py --engine claude`. The reviewer uses the same model and effort.
- **Run name:** `sonnet5_v8_x5` (5 rollouts per task). Machine-readable record: [`manifests/sonnet5_v8_x5_results.json`](../manifests/sonnet5_v8_x5_results.json).

Method boundaries are those of [PROTOCOL.md](PROTOCOL.md) and [V8_REPORT.md](V8_REPORT.md); only the differences are described here.

---

## 0. Summary

**Main result: Sonnet 5 on the V8 workflow passes 37/160 (23.1%) on 32 tasks × 5 rollouts.** The official Sonnet 5 entry (Claude Code, max effort) passes 11/160 (6.9%) on the same 32 tasks and task versions.

| Condition | Same 32 tasks · passes / trials | Tasks solved at least once |
|---|---:|---:|
| Official Sonnet 5 (Claude Code, max) | 11/160 (6.9%) | 5 |
| Official GPT-5.6 Sol (Codex, max) | 15/160 (9.4%) | 10 |
| V8, Sol medium (from V8_REPORT.md) | 60/160 (37.5%) | 16 |
| **V8, Sonnet 5 medium (this run)** | **37/160 (23.1%)** | **14** |
| Official same-version reference, all scored conditions (from V8_REPORT.md) | 1160/4628 (25.1%) | — |

1. **Per task against official Sonnet 5:** higher on 12 tasks, lower on 4, equal on 16 (all 16 are 0/5 on both sides). A two-sided sign test on the 16 tasks that differ gives p = 0.077. The tasks were selected on official Sol's low scores, not on Sonnet 5's, but official Sonnet 5's own failed trajectories are in the query pool (Section 6), so this p-value is descriptive only.
2. **Results are concentrated:** 3 tasks at 5/5, 1 at 4/5, 2 at 3/5, 4 at 2/5, 4 at 1/5, 18 at 0/5. 19 of the 37 passes come from four tasks.
3. **Where official Sonnet 5 does better:** sglang-qwen-burst (4/5 vs 2/5), distributed-dedup (2/5 vs 0/5), payments-pipeline-fix (3/5 vs 2/5), gsea-proteomics (1/5 vs 0/5).
4. **Conservative count:** two ctr-optimization rollouts were re-run after a 0 score had been seen (Section 4). Counting the replaced 0 for the rerun that later passed gives **36/160 (22.5%)**; no conclusion above changes.
5. **All 160 counted trials are valid** under the V8 checks, with model and effort verified as `claude-sonnet-5` / `medium` on every solve and review.

**The comparison is not like for like.** Official Sonnet 5 ran at max effort in the official Claude Code harness without the V8 material; this run is medium effort with the V8 tools, retrieval prompt and same-task failure material, and with web search, web fetch and sub-agents disabled (Section 1). 23.1% against 6.9% is an observed difference between whole conditions. As in V8, there is no same-runtime control without material.

**Cost:** the 160 counted solves took 99.2 hours of solver time, median 22.9 minutes (19.4 for passes, 23.1 for failures), 2.49 billion input tokens (mostly cache reads) and 14.7 million output tokens; the CLI's own list-price estimate is $766 for the counted trials including their reviews, excluding replaced attempts. The run used subscription quota, not API billing.

## 1. Setup

- **Tasks:** the 35 V8 tasks. Three could not run on our host (Section 2), leaving 32.
- **Material, prompts and runtime:** identical to V8. Every rollout starts from the frozen seed; rollouts of a task do not share updates.
- **Solver engine:** `engine/claude_engine.py` runs Claude Code headless (`claude -p --output-format stream-json --dangerously-skip-permissions --strict-mcp-config`) with the five V8 MCP tools from the same `serve_memory.py`. Disabled tools: `WebSearch`, `WebFetch`, `Task`, to match the Codex settings of V8 (no server-side web search, no sub-agents); shell access such as `curl` stays available as in V8. The CLI's own home is `CLAUDE_CONFIG_DIR` inside the trial, so the task image's `HOME` is untouched.
- **Credentials:** no credential enters a container or the server. The CLI talks to `127.0.0.1` inside the container; `engine/relay_exec.py` forwards that port to a mounted unix socket, behind which an operator-side relay adds the login. Three subscription accounts were used over the run (A: 110 trials, B: 46, C: 2; two trials switched from A to B mid-solve); the account of every trial is in the manifest.
- **Effort check:** the CLI does not echo `--effort`, so the request body was captured with a local listener (no model call): with `--effort medium` the CLI sends `output_config.effort = "medium"`; without the flag it sends `"high"`. The engine builds every solve and review command with `--effort medium`; the running processes were checked and carried it.
- **Audit:** `capture_session` writes `usage.json` and `model_audit.json` from the CLI's event stream; any other model producing output, a refusal fallback, or a missing init model fails the audit.

## 2. Host-specific changes and exclusions

- **Rootless Docker:** our host runs rootless Docker, where `docker cp` fails on containers with read-only bind mounts. Without a fix the solution never reached the verifier (the first smoke test scored 0 for that reason and was discarded). `harbor_runtime.install_copy_fallback` falls back to a tar stream over `docker exec`; the second smoke test confirmed that the verifier receives the solution.
- **Oracle preflight:** as in V8, a task is solved only if the official reference solution scores 1 on our host.
  - cad-model: reference solution fails (excluded in V8 too).
  - atrx-vep-crispr, formal-crypto: the task image cannot be pulled under rootless Docker (a file owner's UID/GID lies outside the subordinate ID range), so the oracle gives no verdict.
  - rs-archive-clone: the reference solution failed on our host (41 of 57 tests). The check ran while 14 solves were running, so load may be the cause; it was not repeated.
- These four are outside every count in this report.

## 3. Runs

- **Smoke tests (2026-10-09, payments-pipeline-fix):** smoke 1 was void (copy failure above); smoke 2 was valid with reward 0 and is counted as payments-pipeline-fix rollout 1.
- **Earlier one-rollout run (2026-10-09):** bun-sourcemap-leak and cargo-flight-dispatch rollout 1 finished valid (both 0) before the quota ran out; they are counted as rollout 1. Reuse was checked before the five-rollout run: same source hashes, settings, inputs and task records, and a valid completion.
- **Five-rollout run (2026-10-12 to 2026-10-20):** a paced driver launched solves while the account's 5-hour and weekly usage meters left room (read through the relay from response headers), with a per-running-solve reserve. Concurrency ranged from 3 to about 22.
- **Validity:** all 160 counted trials are valid; 0 counted trials were aborted by API errors.

## 4. Replaced attempts

Every replaced attempt was moved to `trials_void/` with its reason; nothing was deleted. Full log: `replaced_attempts_log` in the manifest.

| Reason | Attempts | Score seen before replacing? |
|---|---:|---|
| Operator pause (the relay host had to go offline) | 8 (ctr ×6, data-anonymization ×1, distributed-dedup ×1) | no, stopped before grading |
| Usage limit hit mid-solve (HTTP 429), solve aborted | 3 (ctr ×2, distributed-dedup ×1) | graded 0 on unfinished work, voided by rule |
| Connection dropped (relay host slept), solve aborted | 1 (ctr) | graded 0 on unfinished work, voided by rule |
| Started on the wrong account, stopped after ~3 min | 2 (ctr) | no |
| ctr r4: the model set a `CronCreate` timer and ended its turn; in headless mode the timer never fires, so the solve ended after ~40 min | 1 | **yes, 0** |
| ctr r5: the relay host slept ~25 min during this real-time task | 1 | **yes, 0** |

The last two were re-run after their score was known. The final ctr r4 scored 0 and the final ctr r5 scored 1; counting the earlier 0 for r5 gives the conservative 36/160. ctr-optimization therefore has 3/5 (or 2/5 conservatively). All five counted ctr solves ran the full 48-hour simulation (291–298 minutes each).

## 5. Results

### 5.1 Per task

Sorted by this run's k/5. "Official same-version reference" is copied from V8_REPORT.md §4.1 (all scored conditions on the same task version, including successes).

| Task | Sonnet 5 + V8 k/5 | Rollouts r1–r5 | Official Sonnet 5 k/5 | Diff | Official Sol k/5 | Sol + V8 k/5 | Official same-version reference |
|---|---:|:---:|---:|---:|---:|---:|---:|
| embedding-drift-monitor | 5/5 | 11111 | 0/5 | +5 | 1/5 | 5/5 | 89/145 |
| interleaved-vigenere | 5/5 | 11111 | 0/5 | +5 | 2/5 | 5/5 | 82/145 |
| roy-polymorph-cn | 5/5 | 11111 | 0/5 | +5 | 0/5 | 5/5 | 25/145 |
| freecad-spring-clip | 4/5 | 10111 | 0/5 | +4 | 0/5 | 5/5 | 62/145 |
| ctr-optimization | 3/5 | 01101 | 0/5 | +3 | 0/5 | 4/5 | 33/145 |
| wal-recovery-ordering | 3/5 | 11100 | 1/5 | +2 | 2/5 | 2/5 | 34/145 |
| foodstuff-beta-activity | 2/5 | 01100 | 0/5 | +2 | 0/5 | 0/5 | 1/145 |
| hof-topology-interpenetration | 2/5 | 00011 | 0/5 | +2 | 2/5 | 5/5 | 91/144 |
| payments-pipeline-fix | 2/5 | 01001 | 3/5 | −1 | 1/5 | 4/5 | 96/144 |
| sglang-qwen-burst | 2/5 | 00101 | 4/5 | −2 | 0/5 | 0/5 | 21/145 |
| freecad-platform-drawing | 1/5 | 00010 | 0/5 | +1 | 0/5 | 2/5 | 77/145 |
| lake-temp-glm | 1/5 | 00100 | 0/5 | +1 | 0/5 | 0/5 | 39/145 |
| protein-autointerp-disulfide | 1/5 | 01000 | 0/5 | +1 | 0/5 | 3/5 | 12/145 |
| vf2-speedup-networkx | 1/5 | 01000 | 0/5 | +1 | 1/5 | 5/5 | 42/145 |
| bun-sourcemap-leak | 0/5 | 00000 | 0/5 | 0 | 0/5 | 2/5 | 0/145 |
| cargo-flight-dispatch | 0/5 | 00000 | 0/5 | 0 | 0/5 | 0/5 | 0/144 |
| data-anonymization | 0/5 | 00000 | 0/5 | 0 | 0/5 | 0/5 | 1/144 |
| freecad-impeller | 0/5 | 00000 | 0/5 | 0 | 0/5 | 0/5 | 1/145 |
| freight-dispatch-shift | 0/5 | 00000 | 0/5 | 0 | 0/5 | 0/5 | 0/145 |
| glycan-ms2-elucidation | 0/5 | 00000 | 0/5 | 0 | 0/5 | 0/5 | 0/145 |
| ks-solver-cpp | 0/5 | 00000 | 0/5 | 0 | 1/5 | 0/5 | 23/141 |
| kv-live-surgery | 0/5 | 00000 | 0/5 | 0 | 1/5 | 3/5 | 47/145 |
| layout-config-recreation | 0/5 | 00000 | 0/5 | 0 | 0/5 | 5/5 | 17/145 |
| music-harmony | 0/5 | 00000 | 0/5 | 0 | 0/5 | 1/5 | 2/145 |
| mvcc-lsm-compaction | 0/5 | 00000 | 0/5 | 0 | 0/5 | 0/5 | 74/145 |
| ontology-kg-querying | 0/5 | 00000 | 0/5 | 0 | 0/5 | 0/5 | 0/145 |
| pretrain-shard-corruption | 0/5 | 00000 | 0/5 | 0 | 2/5 | 0/5 | 64/145 |
| production-planning | 0/5 | 00000 | 0/5 | 0 | 0/5 | 0/5 | 42/145 |
| satb-audio-transcription | 0/5 | 00000 | 0/5 | 0 | 0/5 | 4/5 | 62/145 |
| vllm-deepseek-streaming | 0/5 | 00000 | 0/5 | 0 | 2/5 | 0/5 | 8/145 |
| gsea-proteomics | 0/5 | 00000 | 1/5 | −1 | 0/5 | 0/5 | 44/145 |
| distributed-dedup | 0/5 | 00000 | 2/5 | −2 | 0/5 | 0/5 | 71/141 |
| **Total (32 tasks)** | **37/160** | | **11/160** | | **15/160** | **60/160** | **1160/4628** |

Not in the table: cad-model, atrx-vep-crispr, formal-crypto, rs-archive-clone (Section 2). One official Sonnet 5 trial on cargo-flight-dispatch has no score (`AlreadyExistsError`) and is counted as not passed, as the official denominator of 5 is kept.

Distribution of this run's k/5: 5/5 three tasks, 4/5 one, 3/5 two, 2/5 four, 1/5 four, 0/5 eighteen.

### 5.2 Against Sol on the same workflow

Sol + V8 (60/160) is ahead of Sonnet 5 + V8 (37/160) on these 32 tasks. Sonnet 5 + V8 is higher on foodstuff-beta-activity, lake-temp-glm, sglang-qwen-burst and wal-recovery-ordering; Sol + V8 is higher on 12 tasks and the two are equal on 16, most clearly layout-config-recreation (5/5 vs 0/5), satb-audio-transcription (4/5 vs 0/5), vf2-speedup-networkx (5/5 vs 1/5) and hof-topology-interpenetration (5/5 vs 2/5). The two runs differ in model, CLI, account, date and host, so this is a description, not an attribution.

## 6. Limitations

- **No material control:** effort (max vs medium), harness (official Claude Code vs this engine) and material differ at once. The effect of the failure material alone needs a same-runtime run without it.
- **Official Sonnet 5's own failures are in the pool:** the query pool holds each task's failed trajectories from all 29 official conditions, including the official Sonnet 5 trials in the baseline column. The baseline therefore also contributes input material; it is not a held-out comparison. As in V8, this is same-task replay, not cross-task transfer.
- **Selection:** the tasks were picked on official Sol's low scores; the same selection effects as in V8 apply.
- **Tool differences:** `WebSearch`, `WebFetch` and `Task` were disabled; official Sonnet 5 had the CLI's defaults (version unknown). `ScheduleWakeup` and `CronCreate` were left enabled; `CronCreate` can end a headless solve early (Section 4, Section 7).
- **Host:** rootless Docker and the copy fallback; four tasks could not run.
- **Time and accounts:** the run spans eight days, three subscription accounts and two operator pauses; service-side changes cannot be ruled out.
- **No semantic audit:** unlike V8 §4.2, no per-artifact review of the passing solutions was done; the official rewards are reported as they are.

## 7. Known issues found during the run (not changed in the frozen code)

- **API aborts recorded as valid:** when the CLI ends on an API error it reports `subtype: "success"` with `is_error: true`; the frozen health guard treats that as a finished solve, so the grader scores unfinished work as a valid 0. The driver therefore checks every completed solve and marks such trials with `invalid_api_error.json` (4 trials, all replaced; Section 4). A fix in `health_guard.py` is a follow-up and was not used here.
- **`CronCreate` in headless mode:** a timer set with `CronCreate` never fires in `claude -p`, so the session ends. Disabling `CronCreate` (and possibly `CronDelete`) is a follow-up for future runs.

## 8. Reproduction and data

Run with `scripts.run_v8 run --engine claude` after setting `V8_CLAUDE_BINARY` (native Claude Code 2.1.292) and `V8_CLAUDE_RELAY_SOCKET` (a unix socket that speaks the Anthropic API and adds the login); see the docstring of `engine/claude_engine.py`. The 21 source hashes recorded in the run's `protocol.json` match the files in this branch, and the manifest repeats them. Tests: 139 passed, 1 skipped (the upstream 134 plus 5 for the Claude engine).

The manifest holds, for every counted trial, the reward, validity, model and effort audit, solve time, tokens, list-price cost, result hash, account label, notes and replaced attempts. Raw trial directories (about 14 GB: transcripts, verifier output, artifacts and replaced attempts) are kept on the run host and are not published here.
