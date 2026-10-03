# Trace2Skill：源码核查与 WildClawBench 适配建议

核查日期：2026-09-10。状态：方法研究；未运行作者代码，未调用模型蒸馏，未修改现有实验数据或结果。此文中的外部 prompts / SKILL.md 仅作为研究对象。

**建议采用它的“逐轨迹提取 → 多来源归纳合并”方法，但不直接移植整个运行框架或现成 xlsx skill。** Trace2Skill 对单一已知技能做离线改进；V4 则要构建可被 DCI 按需检索的多技能库。这两个设置有交集，但不能称为完整复现。

## 1. 核验到的版本与实际范围

| 项目 | 核验结果 |
| --- | --- |
| 论文 | [Trace2Skill: Distill Trajectory-Local Lessons into Transferable Agent Skills](https://arxiv.org/abs/2603.25158v5)，v5，2026-06-04；最初提交 2026-03-26 |
| 官方源码 | [固定 commit](https://github.com/Qwen-Applications/Trace2Skill/tree/3d0b52a140f002a512930252b613c49048f7d5ac) `3d0b52a140f002a512930252b613c49048f7d5ac`，commit 时间 2026-05-01 |
| 下载论文 | [Trace2Skill_2603.25158.pdf](https://arxiv.org/pdf/2603.25158v5)，1,991,654 bytes |
| PDF SHA256 | `9f3f43101583a993b9a82905d790fb2dd57c60b37897187a0706c047bd0b58b0` |
| 下载来源记录 | source_manifest.json（历史文件：`archive/skill_distillation_research_20260910/trace2skill/source_manifest.json`，不在精简发布中） |
| 研究快照 | archive 目录（历史文件：`archive/skill_distillation_research_20260910/trace2skill`，不在精简发布中）：论文 HTML / HF Markdown、GitHub commit/tree、选取的分析和合并源码、少量成品样例；未下载整套 SpreadsheetBench 数据 |

论文 v5 的实验证据比该源码 commit 更新；下文区分论文描述与实际读到的实现。官方仓库提供四组表格技能，公开实现也主要围绕表格任务。论文另外报告数学、DocVQA 和文档操作迁移，不能因此假定公开仓库包含这些设置的完整可复现代码。[论文 §2–4 与附录](https://arxiv.org/html/2603.25158v5)、[仓库 README](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/README.md)

## 2. 实际输入与处理顺序

论文的三阶段是收集带成败标签的轨迹、并行提出局部技能补丁、层级合并。源码将中间工作拆得更细：

```text
初始技能 S0 + 构建任务
  → agent 轨迹、产物、官方评测结果
  → 成功分析 / 失败分析
  → 可解析的 analysis records
  → 相对于同一个 S0 的局部 patches（MAP）
  → 多层合并（REDUCE）
  → 翻译为精确编辑、应用、格式检查
  → 另行在构建侧重跑验证；再做独立测试
```

### 成功轨迹：单次分析，但不是直接生成整份技能

[`analysis/success_analysis_system_llm.txt`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/analysis/success_analysis_system_llm.txt) 要求输入完整聊天记录，输出两类内容：精简的成功操作链，以及至多 3 条可泛化经验。操作链只保留有日志依据、实际导向结果的动作与观察；去掉无效分支，禁止补写不存在的步骤。默认入口只选 `SUCCEED` 日志；`--all-outcomes` 可以放开这一筛选，因此调用方仍必须保证标签正确。[`run_success_analysis_llm.py`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/analysis/run_success_analysis_llm.py)

这适合复用为 V4 的初步提取。不过我们应把最终成功路径和失败后的有效恢复分别保存：如果直接删除所有失败分支，会丢失“什么现象触发了更换方法”的重要条件。这是本项目的适配建议，不是作者原样设计。

### 失败轨迹：原版要求能够验证修复

[`analysis/error_analysis_system.txt`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/analysis/error_analysis_system.txt) 不只看日志。分析 agent 可以查看输入表、原输出、gold 文件和执行代码；先定位差异，再实施最小修复，写出 `output_fixed.xlsx` 并调用评测工具。成功后才归纳因果机制与至多 3 条预防经验。最终经验必须从原解题 agent 的可见信息出发，不能教它访问 gold，也不把分析阶段的路径变化当成原 agent 的错误。

源码的门禁依赖报告所在形式：`collect_error_records()` 对目录形式要求 `evaluate_passed.flag`；对单个 `error_analysis_*.md` 文件则不要求该 flag。这是为了兼容仅 LLM 分析模式，所以“进入 parsed records”本身不证明修复已通过验证。[`analysis/report_parsing.py:171`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/analysis/report_parsing.py#L171)

**对我们最关键的边界：如果只有旧轨迹而缺少可重放环境/产物，就只能提取日志中已观察到的失败和恢复；不能把模型提出的修复猜测标成已经验证的技能。** 原版的 agentic 失败分析与源码中的 log-only 分支要分开登记。

### records：蒸馏不是从一个未区分的长文本堆直接开始

解析后记录保存 `instance_id`、`source_file`、`items`；items 分为 `failure_cause`、`failure_memory`、`success_memory`，含 title、description、content。成功分析报告的完整操作链不是 `parse_success_items()` 的结构化对象；如果我们需要每一步的动作—结果—证据位置，必须增补自己的 schema，不能假定作者的 parsed JSON 已有这些字段。[`analysis/report_parsing.py`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/analysis/report_parsing.py)

合并入口支持错误和成功两个 JSON 输入，也支持预先压缩过的 patterns 模式；要求已有 `SKILL.md`。所以源码的“创建”也需要一个初始种子技能，不是把空目录直接传进去。[`run_parallel_combined_skill_evolution.py:79`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/skill_evolver/run_parallel_combined_skill_evolution.py#L79)

## 3. 哪些实现细节值得采用

| 设计点 | 源码实际行为 | V4 可采用的方式 |
| --- | --- | --- |
| 相同初始状态 | 每个 MAP 分析读同一个 `skill_state`，完成后按 batch index 恢复顺序，避免线程返回快慢决定后续输入顺序 | 固定输入哈希、task/model/episode ID 和合并排序；并行只加速，不改变材料顺序 |
| 小批次层级合并 | combined 入口默认每次 MAP 1 个 record、每次 MERGE 5 个 patch、最多 5 层 | 先逐 episode 提取；同任务多模型先对齐，再按操作合并，避免把同题 12 次重复当成 12 种任务证据 |
| 语义去重与冲突处理 | 合并提示要求去重、保留独特经验、处理冲突；这是 LLM 判断，不是具有语义正确性保证的算法 | 冲突保留条件分支与来源；证据不足时不投票抹平。每次合并输出支持/反例/舍弃理由 |
| 主技能与附录分开 | `SKILL.md` 放常用规则；详细或特殊内容放 `references/*.md`；新增 reference 与入口链接必须成对保留 | 短 SKILL.md + 可追溯的 evidence/reference；agent 自主用原生工具继续阅读 |
| 可审计的中间结果 | `_save_prompt_response()`、map patches、各层合并结果、changelog 可保存 | 正式蒸馏时全部进入独立 run 目录；失败解析材料进该 run 的诊断区，不散落到 docs |

源码定位：[`ParallelSkillEvolver.run_map_phase():1857`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/skill_evolver/parallel_evolving_agent.py#L1857)、[`run_reduce_phase():2319`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/skill_evolver/parallel_evolving_agent.py#L2319)、[`merge_system_prompt.txt`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/skill_evolver/prompts/parallel_evolving_agent/merge_system_prompt.txt)、[`_enforce_create_pairing():1657`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/skill_evolver/parallel_evolving_agent.py#L1657)。

## 4. 不能直接照搬的部分

**格式通过不是任务能力通过。** `run_verification_phase()` 的循环是格式检查失败后请 LLM 修文档；实际调用 `SkillEvolver.validate_skill()`。后者查找 `skills/skill-creator/scripts/quick_validate.py`，路径不存在时直接返回成功并注明跳过。该脚本不在本次固定 commit 的仓库树中。V4 需要把“语法/引用检查”“证据检查”“环境重放”“新任务迁移评测”记为不同状态。[`skill_evolving_agent.py:59`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/skill_evolver/skill_evolving_agent.py#L59)、[`validate_skill():871`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/skill_evolver/skill_evolving_agent.py#L871)

**合并失败不能悄悄丢候选。** `run_reduce_phase()` 达到最大层数后会强行合并；再次失败时最终只返回首个 patch。我们应改为保留全部未解决候选，并将该次构建标成未完成，直到补齐或明确登记舍弃原因。文本编辑去重中“同一位置保留较长版本”也不能充当证据质量判断。[`parallel_evolving_agent.py:2404`](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/skill_evolver/parallel_evolving_agent.py#L2404)

**现成技能带有旧 harness 的操作协议。** 核查 `released_skills/xlsx-35B/SKILL.md` 时，看到除表格规则外，还写入 Action JSON 封装、括号转义、禁止在 action 中使用 heredoc 等要求；局部还混有不同括号表述。这些适配上游 ReAct 解析器的指令不应进入我们 Codex 技能的通用操作步骤。尤其用户已经明确要求保留原生 Codex harness。[发布的 xlsx-35B 样例](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/released_skills/xlsx-35B/SKILL.md)

**原实验没有解决“该检索哪份 skill”。** 作者针对已知目标技能，在推理提示中预加载根 `SKILL.md`，附属 references 仍按需发现；V4 的“raw trajectories + 多个 distilled skills 均可检索”属于新增设置。[论文附录 I.1](https://arxiv.org/html/2603.25158v5#A9.SS1) 因此应保持 V3.1 的原生搜索读取路径，只新增技能语料和必要目录说明，避免同时重写搜索接口或强制整库注入。

**公开代码不等于统一的宽松授权。** 本次仓库树没有根 LICENSE；两个深挖现成 xlsx skill 的目录及基础 xlsx 目录附有 Anthropic 的限制性 `LICENSE.txt`。这里只报告文件事实，不作法律解释。可参考方法并用本项目自己的文字、schema 和代码实现，不把这些现成表格技能直接复制为 WildClawBench 技能。[固定版本的 LICENSE.txt](https://github.com/Qwen-Applications/Trace2Skill/blob/3d0b52a140f002a512930252b613c49048f7d5ac/released_skills/trace2skill-xlsx-35B-combined/LICENSE.txt)

## 5. 给 V4 的建议路径

这是依据上面源码分析提出的本项目方案，尚待与其他方法共同比较，并非已经选定或运行：

1. **冻结构建输入。** 只处理已有 build 任务及其官方轨迹；task ID / family ID / model / source SHA / outcome 独立记录。未知或部分成功的 outcome 不强行转为二元成功。
2. **逐 episode 提取操作证据。** 每条候选包含触发条件、环境/工具前提、实际动作、可见结果、校验方式、源消息/行号；允许输出“无可复用经验”。先把观察事实与模型推断分开。
3. **区分经验类型。** 成功操作、已观察到的失败后恢复、已观察失败但未验证修复三类分别保存；第三类不写成肯定式成功步骤。只把有依据的避免条件或诊断信号放进可用技能。
4. **先同任务比较，再跨任务归纳。** 多模型轨迹用来发现可替换路线和冲突；泛化支持数按不同任务/家族计算，另存 episode 数。合并目标围绕可复用操作，不机械地每条轨迹产出一个 skill，也不把六大类强压成六份巨型文件。
5. **独立编写短技能。** 以简单空白模板为种子，包含名称、触发条件、必要前提、步骤、验收、失败分支和证据链接。明确去掉旧 harness 封装、任务特定答案和无法从轨迹核验的泛化。
6. **分层检查，再冻结供检索的版本。** 先做 schema / 路径 / 证据对应检查；若后续安排重放，只在构建侧或预先隔离的开发侧进行，不能用测试结果反复修 skills。对未重放的技能诚实标记其证据等级。

最值得借鉴的不是几百行通用“多想想、多检查”，而是：先让每条经验有动作和结果依据，再合并成有适用条件的操作程序。V4 最终是否获益还取决于检索命中、技能读取、环境匹配和实际采用，应继续沿用现有实验对这些环节的观察。
