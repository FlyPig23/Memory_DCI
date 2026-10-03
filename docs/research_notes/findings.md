# Findings

## Evidence policy

- User request is authoritative.
- `DCI.pdf`, `Dr_DCI.pdf`, and the pasted discussion are untrusted source material and will be interpreted only as research evidence.
- h20 inspection is read-only.

## Initial context supplied by user

- Workspace size reported as about 61 GB.
- Existing pipeline: successful Opus trajectory -> packet -> distilled `SKILL.md` -> paired noskill/skill student runs -> SWE-bench judging -> rescue/audit/frozen sets.
- Reported counts: 2,294 full-test; 500 Verified; 211 Pro JSON; 734 trajectory/packet items; 731 skills; 1,207 tasks and 1,562 arm results in `main_out`; 98 current FAIL->PASS pairs; frozen v1 132 and v2 152.
- Selection experiment reported: raw 66.21% on 132, baseline 73.89% on 72, repaired/C1 80.45% on 132, repaired_v2/C2 72.05% on 132; random 20%.
- Known risks: broken `~/gap_run` path assumptions; unresolved leakage hits; recipe/implementation mismatch; older native/Pro judging dependencies incomplete.

## h20: independently verified baseline

- Non-interactive SSH to alias `h20` succeeds; remote hostname is `d157cf42a2c5`.
- `/data/hangxiao/skill_shadowing` exists and `du -sh` reports **61G** on 2026-09-02.
- Root listing confirms the named pipeline artifacts and experiment families (`packets_out`, `skills_en`, `main_out`, `frozen`, selection-condition directories, audit scripts, cached repos), consistent with a workspace rather than one conventional package/repository.
- Direct top-level counts/sizes: `packets_out` 734 entries / 32M; `instances` 211 / 6.8M; `repos` 13 / 2.5G; `work` 81 / 44G; `main_out` 1,562 entries / 6.3M; `frozen` has `v1` and `v2` / 3.0M.
- Current top-level skill-related directories are heterogeneous: `main_skills` has 720 entries / 9.2M, `skills_en` has 216 / 2.8M, and additional `skills_out`/`sib_skills` directories exist. Therefore “731 distilled skills” cannot be inferred from one directory count and needs provenance-aware counting.
- `main_out` filenames encode `<instance_id>__{skill|noskill}.json`, allowing arm pairing and unique-task counts without parsing full logs.
- `frozen/v1/manifest.json` declares `n=132` with 132 items; `frozen/v2/manifest.json` declares `n=152` with 152 items. `traj_h20` has 734 `.traj` files totaling 186M; `skills_out` has 731 task directories totaling 17M. This directly resolves the earlier skill-count ambiguity: 731 refers to the original `skills_out` lineage, while `main_skills` is a later 720-item filtered/modified lineage.
- A sampled `.traj` is a JSON list of role messages (`system`, `user`, `assistant`, `tool`); assistant messages hold `thinking`/`text` blocks and `additional_kwargs.tool_calls`, tool messages hold textual observations. The sample has 143 messages and 71 assistant turns. No task/model/timestamp/outcome provenance is embedded at the top level; such metadata must be joined from external manifests/files.
- `distill_h20.py` truncates per-step thought/action/observation to 600/800/350 characters when building packets and does **not** explicitly append a gold/final patch, despite `DISTILL_RECIPE.md` describing the packet as including one. This confirms the user-reported documentation/implementation mismatch.
- `distill_h20.py` hardcodes `H = ~/gap_run`. On h20, `/data/hangxiao/gap_run` does not exist, while `/data/hangxiao/skill_shadowing/swebench_all.parquet` does. The runner family is currently non-portable until path resolution is centralized.
- `main_out` is exactly 1,562 compact verdict JSONs covering 1,207 unique tasks: 1,001 noskill arms and 561 skill arms. Only 355 tasks are paired; 852 have one arm.
- Among the 355 paired tasks, the observed matrix is 257 `FAIL→FAIL` and 98 `FAIL→PASS`; all pairs use the same recorded student (348 GPT pairs, 7 Haiku pairs). Thus the current paired rescue rate is 98/355 = **27.6% conditional on a noskill failure**.
- There are no paired baseline-PASS cases in `main_out`, so this dataset cannot estimate PASS→FAIL harm or population-average treatment effect. It is a rescue-conditioned diagnostic, not a held-out benchmark comparison.
- `swebench_full_test.parquet` has 2,294 unique instances and includes `created_at`; `verified_test.parquet` has 500 unique instances and includes `difficulty`. The trimmed `swebench_all.parquet` also has 2,294 rows but omits `created_at`, `hints_text`, and `environment_setup_commit`.
- The 734 trajectory IDs all join to full-test metadata; 235 are in SWE-bench Verified. `main_out` covers 398 Verified tasks, leaving 102 Verified tasks untouched **by `main_out` only** (a stricter global “untouched” definition may be smaller).
- Temporal candidate counts directly match part of the pasted proposal: of the 734 trajectories, 457 predate 2021-01-01 and 548 predate 2022-01-01. However, time is not outcome verification; these are only raw candidate book sizes.
- Full-test temporal distribution is 1,221 before 2021 and 1,616 before 2022; Verified is 254 before 2021 and 340 before 2022. A temporal split must be applied at task/near-duplicate cluster level and reconciled with what prior runs already touched.
- `resolved_cache.json` contains 1,280 boolean outcomes overall (193 true, 1,087 false). Only 124/734 trajectory tasks appear in it: **105 true, 19 false, 610 uncached**. Thus neither the existence of a downloaded teacher trajectory nor the upstream submission label is a locally complete, uniform success proof; a frozen outcome/provenance manifest is prerequisite to the new book.
- `leak_prescreen.json` currently has 85 entries, all with at least one ordinary match and 6 with a nonempty high-risk list. The fuller leakage note says 139 rescues were prescreened, 87 hit, high-risk cases were manually inspected, and **75 ordinary-identifier hits remain unclassified**. It also records that 720 skills carry a flawed self-audit section that may itself reveal identifiers.
- `frozen/v1`/`v2` manifests preserve evidence paths, student, arm verdicts, skill MD5/size, and log sizes, but do not by themselves prove train/test independence or absence of semantic/near-duplicate leakage.
- Selector result directories contain 10 deterministic runs per task (two prompt wordings × five permutations). Recomputed aggregates match the user summary exactly: raw 874/1,320 = 66.21% on 132 tasks; baseline 532/720 = 73.89% on 72; repaired/C1 1,062/1,320 = 80.45% on 132; repaired_v2/C2 951/1,320 = 72.05% on 132. Random choice among five candidates is 20%.
- Because baseline covers only 72 tasks while the others cover 132, its aggregate cannot be used as an unpaired causal comparison. A common-task slice and paired per-task analysis are required.
- The current `run_task_remote.sh` runs Codex with `danger-full-access` inside a copied worktree located under the same broad `$HOME/gap_run` tree that contains metadata, gold/test patches, previous outputs, skills, logs, and other workspaces. It asks the model not to touch `.git`/tests but provides no OS-level read isolation from evaluator-only material.
- `judge_verified.py` applies the gold `test_patch` only after the solver run and checks the union of `FAIL_TO_PASS` and `PASS_TO_PASS`, which is the correct high-level separation of solving and judging; however, both solver and judge share the same broad filesystem, so instruction-level separation is insufficient for a leakage-sensitive DCI study.
- The current runner writes a task-specific `TEACHER_SKILL.md` into the target repository for the skill arm, demonstrating the old same-task treatment and making it unsuitable as-is for a train-only reference-book condition.
- `verify_rescues_h20.py` performs useful evidence-integrity checks (quota noise, short/failed logs, same student, log-verdict freshness, skill presence/format, leakage exclusions) and should be reused as a post-run audit module, but its rescue-only selection does not replace randomized/complete held-out evaluation.
- h20 currently has no executable `rg` found in the standard/local paths checked. `grep -rlF` over the full 186M/734-file raw trajectory directory completed in roughly 0.016-0.078 seconds for three cached test queries, so corpus size is not presently the performance blocker; installing/pinning ripgrep is still needed for the intended compositional interface.
- Broad anchors are poorly selective in this corpus: `ValueError` occurs in 605/734 files, `pytest` in 714/734, and the teacher edit-tool name in all 734. The interface must encourage conjunctive/local queries, repo filters, and bounded output rather than one unqualified keyword.
- A conservative filename/path scan over all major legacy artifact families marks 1,298/2,294 full-test tasks and 413/500 Verified tasks as previously touched, leaving **996 full-test** and **87 Verified** without a detected legacy artifact. This is stricter than `main_out`-only (102 untouched Verified) and explains the pasted discussion's “about 90” statement.
- The 87 strictly artifact-untouched Verified tasks span 8 repos but are skewed: Django 40, SymPy 15, Sphinx 13, Matplotlib 7, Pylint 5, scikit-learn 3, xarray 3, pytest 1. Difficulty labels: 50 at 15m-1h, 18 <15m, 17 at 1-4h, 2 >4h.
- Of those 87, only 28 are dated 2022+, so simultaneously demanding strict legacy-untouched, Verified-only, and a 2022 temporal holdout is likely underpowered. Across full-test, the same conservative scan leaves 253 untouched 2022+ tasks across 10 repos.
- Among the 105 cached-positive trajectory tasks, only 83 predate 2022 (73 predate 2021). Outcome revalidation could expand the pre-2022 candidate book toward the raw 548, but using 548 as “successful trajectories” today would overstate evidence.
- Practical split recommendation: use touched tasks only for development/pilot; the 87 no-major-artifact Verified and 253 no-major-artifact post-2022 full-test candidates can support a **qualified internal holdout** only if prior boolean-label exposure is disclosed. A genuinely pristine confirmatory set requires a new/future task universe.
- “Untouched” has materially different levels and must not be reported as one number:
  - trajectory-or-student-run exposure: `traj_h20 ∪ main_out` covers 1,226 full-test tasks (715 overlap), leaving **1,068 full-test / 101 Verified** without content/run exposure;
  - conservative major-artifact filename/path exposure leaves **996 full-test / 87 Verified** without a detected artifact;
  - adding the 1,280-task boolean `resolved_cache` outcome-label exposure makes the union exactly **all 2,294 tasks**, leaving **zero strictly pristine tasks** in the current full-test/Verified universe.
- Consequently, a “clean final test” claim is not defensible on the existing universe if even automated outcome-label access counts as contamination. The report should offer two honest choices: (a) preregister/seal a no-content/run subset and disclose prior boolean-label exposure, or (b) obtain a new/future benchmark holdout for a genuinely pristine confirmatory result.
- The 734 `.traj` files contain no embedded instance ID, repo, timestamp, created_at, model, teacher, student, or outcome field; instance IDs come only from filenames, issue date from parquet, and teacher source only from a hardcoded S3 path. Download mtimes are not trajectory generation times. A new per-episode provenance manifest is mandatory.
- Agent audit found all 734 trajectories parse as valid JSON. It also found heuristic overlap with gold/test-patch-derived strings in 730/734 raw trajectories, 729/734 packets, and 88/731 skills. These are contamination-screening flags, not automatic proof of answer leakage; exact matching rules and false-positive modes must accompany any reported rate.
- The local `instances/*.json` count of 211 is only a subset. The project also contains a 731-row/11-repo SWE-bench Pro helper JSONL, so “211 Pro tasks” is not a complete Pro-universe count.

### DCI paper limitations that matter here

- In DCI, “trajectory” denotes the current search agent's observation/action sequence, not a corpus of historical repair trajectories. Mapping DCI to ExperienceBook is a new hypothesis, not a result already established by the paper.
- DCI's internal process statistics show its advantage can coexist with lower mean/all gold-document coverage; for ExperienceBook this motivates measuring three stages separately: reaching a useful episode, localizing a useful span, and converting it into a passing current patch.
- The DCI manuscript contains a few reported-number inconsistencies (e.g., Figure 4 counts vs. surrounding 69/80 percentages; Figure 5's 400K accuracy vs. prose). The report should cite the clean Table 4 mechanism comparison and avoid overinterpreting ambiguous scale numbers.
- Open Bash improves DCI but increases cost and attack surface. Historical trajectories must be treated as inert, untrusted text; the agent may quote/adapt ideas but must never replay corpus commands automatically.

## Research findings

### Prior agent discussion (pasted text; hypotheses to verify)

- Recommends reframing the project from same-task offline skill distillation to **Direct Trajectory Interaction (DTI)** over a frozen, train-only **ExperienceBook**.
- Central claimed mechanism: an agent repeatedly uses `find`/`rg`/`read` during repair, updates queries from live repository/test observations, localizes short procedural spans, compares episodes, and composes a cited task-local memo.
- Important representation policy: “raw” should mean no semantic abstraction, not no sanitation. Proposed primary corpus is chronological action-observation traces without final diff, hidden/evaluator material, credentials, or answer-reconstructing edit payloads.
- Proposed factorial core: `{distilled skill, patchless trajectory} × {one-shot static access, DCI-style interactive access}`, with Repo-only and token-matched controls.
- Strong leakage proposal: split/freeze tasks before any downstream processing; keep `/workspace`, read-only `/book`, and evaluator-only judging namespaces isolated; add exact/near-duplicate, patch/AST/test-name, ancestry, and canary audits.
- Prior discussion suggests temporal, component-disjoint, and repo-held-out evaluation; reported candidate temporal counts and outcome coverage require server verification before reuse.
- It correctly identifies a key falsifier: if patchless interactive trajectories do not beat one-shot retrieval, the DCI-interface claim fails; if only full trajectories work, the contribution is historical patch reuse rather than procedural transfer.
- It asserts quantitative DCI results and scalability observations that must be checked against the PDFs before inclusion.

### Reporting stance

- The prior discussion is high-quality design input but is not independent evidence.
- The final report should preserve its strongest causal decomposition while pruning premature naming/positioning claims and any unverified dataset counts.

### Final independent-review corrections

- Outcome-label lookup was separated from direct solver/content contamination: it causes selection bias only if its values affected task selection, tuning, or stopping. The report now requires a decision-lineage audit instead of automatically invalidating every internal holdout.
- The minimum causal experiment now includes Repo-only, candidate-matched one-shot, and candidate-matched interactive arms; dynamic candidate expansion is a separate factor.
- DR-DTI now defines distinct event-window index, episode materialization, and episode-preview units, with token/byte/episode budgets.
- Patchless sanitization now covers every message channel and includes a blinded patch-reconstruction validity audit.
- The temporal split now keys on repair-publication/merge time rather than issue creation alone.
- h20 re-audit found one evaluator-data mismatch: `astropy__astropy-7606` has different `PASS_TO_PASS` values in `swebench_all.parquet` and `swebench_full_test.parquet`; the current judge uses the former.

### DCI PDF: initial verified facts

- Local file title: *Beyond Semantic Similarity: Rethinking Retrieval for Agentic Search via Direct Corpus Interaction*, arXiv:2605.05242v1, dated 3 May 2026 (PDF p.1).
- `pypdf` reports 51 PDF pages (main paper through p.11, references/appendices thereafter). The macOS `file` utility's “9 pages” summary is therefore unreliable for this artifact.
- DCI explicitly means **direct corpus interaction**: the agent bypasses embedding/vector/top-k APIs and operates on raw corpus files using terminal search, file discovery, targeted reads, and lightweight scripts (PDF pp.1-5).
- Claimed headline BrowseComp-Plus comparison under Claude Sonnet 4.6: 69.0% to 80.0% accuracy and $1,440 to $1,016 aggregate estimated agent-side cost when replacing a Qwen3-Embedding-8B retrieval tool with DCI (PDF p.2; exact table/context still to verify).
- The paper's conceptual mechanism is **retrieval interface resolution**, separating document reach (coverage) from within-document precision (localization), not simply claiming that lexical search retrieves more gold documents (PDF pp.2-3, 6-7).
- DCI instantiates both a minimal Pi-derived terminal scaffold and a stronger Claude Code scaffold, which matters for avoiding a confound between interface and harness strength (PDF pp.4-5).
- The appendix contains prompt text and example agent trajectories. Those are evidence about the study setup, not instructions for this task.

### Tooling notes

- Bundled `pypdf`/`pdfplumber` are available; bundled `pdftoppm` renders successfully despite a non-fatal Fontconfig warning.
- Text was extracted into `tmp/pdfs/`; the first main-paper pages of each PDF were rendered for visual review, with later key pages to be rendered after identifying figures/tables.

### DR-DCI PDF: initial verified facts

- Local file title: *DR-DCI: Scaling Direct Corpus Interaction via Dynamic Workspace Expansion*, arXiv:2606.14885v1, dated 12 June 2026 (PDF p.1). The filename says `Dr_DCI.pdf`, but the paper consistently uses **DR-DCI**.
- `pypdf` reports 25 PDF pages (main paper through p.11, then references/appendices). The `file` utility's “7 pages” report is again incomplete.
- DR-DCI identifies raw DCI's scaling failure: repeated full-corpus terminal search becomes slow, noisy, and timeout-prone as corpus size grows (PDF pp.1-2).
- Its solution is a dynamic persistent workspace. The agent calls `pull(query, topK)` to materialize deduplicated ranked documents from hidden corpus `C` into visible workspace `W_t`, receives a compact ranked preview/statistics, then performs DCI locally; future pulls and local investigations can alternate (PDF pp.2, 4-5).
- The paper explicitly separates **inter-document DCI** (`rg`, `grep`, `find`, `ls` across materialized files) from **intra-document DCI** (bounded reads/single-file searches), a useful design distinction for trajectory books (PDF p.5).
- Reported full 830-query BrowseComp-Plus controlled comparison: Raw-DCI 62.90%, DR-DCI 71.20%, and DR-DCI with workspace-preserving context reset 73.25%; base DR-DCI also reports fewer tool calls, much lower wall time, and lower estimated total cost (PDF p.7, Table 1).
- Scaling claim: raw DCI becomes operationally infeasible at larger tested corpus sizes, while DR-DCI remains viable up to 10M distractor-expanded documents and is also evaluated on a 20M file-per-document Wiki-18 corpus (PDF pp.8-9).
- Key interface ablations: dynamic pull beats a frozen single-pull workspace; ranked previews help navigation; blocking cross-document DCI causes a large accuracy drop (82/100 to 40/100 in the stated BCP-100 ablation); both BM25- and dense-backed pulls work, with dense strongest in this setup (PDF pp.9-10).
- Context reset preserves the accumulated workspace while discarding unreliable reasoning history and reruns a DCI agent on that evidence; this is a selective recovery extension, not part of the core interface claim (PDF pp.5, 7).
- Naming caveat: the paper never formally expands the letters “DR”; report it as **DR-DCI, a retriever-steered DCI framework implemented via Dynamic Workspace Expansion**, not as an invented acronym expansion.
- The workspace is monotonic in the current method: it deduplicates and accumulates documents but does not implement semantic compression, eviction, or forgetting. Choosing what to pull/keep/compress/discard is future work (PDF pp.5, 11).
- Terminal-aware materialization uses hard links to immutable corpus items, a root-flat deduplicated layout, shell-safe filenames, bounded search/read outputs, and continuation hints (PDF pp.5, 18-20).
- Context reset was triggered for 49 conservatively defined low-confidence/abstention cases; it rescued 17, taking 591/830 to 608/830, but authors warn indiscriminate retry can damage correct answers (PDF p.15).
- DR-DCI is not uniformly dominant: its appendix ranking average (54.2 NDCG@10) is below DCI-Agent-Lite (56.7), reinforcing that it is a scaling/interface trade-off, not a universal replacement (PDF pp.14-15).
- The corpus-scaling experiment uses random FineWeb distractors, which are substantially easier than same-repo/same-API/near-duplicate bug trajectories; high-scale Raw-DCI points beyond the feasible measured range are extrapolations (PDF pp.7-8, 17).

### Visual PDF checks

- DCI PDF p.10 Table 4 visually confirms the cited 100-query trajectory analysis: Qwen3-Embedding-8B has mean coverage 56.7 and localization 21.7 with accuracy 45; DCI-Agent-Lite (L4) has mean coverage 28.0 and localization 48.4 with accuracy 73. The same page's scale chart/text states degradation from 100K to 200K and especially 400K documents, including accuracy down to 37.5% at 400K and rising tool use/latency/cost.
- DR-DCI PDF p.4 Figure 1 visually confirms the closed loop `pull -> materialized workspace -> local DCI -> new clues/query -> pull` and persistent workspace semantics.
- DR-DCI PDF p.10 visually confirms the inter-document ablation (82/100 to 40/100) and retriever comparison: Raw-DCI 67/100, DR-DCI+BM25 80/100, DR-DCI+dense 82/100 on BCP-100; the paper notes workspace path/rank organization can itself affect search brittleness.

### Paper-to-project design implications

- DCI's runtime management is part of the method's practical envelope: per-tool output truncation, old-tool-result compaction, and optional summarization are separate from corpus access; more compression is not monotonically better (DCI PDF pp.5-6, 11). An ExperienceBook runner must therefore budget both **book exposure** and **solver context** and log each independently.
- DCI's restricted `read + grep` ablation still beats the matched dense-retrieval baseline on its BCP-100 sample (61 vs. 45), while open bash reaches 73 at higher cost (DCI PDF pp.10-11). For this project, a narrow audited search API is scientifically defensible and safer than unrestricted shell access.
- Raw DCI's main weakness is search breadth: at 200K/400K documents costs and failure rise sharply (DCI PDF p.10). With only hundreds of trajectory episodes, direct `rg` is a plausible pilot, but DR-DCI supplies the upgrade architecture if the book grows or if each episode is split into many files/spans.
- DR-DCI demonstrates that the correct scalable hybrid is not “retrieve once and inject top-k.” Retrieval should be an agent-callable, repeated **workspace expansion** action; the retrieved set persists and remains locally searchable (DR-DCI PDF pp.4-5, 9).
- For trajectories, the analog of inter-document DCI is comparing multiple historical episodes; the analog of intra-document DCI is following local action-observation spans within one episode. Both should be separately logged and ablated.
- DR-DCI's main BCP comparison controls backbone/harness between Raw-DCI and DR-DCI, but its external-system comparisons and 20M Wiki-18 baselines are explicitly not fully controlled (DR-DCI PDF pp.7-9). The report should avoid treating all headline numbers as causal evidence.
- Materialization details are causal candidates, not implementation trivia: root-flat, deduplicated, shell-safe files with bounded observations and rank supplied in tool feedback were more reliable than rank/path prefixes (DR-DCI PDF pp.5, 10). ExperienceBook should use opaque stable episode filenames plus a manifest, not semantically revealing or rank-encoded paths.

### Verified related-work boundary (current arXiv metadata on 2026-09-02)

- **SkillRouter** (arXiv:2603.22455, current v5): ~80K candidate skills, body-aware retrieve-and-rerank, 74.0% Hit@1; its scientific target is pre-execution skill routing. This project must evaluate repair success and repeated execution-time trajectory interaction, not merely routing accuracy. Source: <https://arxiv.org/abs/2603.22455>.
- **STAIR** (arXiv:2607.29658): converts historical repair trajectories into multi-level reusable plans, retrieves/adapts plan nodes before prompting the repair agent, and reports raw unabstracted trajectories transfer substantially worse. This is the nearest direct competitor and creates the strongest testable counter-hypothesis: can raw/patchless trajectories work when accessed iteratively at span resolution? Source: <https://arxiv.org/abs/2607.29658>.
- **Search2Skill** (arXiv:2608.05245): searches external sources and uses rubric-based RL to distill persistent structured skills; its abstract attributes gains to abstraction rather than raw evidence. The present project differs only if it uses a fixed train-only experience corpus and makes test-time interaction, rather than external search or persistent skill learning, the principal intervention. Source: <https://arxiv.org/abs/2608.05245>.
- These papers are very recent preprints. They establish novelty pressure and baselines, not settled scientific facts; the report should explicitly phrase claims as reported results.
- Official DCI-Agent-Lite and DR-DCI repositories exist and expose runnable harnesses. They are useful references for tool/runtime design but should not be imported wholesale before the project's leakage boundary is rebuilt: <https://github.com/DCI-Agent/DCI-Agent-Lite>, <https://github.com/EigenTom/DR-DCI>.

## Filesystem-memory evidence added for option 2

- Local paper: *Filesystem-Based Memory for LLM Agents: Organization, Evolution, and Sustainability*, arXiv:2607.26637v1.
- Its results do not justify a universal “search always matters more than build” law. Within the paper's settings, changing the searcher under a fixed builder usually moves performance more than changing the builder under a fixed searcher, so this project must measure search/localization and representation separately.
- On the paper's 140-task ALFWorld setting, the strong executor is competitive with raw memory (reported 87.1 versus 82.1 for generated skills; difference not statistically decisive in the paper), whereas the weak executor benefits more from curated skills plus query-time guidance (reported 76.4 versus 66.4 for raw, with the paper reporting a significant net benefit). Invalid-action degradation from strong to weak is also larger for raw than generated-skill memory.
- Therefore the new plan crosses raw patchless evidence, curated/general skills, and dual-layer memory with both weak and strong executors. Raw-versus-curated is an empirical interaction, not a representation choice to settle in advance.

## Multi-benchmark verification and role assignment

- Primary sources were checked for AppWorld, tau3-bench, TheAgentCompany, HAL, BrowseComp-Plus, SWE-bench-Live, Terminal-Bench 2.0, SWE-bench Pro, WildClawBench, General AgentBench, WebLINX, WebArena/VisualWebArena, and OSWorld/OSWorld 2.0.
- AppWorld is the best first cross-domain task-B experiment because it has a natural train/dev/test structure and challenge split; only train trajectories may enter the ExperienceBook.
- tau3-bench and BrowseComp-Plus are task-A mechanism probes over fixed knowledge/document corpora; they cannot be presented as historical-trajectory transfer.
- SWE-bench-Live is the preferred temporal coding confirmation. WildClawBench is useful for ingestion/schema scale validation. Terminal-Bench 2.0, SWE-bench Pro, TheAgentCompany, WebLINX, WebArena/VisualWebArena, OSWorld, HAL, and General AgentBench remain gated expansions rather than simultaneous launch targets.
- A source-code license never automatically covers trajectories, screenshots, third-party webpages, or model outputs; every imported revision needs a separate asset-policy manifest.

## h20 addendum: general and confusion skills

- Read-only counts show `sib_skills` has 35 files and `sib_out` has 30 result files.
- The user's recollection is that roughly 4–5 related task skills were sometimes fused into a category-level `general skill`, but the current server snapshot does not expose a construction script or provenance manifest sufficient to independently reconstruct that claim.
- These files are retained as a possible curated/compositional Layer-2 asset, contingent on provenance, split, and leakage audits. The report labels this as user historical recollection rather than an h20-verified construction fact.
- Artificial confusion assets occupy 61 task directories and 241 files. They remain archived but are explicitly outside the new main experiment and receive no deeper analysis.

## Final option-2 experimental design

- Three layers: immutable sanitized patchless evidence; rebuildable skills/cards/index/warnings; task-local cited memo plus optional persistent DR-DCI workspace.
- Six primary arms (repo-only, static skill, one-shot raw packet, interactive raw DCI, cited-memo/curated serving, and dual-layer/DR serving) plus A2-CM, which matches A3's candidate evidence and compute/exposure ceiling without interactive search.
- Two executor strengths are mandatory because representation utility is capability-dependent.
- Phase 1.5 uses AppWorld-train scenario-grouped nested pilots to freeze K, task×seed schedules, compute ceilings, and projected precision before dev/test access. K controls Monte Carlo variability, not scenario-sampling uncertainty.
- K=1 permits paired binary methods such as McNemar; K>1 uses task-level seed means with task/scenario clustered or repeated-measures inference.
- Dev-only oracle evidence is built from train evidence before solver outcomes are seen. Quantitative gates govern whether test is ever unsealed; test results cannot be used to retune the book, searcher, arms, or schedule.
- The final paper story is deliberately minimal: h20 as an internal-validity case study, AppWorld as the cross-domain main result, tau3 as the mechanism check, SWE-bench-Live as temporal coding confirmation, and WildClaw as ingestion evidence.

## 2026-09-03 meeting whiteboard and WikiSkill revision

### Whiteboard interpretation

- The photographed design has two state regions. `Task-old` contains a folder of immutable good and bad trajectories plus writable `Skill-1.md`, `Skill-2.md`, … files. `Task-new` is solved by an agent loop `Question -> Reason -> Search -> Read -> Reason -> Search -> … -> Answer` using DCI-style `grep/find` access; arrows indicate that handbook files may be read and written while raw trajectories remain fixed.
- The central unresolved variable is not whether the system can physically write Markdown, but **when a write becomes shared state and what evidence is allowed to authorize it**. Letting task `t` update a shared handbook before scoring later test tasks turns the evaluation into order-dependent online continual learning and allows test-information propagation.
- “Pre-classification” has four separable roles that must not be conflated: creating leakage-resistant train/dev/test groups; optional storage layout; optional retrieval hint; post-hoc stratified analysis. The first is mandatory, the latter two should be randomized/ablated, and analysis labels may remain evaluator-only.
- Per-task distilled artifacts are better named **solution handbooks** or **episode handbooks**, because they summarize one task's multi-model attempts and are not necessarily broadly reusable skills. A later category/general handbook is a distinct abstraction level and must never silently overwrite the episode-level source.

### Delta from the 2026-09-02 report

- The existing report already has immutable sanitized trajectories, rebuildable curated artifacts, task-local cited memos, train-first splitting, A2/A3/A4 comparisons, and a rule that test memos do not cross tasks. Those foundations should be preserved rather than replaced.
- The missing pieces are: a first-class schema for multiple good/bad trajectories per source task; an explicit task-level solution handbook artifact; a separate cross-task/category handbook; a pre-classification protocol whose split, storage, retrieval-hint, and analysis roles are independently controlled; and an explicit distinction between ephemeral task-local writes and validation-gated global evolution.
- The existing A4 “dual-layer DCI” should be refined into at least two representation contrasts: raw trajectories + per-task solution handbooks, and raw trajectories + category/general handbooks. Otherwise any gain cannot be attributed to task-level compression versus cross-task abstraction.
- The current static held-out test rule remains the right primary estimand. Shared test-time evolution should be a separately named online protocol with fixed order permutations, prequential scoring, and no claim of exchangeable static-test accuracy.

### First-pass 2026 primary-source literature findings

- **MemSkill: Learning and Evolving Memory Skills for Self-Evolving Agents** (arXiv:2602.02474; v1 2026-02-02, v2 2026-05-24) turns memory extraction/consolidation/pruning operations into learnable skills. A controller selects skills, an LLM executor produces memory, and a designer periodically examines hard cases to refine or add skills. This targets memory-writing policy rather than task-solving handbooks directly.
- **EvoSkill: Automated Skill Discovery for Multi-Agent Systems** (arXiv:2603.02766; 2026-03-03) analyzes execution failures, creates/edits structured skill folders, and retains candidates on a validation-performance Pareto frontier with the base model frozen. It provides a direct validation-gated baseline but lacks WikiSkill's separate persistent knowledge layer.
- **SkillOS: Learning Skill Curation for Self-Evolving Agents** (arXiv:2605.06614; 2026-05-07) pairs a frozen executor with an RL-trained curator that updates an external SkillRepo. It groups task streams by skill-relevant dependencies: earlier trajectories drive updates and later related tasks evaluate delayed effects. This is strong evidence for a separate online-stream experiment, not a reason to contaminate a static held-out test.
- **Self-Evolving World Models for LLM Agent Planning / WorldEvolver** (arXiv:2606.30639; v1 2026-06-29, v2 2026-09-01) combines episodic transition memory, semantic heuristics extracted from prediction-observation mismatches, and selective foresight. It explicitly studies deployment-time context revision, but the stored object is a world model rather than a reusable solution handbook; relevance is conceptual and secondary.
- All four facts above were checked on current arXiv pages. They are reported preprint/author claims until full methods and evaluation protocols are inspected; the final plan should not treat abstract-level gains as independently replicated evidence.
- WikiSkill's own bibliography resolves several ambiguous names/IDs: Trace2Skill is arXiv:2603.25158; SkillOpt is 2605.23904; Co-EvoSkills is 2604.01687; MetaClaw is 2603.17187; SkillRL is 2602.08234; Skill1 is 2605.06130; Memento-Skills is 2603.18743.
- **Trace2Skill** (2026-03-26) analyzes a diverse trajectory pool in parallel, extracts trajectory-local lessons, and hierarchically consolidates them into a unified conflict-free skill directory. This is the closest baseline for building one category/general handbook from multiple source-task handbooks, but it does not provide DCI-style test-time access in the abstract.
- **SkillOpt** (2026-05-22) treats a single skill document as external trainable state: a separate optimizer converts scored rollouts into bounded add/delete/replace edits, accepts only strict held-out-validation improvements, keeps a rejected-edit buffer, and uses a textual learning-rate budget plus epoch-level meta updates. It supplies a useful reversible-update baseline but may be too monolithic for a large heterogeneous trajectory corpus.
- **Co-EvoSkills** (2026-04-02) co-evolves a multi-file skill generator with a surrogate verifier that provides feedback without test ground truth. It is relevant to acceptance-gate design, but a learned surrogate must itself be trained and audited for reward hacking.
- **MetaClaw** (2026-03-17) combines fast skill synthesis from failure trajectories with opportunistic policy updates and uses support/query versioning to reduce contamination. It targets a streaming production distribution and model updates, so it is an online comparator rather than a clean static-test baseline.
- Full-paper HTML confirms **MemSkill** maintains two distinct objects: trace-specific memory banks and a shared bank of reusable memory-construction skills. It starts from Insert/Update/Delete/Skip primitives, learns Top-K skill selection, mines a bounded/expiring hard-case buffer, clusters representative failures, and snapshots/rolls back the skill bank when stabilized training reward regresses. Its “memory skill” is a write policy, not the stored domain handbook itself.
- Full-paper HTML confirms **Trace2Skill** uses asymmetric good/failure analysts, with failure analysis allowed to inspect artifacts and validate causal fixes; hierarchical merging deduplicates, resolves conflicts, preserves non-overlapping lessons, prefers recurrent patterns, and routes idiosyncratic details to `references/`. By default it aggregates all patches, while an expensive validation-based subset search is optional. This motivates keeping per-task handbooks intact and building category/general handbooks as separately versioned derived artifacts.
- Full-paper HTML confirms **SkillOpt** has explicit train/selection/test splits and exports only the best validation-gated `best_skill.md`. Its bounded fast edits cannot overwrite a protected slow-update field; rejected edits feed a buffer, and a longer-horizon meta update consolidates accepted/rejected patterns. This supports separating evidence/log state from executable handbook state and never applying unvalidated task-time edits globally.
- Full-paper HTML confirms **SkillOS** deliberately trains on ordered groups of related tasks; the first task starts with an empty repo, curator writes after each task, and success on later related tasks supplies delayed reward. Its authors report the strongest ablation loss when related grouping is replaced by random order, and identify fixed one-shot BM25 retrieval plus flat Markdown entries as limitations. This directly motivates an **online stream** secondary experiment with group/order controls and DCI retrieval, not a silent change to the static-test estimand.
- **MUSE-Autoskill** (arXiv:2605.27366; v1 2026-05-26, v2 2026-07-03) treats skills as lifecycle-managed assets with creation, memory, management, evaluation, and refinement. Its especially relevant contribution is per-skill memory accumulated across tasks, plus unit-test/runtime-feedback-triggered refinement and cross-session persistence. For this project, per-source-task solution handbooks should analogously have an append-only evidence ledger and impact history, while promoted handbook versions remain immutable and gated.
- **Learning to Remember / Unified Memory Agent (UMA)** (arXiv:2602.18493; 2026-02-13) learns end-to-end memory management for long-context streams using a compact core summary plus a structured Memory Bank with create/update/delete/reorganize. It is useful for CRUD and conflict-resolution vocabulary, but its goal is evolving factual/state memory in a stream rather than cross-task procedural transfer; it should be a secondary design reference, not a primary baseline.
- MUSE's own comparison frames per-skill experience, unit-test-driven evaluation, and automatic refinement as a lifecycle gap in prior systems. Its reported numbers are under review and include successfully covered subsets, so the final report should cite mechanisms and keep denominators explicit rather than use headline accuracy as proof.
- **SkillRL** (arXiv:2602.08234; 2026-02-09) combines an experience-distilled hierarchical SkillBank, adaptive retrieval of general and task-specific heuristics, and recursive co-evolution of the library with an RL-trained policy. Because both policy and library change, it is useful as a scaling reference but a poor causal baseline for this project's frozen-executor handbook interface.
- **Memento-Skills** (arXiv:2603.18743; 2026-03-19) uses structured Markdown skills as persistent stateful-prompt memory and a read-write reflective loop: a trainable router reads, then the agent updates/expands the library from experience without changing base parameters. It is a direct conceptual comparator for the whiteboard, but its static/online split and update-validity controls need full-method scrutiny before borrowing claims.
- **Skill1** (arXiv:2605.06130; v1 2026-05-07) trains one policy to generate a search query, select a skill, execute with it, and distill a new skill from the trajectory under a shared outcome reward. It deliberately couples retrieval, execution, and writing, whereas this project needs factorized arms to identify which component causes gains.
- WikiSkill cites **Meta Context Engineering via Agentic Skill Evolution** as an ICML 2026 paper, but OpenReview was blocked by a browser challenge in this environment. It can be listed only if a directly accessible primary copy is found; otherwise it should not carry consequential claims in the report.

### WikiSkill primary findings from the attached paper

- Verified title: *WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution*, arXiv:2608.27454v1, dated 2026-08-27/28; 28 PDF pages.
- WikiSkill explicitly separates three layers: immutable raw execution traces (`raw/`), a persistent structured wiki (`wiki/`), and active procedural skills (`skills/`). This is closely aligned with the meeting architecture but adds a crucial middle knowledge layer.
- Training/evolution uses three disjoint splits: train generates rollouts, validation gates candidate skill updates, and test is reserved for final evaluation. Candidate skills roll back when validation does not improve; the wiki is intentionally not rolled back and records accepted/rejected edits plus measured impact.
- Wiki contents include pattern pages, an index, evolution logs, and a programmatically updated `skill-impact.md`. The skill proposer uses ReAct-style on-demand reads of wiki pages and raw traces rather than receiving every trace in one prompt.
- The inference agent is restricted from wiki access during WikiSkill training rollouts; the paper reports that giving it wiki access during training slightly reduces average downstream performance. Therefore the project's test-time DCI use of raw/wiki evidence is a new treatment and must not be attributed to WikiSkill.
- WikiSkill reports results across five benchmarks and five models, three independent evolution runs, and paired bootstrap significance tests. It reports that gains often increase with model capability, skills can transfer across model families, and a persistent wiki materially improves skill evolution.
- The design transfer that is directly justified is: immutable evidence + persistent intermediate knowledge + reversible executable handbook updates + validation gate. It does **not** justify using test outcomes to update shared state or exposing evolving shared state to later test tasks in a static held-out evaluation.
- Visual inspection of PDF pp.4, 8, 11, and 14 confirms the architecture diagram and reported tables. Table 3's default configuration grants wiki access to the Skill Proposer but not the Inference Agent during training evolution; it reports 63.7 average versus 48.7 without persistent wiki access, while granting both agents access reports 60.9.
- WikiSkill itself flags three relevant limitations: it directly injects skills and therefore does not test retrieval/triggering; its strict validation gate rejects neutral stepping-stone updates; and its wiki has no automated pruning. It also identifies within-one-rollout online skill adaptation as future work. These gaps are precisely where DCI-style retrieval and the project's proposed online scratch/delta protocol may contribute, but those extensions require new evidence.
## 2026-09-03 — Additional self-evolution evidence

- **Memento-Skills** (arXiv:2603.18743, 19 Mar 2026) makes the read/write loop explicit: retrieve a structured skill, execute with a frozen model, attribute a failure to one skill, propose a targeted file-level rewrite, and guard mutation with a synthetic unit-test gate. This is a useful *online continual-learning comparator*, but its write-after-feedback setup must not be mixed into the static held-out causal estimate. The paper also reports that cross-task transfer is strongest under structured domain categories, supporting a category-routing ablation rather than making category labels mandatory input.
- **MUSE-Autoskill** (arXiv:2605.27366v2, 3 Jul 2026) treats skills as lifecycle-managed assets with creation, catalog retrieval, per-skill memory, evaluation, and refinement. For our design, the clean translation is an append-only evidence ledger plus versioned candidate handbooks; a candidate becomes shared state only after an independent gate. Its headline self-created-vs-human result is on the successfully covered subset, so the report should not generalize that number to all tasks.
- MUSE’s concrete separation is useful: each skill has a published `SKILL.md` plus a sibling, per-agent `.memory.md` that appends observations and is excluded from the transferable package. The paper itself warns that its within-task distill-and-rerun protocol may overstate gains; this strengthens the case for task-disjoint historical/test splits here.
- MUSE also exposes a failure mode we should measure, not hide: trajectory-derived procedures can be stable yet overfit source-run details. Report both *coverage* (can a usable handbook be built/retrieved?) and *conditional quality*, while keeping the all-task denominator primary.
- Memento-Skills’ reported category benefit is supportive evidence for a category-aware routing arm, not proof that evaluator-supplied class labels should be exposed in every condition.

## 2026-09-03 — Delta against the 2026-09-02 report

- The current report already has strong provenance, split, sanitizer, physical-isolation, candidate-matching, weak/strong executor, and static task-local memo controls. Preserve these rather than rewriting the study from scratch.
- The architecture needs a semantic refactor: current Layer 2 collapses generic skills, cards, and warnings. The revision should distinguish (a) immutable per-history-task **solution handbooks**, (b) optional cross-task/category **general handbooks**, and (c) a persistent evidence-backed **wiki/evolution ledger** from which versioned handbook candidates are proposed.
- Current Layer 3 correctly forbids cross-test-task sharing for the static causal experiment. Add a separate, explicitly prequential online-evolution track instead of weakening that guarantee.
- “Pre-classification” must be split into four roles: split stratification (mandatory), physical organization (generated view only), retrieval hint (ablated), and post-hoc subgroup analysis (evaluator-only by default).
- The current A4 (`raw + Layer 2`) would confound task-specific compression with cross-task abstraction. Split it into `raw + task handbooks` and `raw + task + general handbooks`, or use a factorial representation table.
- There is a duplicated sentence in Phase 1.5’s precision/power memo list; remove it during revision.

## 2026-09-03 — Independent handbook-semantics audit

- Test-time writing changes the estimand. Keep three named protocols separate: **F** frozen shared handbook (primary static generalization), **L** task-local writable overlay reset after each test task (within-task adaptation), and **C** post-score shared continual evolution (prequential learning).
- Never rewrite a historical task’s `TaskBook`. A new task writes only a private scratch/episode delta. Reusable claims become candidates for a wiki/general handbook; promotion is versioned and gated.
- Use a flat, content-addressed canonical episode store. Category directories are generated views/indexes, so the category experiment measures routing/hints rather than accidental file-layout differences.
- For C, enforce `serve → score → ingest authorized feedback → propose → past-only/shadow-dev gate → promote/reject`; the unit of inference is a pre-registered task-order stream, not an independent task row. A fresh post-stream holdout is required to claim forward transfer.
- Unlike WikiSkill’s always-compounding wiki, a rejected candidate must not influence the served state in the confirmatory protocol; preserve it only in an audit branch.
- Necessary falsifiers include mixed-vs-good-only under matched evidence budget, shuffled failure labels, class-routed-vs-flat (plus an oracle-class diagnosis), input permutation audits, and leave-one-source-model-out transfer.

## 2026-09-03 — Early-2026 procedural-memory paper

- arXiv:2602.01869 was initially cited under an older `ProcMEM` name but is now **Skill-Pro: Learning Reusable Skills from Experience via Non-Parametric PPO for LLM Agents** (v3, 28 May 2026; ICML 2026 spotlight). Use the current title and URL.
- Skill-Pro compiles trajectory batches into executable units with explicit **activation, execution, and termination** conditions. This is a strong schema for a *general procedural handbook*, not for per-task solution summaries.
- Its PPO Gate scores candidates counterfactually on historical trajectories and its online score prunes non-positive or redundant skills. It does not use a separately described held-out validation gate or explicit rollback, so our confirmatory promotion protocol should remain stricter and versioned.
- Skill-Pro keeps skill selection fixed while evolving the skill pool. That factor separation is useful, but the project’s DCI contribution is precisely to vary serving/search while holding evidence and executable representation fixed.

## 2026-09-03 — Literature search synthesis

- No verified paper found combines all four: immutable trajectories, persistent writable knowledge/handbook, test-time interactive DCI search, and a safe semantic/behavioral commit gate. This intersection remains the project’s clearest research gap.
- **Tiered Memory** (arXiv:2602.17913) is the closest provenance-aware read path: immutable raw pages, derived facts with stable links, escalation to raw, then write-back. Its main experiment disables online write-back and its repeated-question study is cache amortization, so it is architecture evidence rather than online-generalization evidence.
- **Useful Memories Become Faulty When Continuously Updated by LLMs** (arXiv:2605.12978) provides the strongest negative evidence against recursive in-place rewriting: append-only and raw retention outperform destructive consolidation in its matched ablations. This supports revision/supersede semantics and an immutable evidence layer.
- **RecMem** (arXiv:2605.16045) suggests recurrence-triggered consolidation, but needs a rare-critical bypass and a gate; otherwise one-off safety constraints may never be promoted.
- **Infini Memory** (arXiv:2606.10677) offers a useful filesystem pattern: CURRENT staging, topic documents, catalog/search/local read, and split/merge. It does not supply a semantic no-regression gate.
- **Transferable Self-Evolving Playbooks for Agentic Security Auditing** (arXiv:2606.16420) provides the strongest engineering pattern for safe promotion: Git branches, delayed candidate participation, executable regression gates, replay, and retained rejected history. It is domain-specific and uses frozen held-out evaluation.
- A safe project synthesis therefore needs two independent gates: (1) source entailment/provenance and (2) behavioral non-regression on protected calibration/replay tasks. Updates are append-only candidates; raw is never overwritten.

## 2026-09-03 — Benchmark-fit audit

- Keep **AppWorld** as paper-level confirmatory primary. Promote **τ³ airline/retail/telecom** from mere search sanity check to the low-cost controlled handbook-evolution development benchmark. Keep `banking_knowledge` separately as fixed-document DCI evidence localization.
- Promote **WildClawBench** from ingestion-only to the first end-to-end corpus pilot because its public task×model sessions most closely match the whiteboard’s multi-model good/bad shape. Its 60 tasks, broad classes, live-web/multimedia dependencies, and lack of official split make it insufficient as the only confirmatory benchmark.
- Existing h20 SWE trajectories are not yet the desired per-task multi-model good/bad grid; provenance and outcome coverage are incomplete. Use h20 for schema/lineage/sanitizer work, then use SWE-bench-Live as temporal coding confirmation.
- Before bulk download/generation, freeze benchmark revision, task/near-duplicate clusters, split, model roster, seeds, number of attempts, license state, and outcome policy. “All trajectories” means all pre-registered rollouts—not adaptive sampling until success/failure balance is achieved.
