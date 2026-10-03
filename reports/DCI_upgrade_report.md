> 历史研究报告：保留方案演进与文献讨论。当前实现仅维护 V1 / V1.1 / V5；旧版运行链接不属于精简发布内容。请以 [项目入口](../README.md) 与 [数据说明](../DATA.md) 为准。

# 从同题 Skill Rescue 到可验证的跨域经验迁移

## 一份 mechanism-first、multi-benchmark 的 DCI / DR-DCI 实验计划

**报告日期：** 2026-09-02
**研究补充：** 2026-09-08 新增第13节“检索与证据采用改进”，为首轮 WildClawBench 之后的讨论稿；本次只更新研究文档，未启动新实验。第0–12节保留原框架的历史语境。
**h20 已审计工作区：** /data/hangxiao/skill_shadowing
**结论口径：** 本报告严格区分【h20 已观察】、【论文报告】、【官方核验】和【提议实验】。任何“可下载”都不自动等于“可训练、可再分发或可用于构建 memory”；任何公开同题 trajectory，即使只在推理时读取，也按泄漏处理。

---

## 0. 结论先行

当前最值得发表的问题，不是“把更多 trajectory 塞进上下文是否有用”，而是：

> **在证据池、预算和执行器固定时，DCI 式的查询时交互，是否能降低弱 agent 使用 raw historical trajectory 的能力阈值；这种收益能否从 SWE 同题救援迁移到真正隔离的跨域任务？**

现有证据支持把实验重构为以下路线：

1. **先修内部有效性，而不是继续放大旧 rescue 数字。**【h20 已观察】h20 的 61G 工作区只有 SWE-bench 系列资产；355 个配对任务中的 98 个 FAIL→PASS 对应 27.6% 的“baseline 已失败条件下救援率”，不是总体处理效应。当前没有严格 pristine 的同宇宙 test，且 provenance、runner 隔离、evaluator 版本、路径配置、泄漏筛查和 Pro trajectory 覆盖都仍有限制。
2. **把表示、服务机制和执行器能力分开。**【论文报告】filesystem-memory 研究显示，在其对话记忆设定中，固定 searcher 时 builder 变化远小于 searcher 能力变化；在 ALFWorld 中，raw episode 对强 executor 很有效，但弱 executor 的最佳结果来自 curated skill + memory + query-time guidance。它不是“搜索永远比构建重要”的普遍定律，而是要求本项目检验 **representation × serving mechanism × executor strength** 的交互。
3. **明确区分两个研究任务。** 任务 A 是 search-interface / evidence localization，例如 τ³-bench 的静态 banking knowledge 与 BrowseComp-Plus 的固定文档；任务 B 才是 historical experience transfer，即只用 train trajectory 帮助 held-out task。A 能验证查询机制，不能冒充 B 的迁移证据。
4. **把 AppWorld 作为第一项跨域主实验。**【官方核验】750 tasks 来自 250 scenarios × 3；只允许 train 进入 memory，dev 用于冻结设计，test_normal 与包含未见 app 的 test_challenge 只在一套预先冻结的 task×seed schedule 上做最终评测。这里的“一次”指执行一次完整冻结 schedule，不是随意选择单个 seed。测试包可下载不代表可以构建 memory。
5. **最小可发表路线是分阶段收敛。** Phase 0 修复 h20；Phase 1 用现有 SWE 和 WildClaw 做 ingestion pilot；Phase 2 完成 AppWorld train→dev→test 主实验；Phase 3 用 τ³ 做机制检查、用 SWE-bench-Live 做严格时间切分的编码确认；其余昂贵或许可不清的资产只在 gate 通过后进入。

---

## 1. 当前证据边界：h20 实际有什么

### 1.1 资产盘点

以下均为【h20 已观察】，不是对外部 benchmark 的推断。

| 资产 | 规模 | 可支持的结论 | 不能支持的结论 |
|---|---:|---|---|
| 混合工作区 | 61G | 已有较大规模的 SWE 实验与中间产物 | 不是单一、干净、可复现的数据集快照 |
| SWE-bench full-test 元数据 | 2,294 个唯一任务 | 定义当前 SWE 任务宇宙 | 不等于 2,294 个都有 trajectory 或独立评测 |
| SWE-bench Verified | 500 个唯一任务 | 可做高质量子集分析 | 当前没有严格 pristine 的 Verified test |
| traj_h20 | 734 个合法 JSON .traj，186M | 可做 schema、检索和 patchless ingestion 研究 | 不是 734 条已验证成功轨迹 |
| packets_out | 734 项，32M | 旧蒸馏产物，可用于 lineage 核查 | 不能默认无泄漏或与实现说明一致 |
| skills_out | 731 个任务目录，17M | 可追溯的主要 skill lineage | 不等于 main_skills 的 720 项，也不等于 734 trajectory |
| main_skills | 720 项，9.2M | 后续主实验使用过的一条 lineage | 与 731 skills_out 覆盖不同 |
| skills_en | 216 项，2.8M | 英文 skill 子集 | 不是完整 ExperienceBook |
| main_out | 1,562 个 arm verdict，6.3M | 旧同题 skill rescue 的诊断基线 | 覆盖不齐，不能直接估计总体因果效应 |
| frozen | v1 132 项；v2 152 项，共约 3.0M | 可复核版本差异 | 不是独立 train/test split |
| repos / work | 13 项 / 2.5G；81 项 / 44G | 可复核运行现场 | runner 与 evaluator 尚未物理隔离 |

734 个 trajectory ID 都能连接到 full-test 元数据，其中 235 个属于 Verified。main_out 覆盖 398 个 Verified，因此“未进入 main_out”的 Verified 数量是 102；这与后文更严格的 untouched 口径不是同一个数。

h20 快照中的 SWE-bench Pro 也只是 SWE 家族的局部辅助资产：instances 目录可见 211 个实例元数据 JSON，另有 731 rows / 11 repos 的 helper 列表；**就 trajectory 资产而言，h20 只有 README 和 9 个 evaluation-result JSON，没有 teacher trajectory**。官方 trajectory 位于另行访问的 S3，不能把 h20 的 helper、结果 JSON 或 README 写成“已拥有 Pro trajectory”。

【h20 已观察】服务器快照只读核对可见 sib_skills 35 份、sib_out 30 份。【用户历史回忆，尚未由脚本/manifest独立核验】这些文件可能由每类约 4–5 个同源 task skill 融合为 general skill；服务器快照没有构造脚本或 manifest，当前无法独立还原来源。它们只作为待 provenance / leakage 审计的 curated / compositional Layer-2 候选，不能把融合过程写成已验证事实。另有人工 confusion 资产 61 个 task 目录、241 个文件，保持原样归档，暂不进入后续主实验，也不在本报告深入分析。

### 1.2 旧同题实验到底说明了什么

【h20 已观察】main_out 的 1,562 个 verdict 覆盖 1,207 个任务：

| 统计项 | 数值 |
|---|---:|
| no-skill arm | 1,001 |
| skill arm | 561 |
| 完整配对 | 355 |
| 单臂任务 | 852 |
| 配对中的 FAIL→FAIL | 257 |
| 配对中的 FAIL→PASS | 98 |
| GPT 配对 / Haiku 配对 | 348 / 7 |
| baseline 失败条件下的 rescue rate | 98 / 355 = 27.6% |

这 355 个配对任务的 no-skill arm 全部失败，所以 27.6% 只回答“baseline 已失败时，同题 teacher skill 能救回多少”。它无法估计 PASS→FAIL、平均伤害率或 population-average treatment effect。旧 runner 还把任务专属 TEACHER_SKILL.md 写入当前任务，这是一种 same-task treatment，不是 train-only ExperienceBook transfer。

【h20 已观察】selector 每任务运行 10 次，即 2 种 prompt wording × 5 种 permutation：

| selector 条件 | 任务数 | 正确 / 总运行 | aggregate accuracy |
|---|---:|---:|---:|
| raw | 132 | 874 / 1,320 | 66.21% |
| baseline | 72 | 532 / 720 | 73.89% |
| repaired / C1 | 132 | 1,062 / 1,320 | 80.45% |
| repaired_v2 / C2 | 132 | 951 / 1,320 | 72.05% |
| 五选一随机 | — | — | 20% |

baseline 只有 72 个任务，其他主要条件有 132 个任务。因此 73.89% 不能与 80.45% 做未配对的因果比较；需要 common-task slice、逐任务配对差异和任务聚类置信区间。

### 1.3 Trajectory、outcome 和 leakage 的实际状态

【h20 已观察】

- 734 个 .traj 都能解析为角色消息列表；消息角色包括 system、user、assistant、tool。抽样文件有 143 条消息、71 个 assistant turn，这只是结构样本，不是全语料统计。
- trajectory 顶层不含 instance ID、repo、生成时间、model、teacher/student 或 outcome。instance ID 来自文件名，issue date 来自 parquet，teacher 来源只见于硬编码 S3 路径；下载 mtime 不能代替生成时间。
- resolved_cache.json 有 1,280 个布尔结果：193 true、1,087 false。734 个 trajectory 中只有 124 个能连到缓存结果，其中 105 true、19 false、610 uncached。因此不能把 734 条写成 734 条成功 trajectory。
- 旧 distill_h20.py 对每步 thought / action / observation 分别截断到 600 / 800 / 350 字符；实现没有显式追加 final patch，而 recipe 文字声称包含，已确认存在文档—实现不一致。
- gold/test-patch 派生字符串的启发式筛查命中 raw trajectory 730/734、packet 729/734、skill 88/731。这些是 screening flags，不是“已证实 730 条泄漏”；必须保留规则、匹配片段和假阳性类型。
- leak_prescreen.json 有 85 项：6 项带 high-risk list，79 项不带 high-risk；另有 75 个普通标识符命中尚未分类。旧报告排除过 11 个 skills，但不足以形成可审计 sanitizer。

本报告中的 **raw trajectory** 特指“没有语义蒸馏、但已做安全与答案泄漏清洗的历史记录”，绝不等于原样暴露。主实验 raw 至少删除：

- final diff、gold patch、test patch、可重建最终答案的 edit payload；
- hidden tests、evaluator 内容、credentials、访问令牌和环境秘密；
- system prompt / tool observation 中的命令注入、控制字符和越权指令；
- 跨所有 message channels 的同题标识、未来信息与 judge 路径。

清洗有效性要由 blinded patch-reconstruction audit、canary 测试和逐 episode provenance manifest 联合验证。

### 1.4 当前没有严格 pristine 的同宇宙 test

【h20 已观察】“untouched”至少有三种不同口径：

| 口径 | full-test 剩余 | Verified 剩余 | 含义 |
|---|---:|---:|---|
| 未见 trajectory 且未进入 student run | 1,068 | 101 | no-content/run |
| 未见主要 artifact 路径或文件名 | 996 | 87 | conservative artifact-untouched |
| 再把 resolved_cache 的 boolean outcome 算入 | 0 | 0 | 严格意义不存在完全未接触标签的同宇宙样本 |

boolean outcome exposure 与直接内容泄漏不同：只有当其参与 selection、tuning、stopping 或报告筛选时才形成决策偏差。必须做 decision-lineage audit，不能因缓存文件存在就把所有任务简单判为内容泄漏，也不能把它们称为 pristine。

时间切分同样尚未完成。按 issue creation time 粗筛，734 trajectories 中有 457 个早于 2021-01-01、548 个早于 2022-01-01；但最终 temporal split 必须用修复公开或合并时间，并在任何蒸馏、索引和挑选前冻结。可诚实采用两条路径：

1. 预注册并封存 no-content/run 或 conservative artifact-untouched 子集，完整披露历史 boolean-label exposure，称为 **qualified internal holdout**；
2. 用 SWE-bench-Live 的未来时间窗或其他新任务宇宙建立真正 pristine confirmatory set。

### 1.5 Runner、evaluator 与路径 caveat

【h20 已观察】

- 当前 Codex runner 使用 danger-full-access；copied worktree 与 metadata、gold/test patch、旧 outputs、skills 和 logs 位于同一宽泛目录树。prompt 中禁止访问 .git 或 tests 不是 OS 级隔离。
- judge_verified.py 在 solver 结束后才应用 gold test_patch，并检查 FAIL_TO_PASS ∪ PASS_TO_PASS，高层时序是合理的；但共享文件系统仍允许意外或恶意访问 judge 材料。
- astropy__astropy-7606 在 swebench_all.parquet 与 swebench_full_test.parquet 中的 PASS_TO_PASS 不同，当前 judge 使用前者。所有新实验必须 pin evaluator 数据源和 hash。
- distill_h20.py 硬编码 H=$HOME/gap_run，但 h20 上该目录不存在；实际数据在 /data/hangxiao/skill_shadowing。脚本可能无报错地产生零结果，路径必须集中配置并启动时 fail closed。
- h20 未发现可执行 rg；这首先是接口与可复现性问题，不是当前规模性能瓶颈。734 文件、186M 的 grep -rlF 缓存查询约 0.016–0.078 秒。宽词选择性很差：ValueError 命中 605/734，pytest 命中 714/734，teacher edit-tool 名命中 734/734，因此接口应支持 repo filter、合取查询、窗口读取和有界输出。

---

## 2. 论文证据如何改变实验问题

### 2.1 DCI 与 DR-DCI 提供的是查询机制证据

【论文报告】Beyond Semantic Similarity: Rethinking Retrieval for Agentic Search via Direct Corpus Interaction（下称 DCI）中的 trajectory 指当前搜索 agent 的 action / observation 序列，不是历史修复 trajectory。把 DCI 映射到 ExperienceBook 是本项目的研究假设，不能写成论文已证明的结论。

在 BrowseComp-Plus 的 100-query mechanism comparison 中：

| 方法 | mean gold-document coverage | evidence localization | answer accuracy | tools / query |
|---|---:|---:|---:|---:|
| Qwen3-Embedding-8B | 56.7 | 21.7 | 45 | 17.55 |
| DCI-Agent-Lite L4 | 28.0 | 48.4 | 73 | 35.35 |

同一比较中，受限 read + grep 得到 61，open Bash 得到 73，dense baseline 为 45。它支持“交互式证据定位可能弥补单次检索的定位不足”，但也显示更开放的工具带来额外成本和攻击面。论文的 69.0%→80.0% headline 与这组 45→73 的 100-query mechanism sample 不是同一实验，不应拼接。

【论文报告】DR-DCI 在 830-query controlled comparison 中报告：

- Raw-DCI：62.90%；
- DR-DCI：71.20%；
- 保留 workspace 的 context reset：73.25%。

BCP-100 上 Raw-DCI 为 67/100，DR-DCI + BM25 为 80/100，DR-DCI + dense 为 82/100；禁止跨文档 DCI 后从 82/100 降到 40/100。与此同时，ranking appendix 中 DR-DCI 的 NDCG@10 为 54.2，低于 DCI-Agent-Lite 的 56.7，说明它不是所有指标都占优。DR-DCI 应在本项目中被视为 **动态候选扩张与持久 workspace 的 scaling layer**，而不是核心因果比较的默认组成。

### 2.2 filesystem-memory 的真正启示

【论文报告】filesystem_memory.pdf 将系统分为 manager、searcher、executor。其结果需要窄化解读：

- 在固定 searcher 的对话记忆设定里，builder 变化没有稳定改变最终正确率，而 searcher 从 nano 到 mini、full 时正确率明显上升。这支持“在该设定中，query-time serving 比 builder 变化更关键”，不是跨任务的普遍定律。
- ALFWorld 使用 140 个 held-out tasks，并只用更早任务构建 leak-free store。强 executor 下 raw Episode Log 为 87.1%，Curated Skills + Memory + query-time guidance（GS）为 82.1%，但 paired sign test 约 p=0.23，只能说 raw 在点估计上领先；弱 executor 下二者分别为 66.4% 与 76.4%，GS 净多通过 14/140，约 p=0.02。
- 弱 executor 的 raw episode invalid-action rate 从强 executor 的 19% 升到 36%，而 query-time guidance 从 12% 升到 22%。这说明 raw 与 curated 的优劣取决于 executor，而不是“raw 总好”或“curated 总好”。
- 弱 executor 下，普通 Curated Skills 为 65.7%，Curated Skills + Memory 为 55.0%，都没有复现 GS 的 76.4%。因此优势不能归因于“curation 本身”，而是离线蒸馏与 query-time 筛选、重述、警告整合的联合机制。
- 在弱 executor 的完整链条里，提高 builder（论文中的 curator）能力可把结果从 75.0% / 76.4% 提升到 89.3%，提示 curation 本身也可能有能力阈值。这里的“弱”只指 executor；query-time searcher 仍是较强配置，不能外推为所有角色都弱的系统。

【本报告推论】因此核心设计不是 raw vs skill 的一维比较，而是：

> **representation（raw / curated / dual-layer） × serving mechanism（one-shot / interactive DCI / DR-DCI workspace） × executor strength（weak / strong）**

新的可证伪问题是：

> **DCI 是否降低弱 agent 使用 raw trajectory 的能力阈值？**

如果弱 executor 在相同证据池下只在 searcher-generated cited memo 条件受益，而直接读取 raw 无益，这支持“serving mechanism 缓解能力阈值”。如果强弱 executor 都只有 full trajectory 条件有效，则更像答案复用，而不是程序性经验迁移。

---

## 3. 两个研究任务，不能互相替代

| 研究任务 | 输入与目标 | 合适 benchmark | 能回答 | 不能回答 |
|---|---|---|---|---|
| **A. Search-interface / evidence localization** | 在固定文档或知识库中找到、组织并引用当前问题所需证据 | τ³ banking knowledge、BrowseComp-Plus | one-shot、DCI、DR-DCI 的搜索与 memo 机制是否有效 | 历史成功/失败 trajectory 是否能迁移到新任务 |
| **B. Historical experience transfer** | 只从 train historical trajectories 建库，帮助 held-out task | AppWorld、严格时间切分 SWE-bench-Live；后续 TAC、WebLINX、GUI 等 | raw / curated / dual-layer 的跨任务与跨域迁移 | 单纯的文档问答搜索能力 |

任务 A 中，τ³ 的 terminal_use 只是在静态 banking knowledge 上搜索；BrowseComp-Plus 也以固定文档集合回答查询。它们可以验证 evidence localization 和 persistent workspace，却不能作为“历史经验迁移成功”的证据。任务 B 必须保证 memory episode 与 evaluation task 在 task、时间或 scenario 上隔离。

---

## 4. 外部 benchmark 核验与项目角色

**边界声明：** 下表是截至 2026-09-02 对官方仓库、dataset card 与提交入口所做的 desk check，不是下载后逐文件审计。前三个事实列只写【官方/来源事实】；最后一列才写本项目的【提议实验】。动态或 gated 数据集的规模、字段、许可与可见性都必须在使用前固定具体 revision。除已单列的 SWE h20 资产外，**不得把任何候选写成 h20 已有数据**。

| Benchmark | Trajectory / 数据【官方/来源事实】 | 官方 split / 自然边界【官方/来源事实】 | 访问、许可与环境【官方/来源事实】 | 本项目角色与隔离【提议实验】 |
|---|---|---|---|---|
| **AppWorld** | 750 tasks = 250 scenarios × 3；9 apps、457 APIs；leaderboard 公开部分提交产物 | train 105 tasks / 35 scenarios；dev 60 / 20；test_normal 168 / 56；test_challenge 417 / 139；challenge 含训练未见 app | public 与 protected 文件均按 Apache-2.0 发布；protected 内容若公开再分发须按要求保持加密。在受控环境内训练或生成模型输出本身不等于再分发，但仍受 benchmark protocol 与访问条款约束 | 第一跨域主实验：只 ingest train；dev 调参并冻结；test_normal / test_challenge 只执行一套冻结的 aggregate-evaluation schedule，challenge 为主 OOD 终点 |
| **τ³-bench** | 当前 repo 主线称 τ³-bench v1.0.1；terminal_use 搜索静态 banking knowledge，不是历史 trajectory；Pass^k 是重复 rollout 聚合 | airline 30/20、retail 74/40、telecom 74/40 train/test；banking knowledge 没有对应官方 memory split | 需要 sandbox/runtime 和固定 knowledge revision；v1.0.1 更改 banking grading，跨版本结果不可直接比；新增 DCI/memory 属 custom submission | 任务 A 接口 sanity check；trajectory transfer 需另建 task-disjoint train→test 设计 |
| **TheAgentCompany** | 175 个任务镜像（task images），每个任务使用独立 Docker image；experiments repo 公开 results、screenshots 与 json.gz trajectories | 无官方 train split | 主环境需要 Docker、多项服务和模型凭证；experiments repo 未见独立许可证，不能从主 repo 代码许可推导 trajectory 可训练；运行时长与成本不是固定常数 | 许可通过后按 task/app/workflow cluster 冻结自定义 split；按选定配置实测时长、失败率与成本 |
| **HAL** | 26,597 是 9 个 benchmarks 的 aggregate rollout counter，不是统一 schema、统一许可的独立训练样本数 | 沿用各来源 benchmark 自身 split；不同 benchmark rollout 不构成可随机行切分的同一总体 | harness / 榜单已暂停或归档；traces 动态、体量大，dataset card / 许可需逐 benchmark 核查 | 审计后只作 train-only supplement，不作主 benchmark |
| **BrowseComp-Plus** | 830 queries、约 100,195 份固定文档；官方脚本指向四组 baseline runs：BM25/Qwen3-Embed × GPT-5/o3，不代表所有 leaderboard 条目 | 固定 query / corpus / qrels；同 query 的公开 run 与评测题同源 | query 与 corpus cards 标注 MIT；query/answer/qrels 带 canary 并以混淆形式分发，解密内容不得明文在线发布；runs 也是独立的加密资产，须另 pin revision 与用途；第三方网页内容权利另行审计 | 任务 A 主机制集；公开同题 run 不进入同题 memory，明确区分 document search 与 ExperienceBook search |
| **SWE-bench-Live** | submission 原则上要求 raw trajectories，政策例外可提交代表样本，故历史 coverage 仍需逐 submission 审计 | 官方提交集持续更新，没有天然 train-trajectory→future-task memory split | submission policy 限制 rollout 可见信息；模型输出条款、格式和覆盖率需按 submission/revision 核对 | 用修复公开时间 cutoff 建 pristine temporal confirmatory；任何同题公开 trajectory 即使 inference-only 也按泄漏 |
| **Terminal-Bench 2.0** | 89 tasks；h20 当前没有 Terminal-Bench；leaderboard archive 是动态仓库，不能用未 pin 的单一体积数描述 | 官方 tasks 不是 trajectory memory split | 旧提交入口已迁移；容器执行成本较高。使用 archive 时固定 dataset revision，并分别记录 repository storage、实际 download bytes 和 expanded bytes | Phase 4 按 task family / image ancestry 隔离的终端域确认 |
| **SWE-bench Pro** | 公开 metadata 为 731 rows / 11 repos；metadata 内 patch、test_patch、FAIL_TO_PASS、PASS_TO_PASS 与测试选择均属于 gold-bearing fields；官方 trajectory 需从 S3 另取。h20 的 trajectory 侧仅 README + 9 eval JSON，无 teacher trajectory | 没有可直接复用的官方 trajectory-memory split | S3 访问、数据与模型输出使用权需独立审计；ScaleAI dataset card 与具体 revision 必须一并 pin | 先以字段 allowlist 删除全部 gold-bearing fields，并将原始 metadata、test selection 与 evaluator 物理隔离；之后才可按 repo、时间、ancestry 做代码域扩展 |
| **WildClawBench** | desk-check 快照为 720 rows、60 task IDs、12 models、约 8.86GB；compact parquet 不含完整图片或 score，完整 archive 另含 session/score | 同一 task ID 对应多个 model rollout，没有天然独立行 split | card 标注 MIT；完整图片、score 与第三方模型输出仍须按 pinned revision 审计 | Phase 1 ingestion smoke test；按 task ID 切分，不能按 720 rows 随机切 |
| **WebLINX** | 约 2,300 demos、约 100K interactions；processed card 的 79,777 rows 跨多个 config 与 split label，且 test / test_iid 是 alias 重复，不能当作 79,777 个独立样本 | 官方提供 IID 与 category、geography、visibility、website 等 OOD labels | CC BY-NC-SA 4.0；文本/DOM 离线处理成本较低，网页内容与敏感字段仍需清洗 | 轻量 web OOD / 离线 serving 机制证据；按 demo 与 website 分组，不宣称 live web 端到端迁移 |
| **WebArena / VisualWebArena** | WebArena 公开 179 个 human trajectories 和 Playwright traces；VisualWebArena 公开 GPT-4V+SoM agent 在完整 910-task set 上的 910 条 trajectories，以及按 template type 取样的 233 条 human traces，后者不是完整人类覆盖 | 两者都没有天然 memory train/test split | 代码为 MIT；trace 中的网页内容、截图和第三方资产仍需另审；需要自托管网站、浏览器状态与 Playwright，视觉版本另有截图存储和视觉模型成本 | 按 task/site/template cluster 隔离的后置 GUI/web confirmatory |
| **OSWorld-Verified / 2.0** | Verified card 在 desk check 时列出 1,000+ episodes、15+ variants、约 500GB，含 screenshot/action/reasoning/metrics，并给出“不推荐用于训练”的使用建议；这不是 MIT 许可证禁令 | 没有天然 memory split；OSWorld 2.0 提供版本化 release | Verified trajectory card 标注 MIT，但仍含污染和第三方模型输出风险。OSWorld 2.0 code 为 Apache-2.0；本报告固定 release osworld-v2-2026.08.08，tasks/assets 为 gated；VM、网站、GitLab 环境成本高 | 本项目治理上将 Verified trajectories 设为 evaluation-only；OSWorld 2.0 仅作高成本后置 confirmatory，并 pin release、镜像、task asset 与 evaluator |
| **General AgentBench** | desk-check 快照有 8,653 cleaned trajectories：τ 984、SWE 747、terminal 1,429、math 1,324、search 3,270、MCP 899；过滤 1,445 incomplete；多次 pass 是同题重复 rollout | 没有统一官方 memory split；各来源 benchmark 边界不同 | gated access；dataset license 不明确；eval_details 可能含 SWE gold_patch、math golden_answer、search ground_truth | 许可通过后以字段 allowlist 清洗，并按 benchmark + task ID 分组；只作 train-only multi-domain supplement |

### 4.1 AppWorld 的使用规则

【提议实验】

1. train 的 35 scenarios / 105 tasks 是唯一进入 immutable evidence layer 的 AppWorld split。
2. dev 的 20 scenarios / 60 tasks 只用于选择 schema、预算、query API、builder/searcher 与 arm 配置；一旦冻结，重建同一版本的 book，记录所有 dev 决策。
3. test_normal 的 56 scenarios / 168 tasks 用于同分布最终评测；test_challenge 的 139 scenarios / 417 tasks 包含训练未见 app，是第一跨域主结论。两者各执行一套在看 test 前冻结的 task×seed schedule；不得按单题结果追加 seed。
4. test 只由隔离 evaluator 读取；solver、builder 和 searcher 不挂载 test ground truth、gold program 或公开同题 submissions。
5. 若官方规则只允许 aggregate test feedback，则不依据单题 test 错误继续调参；冻结 schedule 是唯一主结果，不做 outcome-triggered 或单题自适应重跑。不可避免的恢复性重跑须另列 post-hoc，不能替换主结果。

### 4.2 τ³-bench 的使用规则

【提议实验】固定 τ³-bench v1.0.1、domain 数据、knowledge hash、LLM 与 trial 数。在 banking 上比较 direct fixed evidence 与 searcher-generated cited memo；terminal_use 只算知识库接口。Pass^k 作为重复 rollout 的聚合指标报告，每个 rollout 保留 task ID 和 seed，但统计推断以 task 为 cluster，不能把 k 次尝试当 k 个独立样本。任何新增 DCI、memory 或工具都按 custom submission 披露。

---

## 5. 三层 ExperienceBook 与威胁模型

【提议实验】本节定义尚未实施的目标架构，不表示 h20 已按此重构。

### 5.1 Layer 1：immutable sanitized patchless evidence

每个 episode 是不可变、内容寻址的训练集证据单元，最小 manifest 包含：

- benchmark、版本、source URL、artifact hash、task ID、repo/app/domain、split；
- episode / rollout ID、模型、生成时间或可证明时间边界、outcome 与 outcome 证据；
- sanitizer 版本、删除字段、risk flags、许可 / access / redistribution decision；
- 与当前 evaluation universe 的 task、时间、repo、scenario、template 和 ancestry 关系。

内容保留问题理解、诊断、查询、失败分支、工具 observation 和可迁移操作模式，但不保留 final diff、gold answer、hidden evaluator 信息或可重建答案的 payload。原始受限资产保存在独立归档域；主实验只挂载清洗后的 patchless evidence。

### 5.2 Layer 2：rebuildable skills、cards、index 与 warnings

这一层完全可由 Layer 1 和固定 builder 配置重建：

- skill：抽象的可复用策略与适用条件；
- evidence card：来源 episode、局部 span、成功/失败信号和反例；
- index：repo/app/domain、error signature、API、tool pattern 与 event-window 索引；
- warning：已知失败模式、无效动作、环境前提、相互冲突的经验；
- retrieval metadata：只含 train-side 字段，不含 gold 或 future label。

builder 的模型、prompt、版本和随机种子全部 pin。builder 不能决定最终 test 候选池，也不能访问 held-out outcome。任何 builder 变化都生成新 book version，而不是原地修改。

### 5.3 Layer 3：task-local cited memo 与 persistent DR-DCI workspace

推理时 searcher 只能通过窄、可审计的 API 搜索 Layer 1/2，并在任务私有 workspace 中生成：

- query log；
- 引用到 episode ID + span hash 的 evidence snippets；
- 冲突与不确定性；
- task-local action memo；
- 在 DR-DCI 条件下，动态 pull 后持久化的候选窗口和跨 episode 比较。

memo 不是新的全局记忆；任务结束后销毁，只有审计日志进入只读结果域。不同 test task 之间不共享 memo，防止跨测试在线学习。

### 5.4 所有历史轨迹都是 inert untrusted text

trajectory 中的 system prompt、命令、网页内容和 tool observation 一律视为不可信数据：

- 不自动回放命令、不解释为当前系统指令；
- 不允许通过 trajectory 扩大工具、网络或文件权限；
- 对 prompt injection、secret、PII、控制字符、路径穿越和超长 payload 做静态过滤；
- searcher 只能返回带来源的文本窗口，solver 的执行工具与 book 工具分权；
- canary 文件验证 solver/searcher 不能访问 evaluator、raw archive 或其他 test workspace。

---

## 6. 核心实验：六臂、两档 executor

【提议实验】本节全部 arm、因子与消融均为预注册设计，尚未运行。

### 6.1 六个主 arm

在每个 executor tier 内，所有 arm 使用相同 held-out tasks、solver backbone、harness、候选证据池、exact K、temperature / decoding、task×seed schedule、最大 book exposure、总 inference-token / tool-call / wall-time ceiling 和 evaluator。对 arm 3–5，先冻结相同的 candidate set；只有 arm 6 允许动态候选扩张，并单独核算扩张预算。

| Arm | 表示 | Serving mechanism | 目的 |
|---|---|---|---|
| **A0 No memory** | 无历史证据 | repo / environment only | 估计同一 executor 的基线 |
| **A1 Static skill** | Layer 2 curated skill / warning | 一次性注入或固定检索结果 | 复现传统 distilled-skill 路线 |
| **A2 Candidate-matched raw one-shot** | Layer 1 sanitized patchless raw | 一次性给定固定候选窗口 | 控制“看到了哪些 episode”后的直接上下文基线 |
| **A3 Candidate-matched raw DCI** | 与 A2 完全相同的 raw candidate set | 迭代 search / grep / read / compare，并生成引用 memo | 端到端 interactive 条件；interface 归因由 A2-CM 消融完成 |
| **A4 Dual-layer DCI** | Layer 1 raw + Layer 2 skill/card/index/warning | 迭代搜索，两层证据共同生成引用 memo | 检验抽象与原始证据是否互补 |
| **A5 Large-scale dual-layer DR-DCI** | 大规模 Layer 1 + 2 | retriever-steered dynamic pull、persistent workspace、必要时受控 context reset | 检验规模化候选扩张与持久 workspace；不是用来替代 A2-CM↔A3 的核心机制比较 |

“candidate-matched”必须落实为逐 task 的候选 episode ID 清单和内容 hash 完全一致，但这还不足以把 A3−A2 归因于 interface：A3 额外包含迭代搜索工作流与相应计算。A3−A2 只报告端到端 effectiveness / cost 差异；交互式 serving 的隔离归因依赖下文的 A2-CM。A4 相对 A3 的差异是增加 rebuildable abstraction layer。A5 的动态 expansion 是独立 scaling 因子。

### 6.2 两档 executor

每个主 arm 至少运行两档执行器：

- **Weak executor：** 成本较低、工具规划较弱的固定模型配置；
- **Strong executor：** 能力更强但仍使用同一工具契约、环境和安全边界的固定配置。

不以模型名称定义“弱/强”的理论含义，而以预注册配置和 A0 能力差异确认操纵是否有效。核心统计交互是 Arm × Executor，重点比较：

- 弱 executor：A3 − A2-CM，交互定位是否帮助消费 raw；
- 强 executor：A3 − A2-CM，是否存在同样或更小的 mechanism 增益；
- 两档 executor：A3 − A2，端到端 accuracy–cost 差异，不作单独 interface 归因；
- 两档 executor：A4 − A3，curated layer 是否主要帮助弱执行器；
- A5 − A4：动态扩张是否只在大 corpus 和检索 recall 不足时有价值。

### 6.3 必做的机制消融

1. **A2-CM：compute-matched non-interactive memo。** A2-CM 不是第七个主 arm，而是 A2/A3 之间的预注册机制消融。它与 A3 使用相同候选 raw、同一 analyst/searcher backbone，以及相同的总 inference-token、tool-call、wall-time 和 book-exposure ceiling。A2-CM 一次性接收预先固定导出的候选窗口并生成 cited memo，但不能交互调用 grep / read / expand；A3 可在同一 ceiling 内迭代定位并生成同格式 memo。A3−A2-CM 是交互式 serving 的首要 mechanism contrast；A3−A2 只作为真实部署工作流的端到端 effectiveness / cost contrast。两者都记录实际用量，ceiling 匹配不等于假定 realized cost 相等。
2. **Builder / searcher 分离。** 固定 executor，交叉较弱/较强 builder 与较弱/较强 searcher；builder 只离线重建 Layer 2，searcher 只处理当前 task。避免把 searcher 增益误记为更强 builder。
3. **Raw / full / patchless。**
   - patchless sanitized raw 是主实验；
   - full trajectory 只作为受控、隔离的泄漏敏感上界，不进入正式无泄漏主结论；
   - 若 full 显著有效而 patchless 无效，优先解释为答案复用或 edit payload 依赖。
4. **Success / failure / mixed evidence。** 在不看 test outcome 的前提下按 train-side outcome 构造成功、失败和混合 book；验证 warnings 与反例是否贡献。
5. **Static candidates / dynamic expansion。** 只在 A5 打开；记录每次 pull 的触发、候选增量、bytes、tokens 和最终引用。

---

## 7. Split、泄漏和物理隔离协议

【提议实验】本节是目标隔离与治理协议；当前 h20 caveat 仍以第 1.5 节的已观察状态为准。

### 7.1 Split 必须先于一切下游处理

每个 benchmark 按自然 split 优先；无自然 split 时按 task ID 与 near-duplicate / template / ancestry cluster 分组。根据域选择 scenario-disjoint、repo-held-out、app-held-out、website-held-out 或 temporal holdout。冻结顺序为：

1. 解析原始 task metadata；
2. 构建 task 与 near-duplicate cluster；
3. 冻结 train / dev / test manifest 和 hash；
4. 物理移动或挂载隔离；
5. 才允许 sanitizer、builder、indexer 与 searcher 读取 train。

同一 task 的多模型、多 seed、多 pass rollout 必须全部留在同一 split。公开同题 trajectory、solution、screenshot、tool trace、evaluation log 或 task-specific skill，只要 solver/searcher 可读，即使在 inference-only 阶段也算 test leakage。

### 7.2 目标文件系统

| Namespace | Solver | Searcher | Builder | Judge |
|---|---|---|---|---|
| /workspace/current_task | 读写 | 只读必要视图 | 不可见 | 评测后只读 |
| /book/frozen_train | 不直接挂载或只经 API | 只读 | 构建完成后只读 | 不需要 |
| /memo/task_id | 只读 | 读写 | 不可见 | 归档审计 |
| /raw_archive | 不可见 | 不可见 | 只读受限 | 不可见 |
| /evaluator | 不可见 | 不可见 | 不可见 | 只读/执行 |
| /results/task_id | 只写 final artifact | 只写审计 log | 写 build manifest | 写 verdict |

默认无网络；网络需求必须按 benchmark allowlist。容器以非 root 运行，book mount 只读，judge 在 solver 退出后进入独立 namespace。每次运行放置随机 canary，若 solver 或 searcher读到 evaluator / raw archive canary，则该批次全部作废。

### 7.3 版本与许可登记

每个 run manifest 固定：

- benchmark release、repo commit、dataset revision、下载时间和文件 hash；
- task manifest、split algorithm、near-duplicate cluster version；
- sanitizer、builder、index、searcher、solver prompt 和模型 snapshot，以及 builder / searcher / solver 的 temperature、decoding 参数、随机种子、exact K 与完整 task×seed schedule；
- container / VM image、OS、依赖 lock、tool 版本、evaluator 与 golden-test hash；
- dataset/code/model-output 许可、访问条件、训练许可、再分发许可，分别记录；
- 过去对 task 内容、boolean outcome、public submission 的任何 exposure 及其 decision lineage。

许可清单只有 allow / deny / evaluation-only 三种可执行状态。状态不清的资产默认 deny，不进入 book；“公开仓库”“可下载”“代码 MIT”都不能替代对 trajectory 和模型输出的单独判断。

---

## 8. 指标、统计和成本

【提议实验】本节是未来 run 的计量和统计方案，不是已产生的实验结果。

### 8.1 三阶段机制指标

每个 task 分开记录：

1. **Episode reach：** useful episode 是否进入候选集；recall@budget；
2. **Span localization：** solver 最终 memo 是否引用 useful span；引用 precision/recall、无来源断言率；
3. **Execution conversion：** 引用的经验是否转化为通过 evaluator 的当前解法。

这能区分“检索不到”“定位不到”和“看到了但不会用”。任务 A 以证据定位与答案正确率为主；任务 B 以环境成功 / resolved / pass@1 为主，并报告前两阶段作为机制中介指标。

### 8.2 Benchmark-specific outcome

- SWE：官方 resolved 判定与 FAIL_TO_PASS / PASS_TO_PASS；主指标 pass@1。
- AppWorld：官方 task / scenario 指标，按 scenario 聚类；test_challenge 单独作为 OOD 主终点，不与 normal 混成一个数字。
- τ³：官方 success 与 Pass^k；同时报告每个 task 的 fresh-trial 成功模式。
- BrowseComp-Plus：官方 answer correctness，以及 gold-document coverage、evidence localization 和引用有效性。
- GUI / web：官方 task success，另报环境启动失败、超时和 evaluator 不确定性。

### 8.3 统计计划

- 统计单位先在 task 层形成，随后按 benchmark 的自然依赖结构聚类；同 task 的 seeds、models、passes 和 trajectories 是重复测量，不是额外独立样本。
- 所有 arm 共享完全相同的 task×seed schedule；若调用不能确定性复现，则以 paired seeds 作为 task 内重复测量，禁止依据中间结果自适应增加 seed 或重试。
- 当 K=1 时，二元 paired outcome 可以使用 McNemar，但只把它作为预指定的单-seed 敏感性分析；primary effect 仍报告 task-level paired mean difference 与相应的 cluster-aware CI。
- 当 K>1 时，primary outcome 是每个 task 在 K 个 seeds 上的平均 success。先计算每个 task 的 arm 间 paired mean difference，再按 AppWorld scenario、一般 task cluster、SWE repo 或 WebLINX demo / website 做 cluster bootstrap；也可使用预注册的 repeated-measures model，将 seed 明确嵌套在 task 内。不得把 task×seed 行当作独立样本。
- K>1 时不得对 K-seed 平均值使用 McNemar；McNemar 只允许用于事前指定的一个 seed 的二元敏感性分析。
- 多 benchmark 汇总用 benchmark / domain 随机效应的分层 logistic model，同时始终给出每个 benchmark 的原始分子、分母与 CI；不只报 pooled aggregate。
- AppWorld 按 scenario cluster bootstrap；SWE 另按 repo cluster 做稳健性分析；WebLINX 按 demo / website cluster。
- Arm × Executor 为预注册主交互；A3−A2-CM 是首要 mechanism contrast，A3−A2 是端到端 effectiveness / cost contrast，A4−A3 与 A5−A4 为次要 contrast。
- 多重比较仅对预注册的有限 contrasts 使用 Holm 校正；探索性结果明确标记，不反向修改主假设。
- 失败、超时、环境启动失败、evaluator crash 分开计数；intent-to-treat 主分析把超时视为失败，另给可运行子集敏感性分析。

### 8.4 公平预算与成本

每个 arm 同时记录并约束：

- solver input / output tokens、searcher tokens、builder amortized tokens；
- book exposure 的 episode 数、bytes、rendered tokens、preview 数和 materialized windows；
- search / grep / read / pull 次数，命令或 API 失败率；
- wall time、container / VM 启动时间、CPU/GPU 小时、存储与网络量；
- 按固定价格快照计算的模型成本，并把一次性建库成本与每任务 inference 成本分开。

报告 accuracy–cost frontier，而不只报最高 accuracy。若某 arm 超预算，仍按 intent-to-treat 计入主结果；不得在失败后给它额外上下文或工具额度。

---

## 9. 实施阶段与 go / no-go gate

### Phase 0：修复 h20，建立可信地基

【提议实验】

- 统一配置真实路径，启动时验证必需目录非空；
- 为 734 trajectories、731 skills 和所有 outcome 建 provenance manifest；
- 冻结 task universe、版本和三种 untouched 口径；
- 实现跨 channel sanitizer、patch reconstruction audit 与 injection / secret canary；
- 把 runner、book、raw archive、judge 做物理 namespace 隔离；
- 对 evaluator snapshot 不一致和所有历史 label exposure 建 decision-lineage ledger。

**G0 go 条件：**

- 纳入 book 的 episode 100% 有 source、split、license-state、hash 和 sanitizer record；
- sanitizer 测试集中 gold/test patch、secret、control instruction 的阻断率为 100%，并人工抽审误删；
- 隔离 canary 访问为 0，judge 路径对 solver/searcher 不可见；
- 空路径、版本漂移、manifest mismatch 均 fail closed。

任一条件不满足则 no-go：只能继续数据工程，不能运行可发表效果实验。

### Phase 1：SWE + WildClaw ingestion pilot

使用 h20 touched SWE 任务验证 schema、工具日志和“六个主臂 + A2-CM 消融”runner，不把结果称为无泄漏泛化。按 WildClaw task ID 选小型合法样本，验证图片引用、session、score 和多模型重复 rollout 的归一化；compact parquet 只做轻量输入，不能假设包含完整图片/score。

**G1 go 条件：**

- schema round-trip 后 episode、span citation 与原 source hash 一致；
- 同 task 的 12-model rows 不跨 split；
- A2、A2-CM 与 A3 的 candidate IDs 和 source hashes 完全匹配；A2-CM / A3 使用同一 analyst/searcher backbone、相同 ceilings 与同一 task×seed schedule，且日志证明 A2-CM 没有交互 grep / read / expand；
- 运行失败可被归因到 solver、searcher、environment 或 evaluator，而不是混成一个 FAIL。

否则 no-go：不下载更大 archive，不进入跨域主实验。

### Phase 1.5：AppWorld train nested pilot 与 rollout schedule 冻结

在查看任何 AppWorld dev / test 内容或结果之前，只在 AppWorld train 的 35 个 scenarios 内做 scenario-grouped nested pilot：每个 fold 只用其余 train scenarios 构建 book，以 held-out train scenarios 估计 arm discordance 和 seed variance；fold 内绝不把 held-out scenario 的 trajectory 写入 book。SWE / WildClaw pilot 的跨域方差不能用来保证 AppWorld 精度。

nested pilot 形成一份签名、内容寻址的 prospective Monte-Carlo precision / power memo：

- primary estimand 固定为弱 executor 上 A3−A2-CM 的 task-level paired difference；当 K>1 时，每个 task 先对 K 个 paired seeds 的 success 取平均，再在 task / scenario 层聚合；
- K 只根据 nested pilot 的 seed variance 冻结，目标是 primary paired difference 的 Monte-Carlo standard error 不超过 1 percentage point；
- 若 API 在重复验证中可确定性复现，则冻结 K=1；否则冻结满足上述 MC-error 目标的一个确切整数 K，并同时固定 temperature、decoding、solver seeds、searcher seeds 及其配对映射；
- memo 在求解 K 前同时冻结总推理成本 ceiling；若没有整数 K 能同时满足 MC-error 与成本 ceiling，则在打开 dev 前 no-go；
- 所有 arm 使用完全相同的 task×seed schedule，不做按 arm、按 task 或按中间结果的自适应重试；
- memo 在 dev 解封前记录基于 nested pilot 或保守 variance bound 投影的 cluster-CI width、80%-power MDE、exact K 和成本；这些投影只用于事前 go / no-go 与预算判断，不能在看到 dev / test 后调整。

test_challenge 的 sampling uncertainty 主要由 139 个 scenarios 及其异质性决定，不能靠增加 K 消除；本设计不承诺最终 CI half-width 小于某个固定值。最终报告必须给出实际 scenario-cluster CI。没有该 memo、配置 hash 与 schedule hash 就不得开始 AppWorld dev。最终“一次主评测”指完整执行这一冻结 schedule，而不是任意单 seed 或一次临时 API 调用。

### Phase 2：AppWorld train→dev→test 主跨域实验

只用 train 建 Layer 1/2；在 dev 完成 executor 档位、预算、六臂和消融冻结。冻结后重建最终 train-only book，对 test_normal 与 test_challenge 各执行一套已冻结的 task×seed 主评测 schedule。test_challenge 是第一 OOD 主终点。

**Dev-only oracle-evidence 操作化：** oracle 只用于 G2 诊断，不是 test arm。候选证据宇宙严格限于 AppWorld train。annotation protocol、relevance rubric、annotator / adjudication 规则、packet token / byte 预算和生成代码在查看任何 dev outcome 前冻结；blinded annotator 可读取 dev task statement 与 sanitized train evidence，但看不到 dev outcome、gold program、评测日志或其他 arm 结果。每个 dev task 在任何 solver run 前生成一个同预算、不可变且同时记录 packet hash 与 source hashes 的 oracle candidate packet。test_normal / test_challenge 不构建、也不读取 oracle mapping。

**G2 go 条件：**

- provenance completeness、A2/A2-CM/A3 candidate 与 source-hash matching、leakage-canary pass 和 citation-resolution validity 均为 100%；test asset 在冻结前从未挂载到 builder/searcher；
- 预算合规率与非 evaluator 判定导致的环境完成率均至少 95%；
- 在 dev 上，上述冻结 oracle candidate packet 条件相对 A0 的 task-level paired point difference 至少为 +5 percentage points；
- 在预注册的 dev 标注子集上，searcher episode reach@budget 至少 70%，cited-span precision 至少 90%；
- A0(strong executor) 的 dev success rate 严格高于 A0(weak executor)，确认能力档位排序有效；
- Phase 1.5 的 precision / power memo、exact K、temperature / decoding、完整 task×seed schedule，以及 book、runner、evaluator 和配置 hashes 全部冻结后，才可解封 test。

任一阈值未达即 no-go，不得以“A3 显著”或“瓶颈看起来可修复”作为主观例外。若 oracle-evidence 增益低于 +5 points，停止扩张 memory；若 oracle 达标而 A3 无益，优先修 search/localization；若 A3 有益而 A5 无额外收益，不进入大规模 DR 扩张。

### Phase 3：τ³ mechanism + SWE-bench-Live temporal confirmatory

- τ³-bench v1.0.1：执行任务 A，比较 fixed evidence direct、DCI cited memo 与 DR workspace；明确这是知识库搜索，不是历史 trajectory transfer。
- SWE-bench-Live：按修复公开时间建立 train-before-cutoff book 和 post-cutoff pristine set；逐 submission 排除同题 trajectory，完成编码域的任务 B confirmatory。

**G3 go 条件：**

- τ³ 的首要机制结果使用 compute/exposure-ceiling 匹配的 A3−A2-CM，并能分解为 episode/document reach、span localization 与 execution；A3−A2 仅报告端到端 accuracy–cost，不把额外搜索计算误归因于 interface；
- SWE-Live cutoff、task hash、public-submission exposure audit 全部在运行前冻结；
- 主结论至少在 AppWorld OOD 或 SWE-Live temporal confirmatory 中成立，且不存在 full-only / patchless-fail 的答案复用模式。

若只在任务 A 成立，结论必须收窄为“查询接口改进”，不能宣称 historical experience transfer。

### Phase 4：按 gate 扩展，不同时铺开

优先级按研究增量与成本决定：

1. SWE-bench Pro 或 Terminal-Bench 2.0：代码/终端环境扩展；
2. TheAgentCompany：真实办公工作流；
3. WebLINX：低成本文本/DOM OOD；
4. WebArena / VisualWebArena、OSWorld：高成本 GUI confirmatory；
5. HAL、General AgentBench：许可与 gold-field 审计后的 train-only supplement。

WebLINX 可以在 Phase 2 后提前，用于快速检查 web/DOM 上的 offline serving mechanism；报告必须标注它是离线机制证据，而不是对 live web agent 的端到端成功证明。

**G4 资产准入条件：**

- 许可明确允许拟议用途，且模型输出/截图/网页内容分别通过审计；
- 自然 split 或 task-cluster split 可防止同题 rollout 跨界；
- evaluator、container / VM 和数据 revision 可 pin；
- 预估成本在预算内，且该 benchmark 能提供现有结果没有覆盖的新机制信息。

未同时满足四项的资产保持 evaluation-only 或 deny。

---

## 10. 预注册 falsifiers 与解释边界

| 观察结果 | 对主张的影响 | 后续决策 |
|---|---|---|
| patchless A3 不优于 compute-matched A2-CM | DCI-style iterative serving 的核心机制主张失败 | 不用 A3−A2 的额外计算或 DR 扩张掩盖；报告零结果并检查 localization |
| A3−A2-CM 只对 strong executor 为正 | “降低弱 agent 能力阈值”被证伪 | 收窄为强执行器的交互定位增益 |
| 弱 executor 仅 A4 / cited memo 有效 | 支持 query-time guidance 或 curation 缓解阈值 | 进一步用 builder/searcher 交叉分离来源 |
| 只有 full trajectory 有效，patchless 无效 | 更像 final-patch / answer reuse | 不宣称程序性经验迁移 |
| oracle useful episode 也不改善 task success | historical experience 对该域没有可用信号 | 停止扩大 corpus，转向任务表示或完全不同机制 |
| A3−A2-CM 提高 localization 但不提高执行成功 | bottleneck 在 executor conversion | 不把搜索指标当最终任务收益 |
| A5 相对 A4 无益或成本急升 | 动态扩张 / persistent workspace 不必要 | 保留较简单的 dual-layer DCI |
| τ³ / BCP 有益，AppWorld / SWE-Live 无益 | 只证明任务 A，不证明任务 B | 标题和结论收窄为 evidence localization |
| AppWorld dev 有益、test_challenge 无益 | 跨 app/domain 泛化失败 | 报告分布内增益，不合并 normal/challenge |
| 收益在 task/repo/scenario 聚类后消失 | 重复 rollout 或单域驱动 | 以聚类推断为准，不使用行级显著性 |
| 任一 public same-task trajectory 被读取 | 该 task 的无泄漏结论失效 | 预注册规则剔除整 cluster，记录 exposure |
| arm 间候选 evidence 或预算不匹配 | 无法归因于 serving mechanism | 该 contrast 作废并重跑，不做事后校正 |

本项目不预设 DCI、raw 或 dual-layer 必然胜出。可发表价值来自：固定证据与预算后，把检索、定位、执行转化和能力阈值拆开；在至少一个真正隔离的跨域或时间 holdout 上验证，或诚实证伪。

---

## 11. 推荐的最小论文叙事

一篇最小而可信的论文不需要同时跑完所有 benchmark。主线可以是：

1. 用 h20 说明旧 same-task rescue 的信号与内部有效性边界；
2. 提出三层 ExperienceBook 和 representation × serving × executor 的机制框架；
3. 在 AppWorld 完成 train→dev→test_normal / test_challenge 的六臂两执行器主实验；
4. 用 τ³-bench 验证 fixed-document search mechanism；
5. 用 SWE-bench-Live 的严格时间 cutoff 做编码域 confirmatory；
6. 以 WildClaw 展示 ingestion 可扩展性，其余 benchmark 作为通过 gate 后的扩展。

最关键的结论格式应是条件化的：

- “在相同候选 evidence、analyst/searcher backbone 与 compute/exposure ceiling 下，A3 相对 A2-CM 的交互定位增益是多少；A3 相对 A2 的端到端 accuracy–cost 差异是多少”；
- “该增益是否集中在弱 executor，是否由 cited memo 中介”；
- “dual-layer 是否优于 raw-only，DR-DCI 是否只在大规模下有额外价值”；
- “机制收益是在任务 A、任务 B，还是两者都成立”。

这比把 h20 的 61G 目录、论文数字和外部可下载 trajectory 简单相加，更能形成可复核、可证伪且不夸大 novelty 的研究贡献。

---

## 12. Primary sources

### 本地论文与方法证据

- Beyond Semantic Similarity: Rethinking Retrieval for Agentic Search via Direct Corpus Interaction（历史文件：`papers/DCI.pdf`，不在精简发布中）
- DR-DCI（历史文件：`papers/Dr_DCI.pdf`，不在精简发布中）
- Filesystem-Based Memory for LLM Agents（历史文件：`papers/filesystem_memory.pdf`，不在精简发布中）

### Benchmark 与 trajectory 一手来源

- [AppWorld repository](https://github.com/StonyBrookNLP/appworld)
- [AppWorld leaderboard repository](https://github.com/StonyBrookNLP/appworld-leaderboard)
- [τ³-bench repository](https://github.com/sierra-research/tau2-bench)
- [τ³-bench leaderboard submission rules](https://github.com/sierra-research/tau2-bench/blob/main/docs/leaderboard-submission.md)
- [TheAgentCompany main repository](https://github.com/TheAgentCompany/TheAgentCompany)
- [TheAgentCompany experiments](https://github.com/TheAgentCompany/experiments)
- [HAL](https://hal.cs.princeton.edu/)
- [BrowseComp-Plus repository](https://github.com/texttron/BrowseComp-Plus)
- [BrowseComp-Plus query card](https://huggingface.co/datasets/Tevatron/browsecomp-plus)
- [BrowseComp-Plus corpus card](https://huggingface.co/datasets/Tevatron/browsecomp-plus-corpus)
- [BrowseComp-Plus baseline-run card](https://huggingface.co/datasets/Tevatron/browsecomp-plus-runs)
- [SWE-bench-Live submission repository](https://github.com/SWE-bench-Live/submission)
- [Terminal-Bench 2.0 leaderboard archive](https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard)
- [Terminal-Bench 2.0 task card](https://huggingface.co/datasets/harborframework/terminal-bench-2.0)
- [Terminal-Bench 2 repository](https://github.com/harbor-framework/terminal-bench-2)
- [ScaleAI SWE-bench Pro dataset card](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro)
- [SWE-bench Pro trajectory README](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/traj/README.md)
- [WildClawBench trajectories](https://huggingface.co/datasets/internlm/WildClawBench-Trajectories)
- [General AgentBench cleaned trajectories](https://huggingface.co/datasets/cx-cmu/agent_trajectories)
- [WebLINX dataset](https://huggingface.co/datasets/McGill-NLP/WebLINX)
- [WebArena resources and trajectories](https://github.com/web-arena-x/webarena/blob/main/resources/README.md)
- [VisualWebArena repository](https://github.com/web-arena-x/visualwebarena)
- [OSWorld-Verified trajectories](https://huggingface.co/datasets/xlangai/ubuntu_osworld_verified_trajs)
- [OSWorld 2.0 official site](https://osworld-v2.xlang.ai/)
- [OSWorld 2.0 repository](https://github.com/xlang-ai/OSWorld-V2)
- [OSWorld 2.0 gated task card](https://huggingface.co/datasets/xlangai/osworld_v2_tasks)

### 使用这些来源时的统一规则

来源链接用于核对规模、split、schema、提交要求和访问入口；真正纳入 book 前，仍以下载时的具体 revision、LICENSE、dataset card、模型输出条款和 benchmark policy 为准。源代码许可证不自动覆盖数据、trajectory、截图、网页内容或第三方模型输出。

---

<a id="retrieval-followup"></a>

## 13. 检索与证据采用改进：首轮之后的讨论稿

**日期：2026-09-08。状态：文献与源码核查完成，方案待讨论；未改实验实现、未启动新版实验。** 本节补充第3节的机制划分、第5节的三层 ExperienceBook 和第6节的对照设计，不重新定义原 A0–A5。WildClawBench 首轮是其中一个端到端 pilot，不等于原多基准框架已经执行完毕。

### 13.1 先回答两个问题：上游提供了什么

**两个仓库都提供了部分解决思路，但没有直接提供完整的“历史轨迹检索与可靠采用”模块。** DCI-Agent-Lite 主要把搜索、阅读和继续查证的选择交给 agent；DR-DCI 增加检索器选取候选、文件落入工作区和持续局部探索。两者研究的主要对象是问答文档；将其用于历史行动经验迁移，需要额外设计。

本次固定核查 DCI-Agent-Lite commit `271f37e71f053bf0c99c05ce6d2fb53b841d922e`，以及 DR-DCI commit `0d0410f3c2b98fb33145adc250a09fded028cd3c`。表内“提示”表示要求模型这样做，不表示程序保证执行；“未发现”限于所审查实现。

| 机制 | DCI-Agent-Lite | DR-DCI | 对当前项目的含义 |
|---|---|---|---|
| 多词和查询改写 | **实现：**原生 bash 可组合多模式、正则、管道和文件范围。**提示：**IR 模式要求多角度关键词 | **实现：**`pull(query, topK)`；可选多 query，但主配置关闭。不同检索后端有各自语义 | 首轮把整个 query 当一个字面串，明显收窄了可组合性；操作词提取、别名和统一 AND/OR 仍需设计 |
| 候选相关性排序 | IR 最终列表由 **agent 排序**；没有独立 ranker | 候选按检索器得分返回；主配置使用 dense/FAISS，也有 BM25 后端 | 排序可以借鉴 DR，但它是额外检索因子，不能称为 Lite 默认已有功能 |
| 去重与去噪 | 可由 agent 写命令筛选；未发现系统级轨迹去噪、任务级多样性或候选近重复合并 | 已拉取文件按规范化来源路径去重；多 query 顺序合并，未见跨 query 全局融合重排 | 路径去重不解决不同模型重复同段内容，也不保证返回的是操作正文 |
| 命中后阅读 | `read` 支持局部读取和续读；仍会截断。IR 提示要求先读再选 | 有行/字符/字节窗口、长行处理及续读；工作区文件可反复检查 | 可以直接借鉴“位置可寻址、可续读”，还需把动作与实际返回结果关联起来 |
| 何时再查 | 持续模型—工具循环已实现；IR 提示按证据缺口继续查 | 提示先检查本地候选，再按缺口 pull；可选 context reset 是另一恢复机制 | 不能把“允许再查”当成“遇阻必查”；本项目仍需定义可观测触发条件 |
| 历史操作是否适用 | 未发现面向历史轨迹的环境适配器或采用验证器 | 同样未发现 | 工具可用性、输入条件、输出要求、证据可靠性检查属于我们的迁移设计 |

一个重要细节：Lite 的五步搜索策略在 `build_ir_prompt`，由 `--enable-ir` 开启；默认 benchmark prompt 较简短。不能将 IR 配置的要求描述成所有 BrowseComp 运行的默认行为。DR-DCI 的路径去重、多 query 和检索排序也不能合称为语义去重或自动改写。[Lite 默认与 IR prompt](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bcplus_eval/run_bcplus_eval.py#L363-L405)、[DR-DCI 固定代码版本](https://github.com/EigenTom/DR-DCI/tree/0d0410f3c2b98fb33145adc250a09fded028cd3c)

逐项源码位置和依赖边界见 [Lite 核查](../docs/research_notes/retrieval_followup/dci_lite_review.md) 与 [DR-DCI 核查](../docs/research_notes/retrieval_followup/dr_dci_review.md)。DR-DCI 的公开论文为 [arXiv:2606.14885v1](https://arxiv.org/html/2606.14885v1)；README 的旧占位文字不能用来判断论文尚未发表。

### 13.2 其他研究中最有用的思路

下表是方法借鉴，不是声称已复现这些系统。问答中的正确证据检索，不能直接证明历史操作可迁移；尤其不能把“搜到了相似内容”作为采用成功的标签。

| 研究与证据层次 | 核心思路 | 我们可以借鉴什么 | 边界 |
|---|---|---|---|
| [SIEVE，2026，论文§3/附录B–C](https://arxiv.org/html/2608.02751v3) | 字段 Boolean 筛选 → 候选排序 → 结构卡片 → 指定 section 读取；零命中时有回退 | 将轨迹的工具名、动作、观察、错误和来源变成可寻址字段；预览命中位置，再读对应事件 | 最贴合本次两个问题；但它处理网页章节，其 DCI 对照的提示词和步数预算与上游不同，不能把对比胜负直接移植到本项目 |
| [IRCoT，ACL 2023](https://aclanthology.org/2023.acl-long.557/) | 推理与检索交替；中间发现影响下一次查询 | 用“当前子目标和未解决问题”更新查询，读到新操作名后再定向搜索 | 采用显式简短任务状态，不要求导出模型隐藏思维链；增加调用需计入预算 |
| [Agentic-R，2026，§4](https://arxiv.org/html/2601.11888v1) | 检索训练同时考虑局部相关性与最终答题正确性 | 区分主题相似与能否推进当前步骤；建立方法有用性的标注 | 原方法用训练答案和续跑构造监督；不能在测试检索时使用答案，也不是只加一个排序提示就完成复现 |
| [ITER，2026，§3–4/§7](https://arxiv.org/html/2608.27912v1) | 区分已返回、已读、有用文档，利用交互历史减少重复证据 | 记录候选阅读状态，将新候选与以前结果分开显示，保留旧结果入口 | “返回但没读”不能直接判无用；其主方法训练了 retriever，简单去重只借鉴了接口 |
| [ExpeL，§4](https://arxiv.org/html/2308.10144v3)；[ReasoningBank，§3.2](https://arxiv.org/html/2509.25140v2) | 从历史成功/失败中提炼经验；ExpeL 同时检索成功示例，ReasoningBank 存储结构化策略记忆 | 构建带来源的操作策略与失败警示，比较 raw-only 和 dual-layer | 改变了表示，属于 A4；ReasoningBank 原有在线写回不适用于本轮冻结测试库，不能照搬 |
| [RISE，2026](https://arxiv.org/abs/2606.06880)及[官方仓库](https://github.com/texttron/RISE) | 先形成有限候选工作区，再通过目录结构和局部读取探索 | 支持把候选范围和内部导航作为两个因子；是 DR-DCI 扩展方向的补充 | 主要解决大 corpus 的探索成本；当前小库未显示扫描预算耗尽，优先级低于命中质量与读取 |

补充线索：[AgentIR](https://arxiv.org/abs/2603.04384) 将搜索前的 reasoning 与 query 联合编码；[FLARE](https://aclanthology.org/2023.emnlp-main.495/) 根据生成置信信号触发检索；[GrepSeek](https://arxiv.org/html/2605.29307v1) 学习可执行的 corpus 搜索策略。它们分别提示“查询需要任务状态”“检索需要时机”“工具之外还需要搜索能力”。本项目可先讨论显式 search brief、可观测阻塞和构建集示例，不在此引入隐藏思维链访问、token 置信接口假设或模型训练。

**查询上下文并非越多越好。** Agentic-R 的配置采用原问题 + 当前 query，并报告加入先前 query 带来噪声；ITER 则在其训练和评测中发现加入先前 query 更稳健，同时加入已读全文或其解释会降低检索召回。两者设置不同，这不是普遍规律冲突，而是需要验证的条件差异。应将 `q`、`Q + q`、`Q + q + 有界查询历史` 作为候选设计，不能默认拼入整段执行历史。[Agentic-R §4.1.2/附录C.2](https://arxiv.org/html/2601.11888v1)、[ITER §7.1](https://arxiv.org/html/2608.27912v1)

更多来源、阅读深度和取舍见 [文献笔记](../docs/research_notes/retrieval_followup/literature_review.md)。2026 年的新预印本仅作为可检验思路；本文不采用其报告的准确率作为本项目的预期收益。

### 13.3 改查询与候选返回：先定义清楚“什么算命中”

**以下为本项目提案，不是上述仓库已经完整实现的接口。** 首选缩小首轮 MCP 与可组合 DCI 的能力差距，并将额外排名作为独立开关。

1. **查询围绕当前操作展开。** agent 提交一个简短 search brief：当前子目标、需要的操作/工具、已知约束、缺失信息。初始 query 可从任务规划产生；执行中则由真实观察更新。不要只拿题材词当操作词，也不要把所有约束一次硬拼成一个长字面串。
2. **明示模式和作用范围。** 区分精确短语、任一词（OR）、所有词（AND）、显式正则；同时声明 AND 是同一行、同一事件块还是同一 episode 内成立。初步倾向在“关联动作与观察的事件块”内作 AND，允许显式放宽到 episode；两种语义不能混用。错误语法返回错误，零命中返回零命中，不静默改解释。
3. **别名来自可审计来源。** 首选工具/API 的确切名称、构建语料已出现的词和任务内新发现的术语。中英文、缩写和同义词扩展应显示原 query、扩展词及来源。工具别名也可能不等价，不能把历史工具名自动映射为当前可用工具。
4. **将可搜索正文与低信息字段分开。** 优先搜索动作、观察、代码和错误；环境清单、目录 URL、包装 JSON 单列为可选字段。只调整检索视图和默认权重，保留完整原文及回查入口。仅依赖确定性解析的结构索引与由 LLM 提炼的语义卡片分开计量。
5. **先去重复出现，再处理候选多样性。** 合并同一事件的多个命中；内容完全相同的展示可合并并保留全部来源。不同模型或同一 family 的不同方法不能直接删成一条，宜分组折叠并允许展开。按任务设候选配额是另一个有待讨论的因素。
6. **返回可解释的候选卡。** 包括 episode/task/model、事件类型、命中字段与精确位置、短正文、匹配了哪些条件、缺少哪些条件、此前是否读过，以及下一步读取参数。来源信息按块给一次，减少重复元数据占用。

例如对一个一般性的“按给定尺寸裁切图像”子目标，可提出以下**概念查询**，不是当前可执行 API，也不是由测试答案反推的 query：

```text
目标：保持比例裁切并导出指定尺寸
候选表达式：(ImageOps.fit OR "crop to fit" OR "resize and crop")
字段：action / observation / code
约束：输出宽高必须能验证；当前工具能力需要单独检查
AND 范围：关联事件块；若改成 episode 范围必须显示说明
```

此例用于解释操作查询，不证明库内已有可迁移解法。一般概念词更利于召回，确切 API 名更利于定位；两者应分轮或显式 OR 组合，而不是默认必须全部出现。

**排序与回退的讨论建议：**先固定候选及卡片格式，比较稳定原序、词法相关性排序和受控的语义/混合排序。选 BM25 并不自动获得字段语义或去噪；选 embedding 也不自动判断操作适用性。若严格条件零命中，可参考 SIEVE 返回单列的“放宽候选”，明确哪些条件被放宽，不能伪装成满足全部约束。对于硬性环境条件，例如必须可离线运行，放宽仅为发现替代方法，不能据此直接采用。放宽仅涉及查询条件，绝不能解除构建集、family 排除或其他证据访问边界。

### 13.4 改读取与采用：读到方法，再验证迁移

检索的完整路径应是：**库内存在可用经验 → 返回候选 → 读到关键行动和结果 → 当前环境适用 → 正确采用并验证 → 最终任务获益**。首轮中的分数变化不能单独证明这条链在哪一步成立或失败。

建议增加下列工具契约和可观测记录；这部分属于我们的轨迹迁移适配。

| 环节 | 建议行为 | 失败时下一步 |
|---|---|---|
| 从命中位置读取 | `read_hit` 概念接口接受稳定 hit ID；返回该命中的原始文本、所属动作和对应工具返回，必要时链接后续验证 | 命中未覆盖、长行截断或结果缺失时返回明确状态与续读坐标，不能默认从 episode 开头读几行 |
| 按事件边界扩展 | 优先读完整的“动作—观察”关联块；预算内逐步补充前提和后续验证；若没有验证，显示未知 | 超长内容分段，保留原始行/字节定位及完整读取入口；不把生成摘要当原始证据 |
| 检查适用性 | 对照当前工具/版本、输入文件与结构、资源条件、输出规范；区分工具真实返回和模型自述成功 | 环境不匹配则记录拒绝或改用另一方案；历史可用不表示当前可用 |
| 记录采用 | 只记简短可观察决策：采用/拒绝/待查、来源位置、准备迁移的操作、验证方式 | “读过”不等于“采用”；没有行动或产物证据时，不反推已经迁移 |
| 验证并继续 | 用当前环境合法可观察的输出检查结果；区分执行失败、验证失败和证据仍不足 | 按真实阻塞改写 query，或继续读原候选；重复无增量时停止检索 |

“完整读取”指关键操作链可恢复，不是每次灌入整条轨迹。JSON 中的一个工具调用及返回可能跨多个长行；固定前后若干行并不一定形成完整事件。相反，连续读完整篇长轨迹也会引入无关上下文。这是从 SIEVE 的章节访问、DR-DCI 的可续读窗口到 trajectory event 访问的适配推论。

事件关联优先使用原始 `tool_call_id` 等结构标识；缺失或冲突时显示未知，不能仅凭相邻行认定动作与结果配对。hit ID 应绑定语料版本及原文坐标，保证索引重建后不会悄悄指向另一段内容。

**检索时机的候选规则：**允许开局针对真实子目标查一次；随后在工具报错、环境条件不符、输出验证失败或明确缺失方法证据时选择再查。缺少网页事实与缺少操作方法应分别命名，以免用题材相关的文档代替行动经验。有可行计划且验证通过时，不为满足调用次数而查；零命中、明确不适用或多轮没有新信息时允许放弃历史经验。重试上限和“无增量”的判定需后续冻结，本稿不设未经讨论的阈值。

候选记录至少区分 `returned / read / adopted / rejected / unresolved`：返回、阅读属于暴露历史，采用、拒绝、待查则是针对当前子目标的可修订决策，应分字段保存，不设为互斥、不可逆的单一状态。旧结果保留入口；此前只出现于列表但未读的候选不能被永久排除。重新读同一经验的另一操作步骤也不应一律罚为冗余。这里借鉴 ITER 的交互状态思想，但不声称已获得其训练检索器的效果。

### 13.5 与原实验框架如何衔接

建议按问题分层讨论，再决定实现和运行，避免一次改变所有环节。

| 讨论优先级 | 提案 | 对应原框架 | 需要隔离的变量 |
|---|---|---|---|
| **优先0（09-09补充）** | 先建立忠实于DCI-Agent-Lite默认终端检索能力的适配基线，再讨论新增检索算法 | A3的上游能力基线；保留首轮受限版本作历史参照 | 原生可组合搜索、连续文件读取、提示与预算差异必须登记；保留Codex时不称完整Pi运行时复现 |
| **优先1** | 可组合查询语义、候选位置、命中关联读取、明确截断/续读；随后单独比较字段去噪与展示去重 | A3 的接口能力修订；确定性索引仅作可逆导航，并同步到相关候选对照 | 查询能力、读取粒度、候选展示不能只合为一个总升级结论 |
| **优先2** | 显式任务状态、适用性检查、采用记录、遇阻再查 | A3 的搜索/使用策略；仍读取同一 raw | prompt 与触发策略、调用次数、实际阅读字节；比较查询上下文是否帮助 |
| **独立对照** | 固定候选内排序；另比较 retriever 生成候选 | 检索因素单列，A2/A2-CM/A3 使用匹配候选 | 排序变化与候选集合变化必须区分；候选扩张不是排序改进 |
| **后续表示对照** | 构建集生成带原文来源的策略卡、成功条件和失败警示 | A4 相对 A3；ExpeL/ReasoningBank 启发 | 离线 builder、表示、验证标签；不把模型解释当可靠 outcome |
| **有规模瓶颈再做** | 检索器选取候选并形成持续工作区，必要时动态 pull | A5 / DR-DCI；RISE 提供有界工作区对照 | 候选规模、扩张调用与工作区预算；不用于替代 A2-CM↔A3 |

因此，**建议先讨论“原始轨迹上的搜索与读取修订 + 使用策略”，暂不把 dense retrieval、经验蒸馏、在线写回全部并入下一版。** 若第一组只能提高定位，仍无法提高操作采用或任务分数，应优先调查经验是否有迁移价值、执行器是否能转化、评分是否反映产物，而不是持续增加 query 数量。

### 13.6 后续怎样验证这些思路，而不是再次只看总分

以下只定义后续设计需要回答的问题，本次没有执行这些步骤。

- **定位质量：**候选中操作正文比例、不同事件/任务覆盖、标注可用证据的 Recall@K、首个有用证据排序位置、命中覆盖率和动作—观察完整率。零命中率下降也可能只是放宽产生更多噪声，不能单独算改进；证据标注不完备时，不把未标注内容全部判负。
- **使用质量：**返回→读取→采用→验证的转化，环境不适用识别率、错误采用、读取后补查的有效性。模型自己写“有帮助”只是一种记录，需要与后续行动和产物对应。
- **最终收益及成本：**固定任务/模型/预算的配对分数，按 task family 聚类；单独报告检索与阅读的实际 bytes/tokens、所有额外模型调用、用时和工具数。算法预算相同不代表实际成本相同。
- **存在性与因果问题：**先在构建集内部标注“库中是否存在可用方法”；若将可靠方法明确给出仍不能帮助，问题可能在可迁移性或执行。后续若做固定执行前缀下的有用/无关/无历史证据对照，要另行设计匹配条件，不能从现有观察日志认定某条经验造成了分数提升。
- **数据边界：**这24道首轮测试题已被逐题分析，应作为诊断集；据其失败改方法后，再跑只能叫回归或探索验证。可先在36道构建题内部按 family 留出开发验证，查询库同时排除该任务的全部12模型轨迹及同 family 材料；从这些材料提炼的卡片和别名也须按折重建。后续确认性评估需要未参与调参的新任务或独立 holdout，不能靠换 seed 把旧测试恢复成“未见”。

首轮的事实依据集中在 检索审计（历史文件：`experiment/reports/analysis/retrieval_audit.md`，不在精简发布中）、逐题审计（历史文件：`experiment/reports/analysis/case_audit.md`，不在精简发布中） 和 正式成绩（历史文件：`experiment/reports/formal/summary_zh.md`，不在精简发布中）：已观察到噪声与读窗问题，同时也存在评分判据与无历史证据仍涨跌的情况。因此，本节提出的机制有针对性，但目前仍是待检验假设。

**接下来一起讨论的三个选择：**第一版是否只做原始轨迹的接口与采用策略修订；候选相关性排序是否作为单独对照；何时再加入构建集策略卡。具体 API、参数、对照数量和新实验预算，在讨论后再形成新的冻结方案。

### 13.7 机制澄清与对照解释（2026-09-09）

本次进一步核查确认：首轮由同一solver自主生成查询，服务只执行连续字面匹配；不存在独立关键词生成器。固定查询状态的服务返回可复现，与关键词质量及完整agent运行的稳定性是不同问题。SIEVE的字段筛选与具名章节读取也不能自动保证事件内的条件关联；需要按轨迹原始调用标识设计粒度。

IRCoT明确编排中间句生成与检索交替；DR-DCI的agent则自主操作候选工作区。ITER主要训练检索器的状态化查询表示与交互监督，不能简化为记录历史；首轮solver本来就能看到工具历史。后续应区分“用历史指导agent生成短query”“把历史编码进retriever输入”和“候选新颖性展示”三个因素。当前字面接口不能直接拼入整个task及history，否则仅变成长短语匹配；这样的对照不能验证ITER与Agentic-R的机制差异。

完整流程、真实调用案例、论文/代码依据、服务日志与模型事件的统计口径，以及可借鉴的实现细节见[首轮检索与论文机制详解](../docs/research_notes/retrieval_followup/retrieval_mechanics_qa.md)。本次依旧只做研究讨论，未更改或运行实验。

### 13.8 下一步优先建立Lite检索基线（2026-09-09讨论）

用户提出先照搬DCI-Agent-Lite的检索逻辑再试。这个顺序比直接加入SIEVE/ITER/语义排序更利于解释：首轮虽然有持续agent循环，却将终端可组合搜索收窄为单字面串接口，并额外引入逐行来源包装和开局调用要求。先恢复上游能力，才能区分受限适配的问题与DCI方法在历史经验迁移中的局限。

拟以已固定的Lite commit `271f37e71f053bf0c99c05ce6d2fb53b841d922e`为参考，优先使用默认benchmark检索策略；IR策略另作提示对照，不混入默认基线。

| 范围 | 拟定基线要求 |
|---|---|
| 搜索能力 | 向历史证据访问提供真正可组合的终端原语：rg多模式/正则、文件范围和管道；不再将所有输入强制解释成一个字面query |
| 阅读能力 | 以普通连续文件文本读取，允许agent选择局部窗口并续读；复核上游read/bash实际截断与全文输出保存行为；不沿用每物理行重复完整provenance的包装 |
| 提示策略 | 先跟默认模式，移除本项目额外的“至少一次搜索且有命中至少读一次”计数要求；不在同一条件加入操作词模板、自动read_hit、字段排序或策略卡 |
| 必要任务适配 | 说明目录中是可能有帮助的其他任务经验，不照抄“答案就在corpus中”；保留WildClaw合法任务工具、联网需求和产物交付，不把问答或IR最终列表格式套给执行任务 |
| 数据与隔离 | 使用同一合法构建集raw内容，冻结来源映射；只向相应检索环境暴露允许的材料。首轮“不挂载book”的规定属于旧MCP协议，新终端方案需另有隔离设计，不能直接改旧容器绕开预算 |
| 运行时与成本 | 保持当前Codex和solver条件便于比较时，命名为“Lite检索逻辑的忠实适配”；Pi循环、read/bash和L3上下文管理未实际移植的部分逐项登记。工具输出限制、调用口径和总预算不能默默改变；上游能力配置与compute-matched对照分别说明 |

这一步的主要比较问题是“恢复上游终端检索能力后，相对首轮受限接口，定位、阅读和任务完成是否改善”，而非预先宣称任何一项新机制有效。默认与IR、工具接口与上下文预算都可能影响表现；完整系统差异不能全部归因于搜索表达式。保留首轮A0/A3原始结果，新条件另有版本和输出目录；开发验证仍按构建集family留出，已有24题只能作诊断/回归比较。

本节先确定讨论优先级及复现范围。用户随后确认按此准备第三版代码；实现及验证记录见下节。首轮冻结协议和原始成绩继续保留。

### 13.9 第三版代码准备：DCI终端检索（2026-09-09）

第三版使用独立条件名 `DCI_TERMINAL`，阅读标签为 V3；V1 仍对应 A0 无历史检索，V2 仍对应 A3 简易字面检索。旧24对、48次正式运行及其原始分数不重写、不重命名。新实现位于 V3目录（历史文件：`experiment/variants/dci_terminal/README.md`，不在精简发布中），统一版本及旧结果校验入口为 versions.json（历史文件：`experiment/reports/versions.json`，不在精简发布中）。本节记录代码准备和不调用模型的接入验证阶段，当时V3尚无实验分数；后续获准执行的进展见13.10。

DCI-Agent-Lite中的Lite指作者的精简公开实现和轻量运行框架，不限制模型大小。本版保留本项目已冻结的 `gpt-6-astra / ultra`、Codex CLI、逐题任务环境、1800秒解题时限与原评分流程；以固定Lite源码为检索逻辑参考，并不声称复现Pi运行循环或其L3上下文压缩。

| 项目 | 第三版具体实现 |
|---|---|
| 查询 | 同一solver自主调用MCP `bash(command, timeout)`，在独立语料容器中真正执行bash、rg、正则、多模式和管道；没有单字符串关键词解释层或独立查询模型 |
| 阅读 | `read(path, offset, limit)`返回连续原文，一次附带路径/位置/截断信息；默认及最大2000行，同时受字节上限约束。可局部续读，或用bash切片超长单行；读取引用图片沿用独立图片预算 |
| 原始数据 | formal视图保留原36题×12模型的432条轨迹及381个去重引用图片；dev视图按原family排除保留26题×12模型的312条轨迹及100个图片。仅复制清单允许文件，并验证字节SHA；来源清单在宿主侧，不向agent增加摘要或策略卡 |
| 可见范围 | `/corpus`只读，`/scratch`保留命令输出和任务内文件。语料容器无网，不挂载当前任务、评分材料、认证文件或主机项目；solver继续使用原任务环境 |
| 默认提示 | 使用本地历史库和rg，由agent决定检索时机、表达式和是否采用。取消V2额外的开局最少查读次数；不加入IR排序、多角度模板、BM25、向量检索或自动别名 |
| 输出与预算 | 保留80次接纳调用、单次12000与累计256000字节UTF-8文本上限；文本元数据计费。图片仍为单张5MiB、总16MiB。bash返回尾部，超长输出保存供续读；命令输出超过16MiB时停止并标记保存不完整 |
| 工程接入 | 每次命令20秒默认、30秒最大；1CPU/512MiB/64进程。scratch有256MiB周期监测停机阈值，允许瞬时超调。并行检索调用立即返回busy，不在60秒MCP超时后继续排队执行 |
| 运行与保全 | `prepare`只生成独立清单；显式单任务`run`才调用solver及grader。运行前重建并核对协议及数据哈希，拒绝覆盖任何已有run目录；检索服务在引入评分材料前关闭 |

这里恢复了上游终端检索的表达能力，同时保留本项目的返回预算，所以不是原封不动的上游配置：Pi工具默认50KiB，本版12000字节；旧字面扫描的64MiB范围上限由真实命令的时间/资源上限替代；Codex继续管理上下文。单次/总字节数字相同，也不代表两版进入模型的有效原文量相同——V2逐行重复来源包装，本版只附一次。报告将实际返回的正文、已核验包装、控制提示及无法分类的文本分开计量，同时列出总返回量、tokens、时延及调用数。bash正文可能是路径列表或加工后的匹配行，不能直接当作原trajectory阅读量；独立原文字节和实际采用量仍需额外证据。

因此V3首先衡量“更完整的DCI检索适配是否改善任务表现”，其中接口、提示时机和包装一起变化，不能把所有差异归因为关键词搜索能力。旧24题已参与诊断，未来新增V3分数属于回归/探索比较；本次准备不会恢复这些题作为未接触确认集的资格。

### 13.10 V3执行与结果读取（2026-09-09，全部完成）

用户授权的 **6道构建集内部开发验证 + 24道正式比较题，每题一次求解** 已全部完成。六道开发题正常完成求解和评分，并通过工程验收（历史文件：`experiment/manifests/dci_terminal_development_audit.json`，不在精简发布中）与独立执行、检索证据复核（历史文件：`experiment/reports/dci_terminal/development_verification.json`，不在精简发布中）。开发验收不设置分数或最少检索次数门槛；有效零分保留，没有因低分或零检索重跑挑选结果。24道正式题按固定清单（历史文件：`experiment/manifests/dci_terminal_schedule.json`，不在精简发布中）串行完成，均有有效官方评分；其中地点搜索达到1800秒求解上限，按冻结规则保留0.5分。见完成批次（历史文件：`experiment/runs/dci_terminal/batch.json`，不在精简发布中）及完成核验（历史文件：`experiment/reports/dci_terminal/completion_verification.json`，不在精简发布中）。

六道开发题均未主动调用DCI，记录中可以核验这一点。工具可用性另由真实Docker工具检查及实际solver容器中的连接探针验证；连接探针只执行initialize、ping和tools/list，没有注入模型查询或消耗证据预算。这证明接入可用，不证明模型已自然阅读或采用历史经验。正式结果同样分别报告工具调用、检索证据及分数，不能把“配置了DCI”直接当作“利用了DCI”。

新增三版本比较与证据导航（历史文件：`experiment/reports/dci_terminal/README.md`，不在精简发布中）使用V1/V2/V3名称，原始A0/A3目录、协议与48次结果保持原样。比较固定24题分母，六道开发分数单独保存；缺失、未完成和评分错误保持null，合法零分仍为0。只有同题有效结果才能进入配对差值。报告将“分数收集完整”和“检索证据全部核验”分别标明；独立执行记录、最终运行记录、非空转录、来源哈希、账本预算及模型调用对应关系均需检查。

V2强制开局检索，V3按已确认的默认策略自主决定是否查询，因此两版同时改变了接口和调用策略。没有检索的任务，其成绩变化不能解释为历史经验内容带来的效果。三版实际均可能使用Codex原生子代理，尽管旧配置字段为multi_agent=false；成本表中的主solver token不覆盖全部子代理、评分和辅助请求，不能直接换算订阅美元费用，也不能把带继承上下文的子会话累计token简单相加。

已完成的地点搜索案例（历史文件：`experiment/reports/dci_terminal/cases/location_search.md`，不在精简发布中）首次观察到自然DCI调用：两次bash返回20个路径及4条搜索工具相关的匹配行，共2931字节，未继续读取完整操作与结果。历史轨迹紧接命中行的“零搜索结果”没有进入模型上下文；后续动作也不足以确认采用。这给13.4的事件边界读取建议提供了具体诊断案例，但不能据单次失败证明该改法一定有收益。两次调用均已返回，随后继续解题约28分钟才触及1800秒上限；保留合法超时分数，并单独核验超时前保存的调用对应关系。

已完成的升级路由案例（历史文件：`experiment/reports/dci_terminal/cases/chat_escalation_routing.md`，不在精简发布中）说明分数也需要结合评分可见内容解释：官方grader只取完整结果的前10000个Python字符，部分已经写出的调查结论和草稿不在输入中。V3的0.775高于V1的0.675和V2的0.595，但三版完整报告均识别了QA测试，不能把QA的0/0/1评分解释为V3首次获得这一能力。该V3运行没有调用DCI，因此也不能把加分归因于检索。原始官方分数及输入裁剪规则均保留，案例只补充解释，不重评。

人物传记案例（历史文件：`experiment/reports/dci_terminal/cases/wikipedia_biography.md`，不在精简发布中）中的唯一DCI调用因不支持的`timeout_ms`参数而未执行，没有返回轨迹。三版得分均为0.51；后续成功的User-Agent请求方法在DCI返回前就已提出并运行，不能归因于检索。冻结源码提供的参数定义是`timeout`（秒），但实际会话只保存了截断描述的发现输出，缺少模型收到完整schema的证据；目前只能确认请求与接口不符，不能进一步断言责任在模型或适配层。后续应分别检查“实际调用、命令执行、候选返回、连续读取、适用性判断、采用”，避免将调用次数等同于利用历史经验。

艺术品搜索案例（历史文件：`experiment/reports/dci_terminal/cases/artwork_search.md`，不在精简发布中）三版均为1.0。V3第一次调用因额外参数被拒绝，第二次执行管道后无输出，两次均没有返回轨迹正文。管道整体退出码0不提供`rg`独立状态，不能把它记作成功命中，更不能把满分归功于检索。另一个评论任务案例（历史文件：`experiment/reports/dci_terminal/cases/malicious_comments.md`，不在精简发布中）三版均为0，直接触发官方识别项总分门槛；输入与产物完整，但评分可见内容和短语规则限制了对批评对象的解释。两例均保留原分数，用于区分任务得分、实际检索与评分测量范围。

完整24题的结果如下；分数、实际调用与采用证据应分开解释。

| 版本 | 正式题数 | 均分 | 有DCI调用的题数 | 保存的主／子线程DCI调用合计 |
|---|---:|---:|---:|---:|
| V1：无检索 | 24 | 0.7979 | 0 | 0 |
| V2：简易检索 | 24 | 0.7869 | 24 | 84 |
| V3：DCI终端检索 | 24 | 0.7664 | 3 | 5 |

V3较V1平均低0.0315，逐题提高／持平／下降为2／14／8；较V2低0.0205，为2／16／6。以family为簇的5000次bootstrap探索性95%区间分别为[-0.0875, 0.0046]、[-0.0611, 0.0121]，均覆盖0；每题单次运行且题目参与过诊断，不能据此建立独立泛化结论或证明两种方法等效。详细数据和复算见完整比较（历史文件：`experiment/reports/dci_terminal/summary_zh.md`，不在精简发布中）。

**本轮没有观察到整体提升，更直接的问题是检索链路很少被实际走通。** V3的21题有零调用记录支持；3题的5次调用均来自主线程，2次参数校验失败，另外3次实际执行bash。只有地点搜索返回正文，共2262字节，包含20个文件路径和4条匹配行，不能当作完整轨迹阅读量；整轮总返回3706字节还包含1286字节包装和158字节控制文本。没有专用`read`调用或图片返回，也未确认一次历史经验采用。`bash`本身可以读正文，因此0次`read`不等于0次阅读；这里的具体不足是没有从命中行继续读完整操作与结果。

V3主solver累计20336367个token、求解8386.5秒；V1为15153698个token／10029.9秒，V2为22778668个token／11420.0秒。主solver token包含缓存与多轮输入，不含所有子代理和评分成本，不能换算实际订阅费用。V3更短的求解时长也不能据此归因于检索效率，尤其多数题未调用DCI。模型配置核查（历史文件：`experiment/reports/dci_terminal/model_configuration_audit.json`，不在精简发布中）覆盖V1/V2的48次、V3开发6次和正式24次，主／子会话保存记录均符合冻结配置；这不独立证明服务端内部路由。

后续讨论应先围绕这些已观察到的断点设计对照：在构建集验证工具参数信息是否完整可见、错误后能否修正；以相同接口比较自主调用与明确阻塞时的查询指引；对命中后的连续操作／结果读取和环境适用性判断作独立对照。关键词别名、范围和多词语义可继续按13.4改进，但不应把参数失败或无阅读一律归为关键词问题。以上是下一轮候选思路，本轮冻结参数与结果保持原样，未启动追加实验。

终态核验确认30份运行与清单一一对应，无计划外运行；旧31个冻结文件、157个保全文件以及V1/V2的96份结果／分数哈希和5份正式报告均保持一致。任务容器、检索与推理租约、在途请求已排空，本轮gateway、bridge、proxy按登记身份停止，端口和socket已释放。全部证据入口见三版本导航（历史文件：`experiment/reports/dci_terminal/README.md`，不在精简发布中）。

### 13.11 V3.1：由Codex原生工具检索历史轨迹（2026-09-09，协议已冻结）

用户明确要求取消V3自定义的`bash/read` MCP，并授权完整执行 **6道开发验证 + 24道正式比较，每题一次求解**。V3.1使用独立条件名`DCI_NATIVE`，直接将V3同阶段的轨迹文件库只读挂到求解容器的`/corpus`，由Codex原生终端和文件工具完成搜索、阅读、续读及再查询。没有自定义检索MCP、关键词生成器、命令包装或结果截断层；任务、鉴权、快照与评分仍由现有实验代码管理。源码和运行说明见V3.1实现（历史文件：`experiment/variants/dci_native/README.md`，不在精简发布中），新增结果与原生工具证据见独立报告导航（历史文件：`experiment/reports/dci_native/README.md`，不在精简发布中）。

本节是13.10之后新获授权的实验，前三版已完成的成绩、协议和终态停机记录继续作为历史事实保留。V3.1的开发协议（历史文件：`experiment/variants/dci_native/prepared/dev/protocol.json`，不在精简发布中）、正式协议（历史文件：`experiment/variants/dci_native/prepared/formal/protocol.json`，不在精简发布中）与固定调度（历史文件：`experiment/manifests/dci_native_schedule.json`，不在精简发布中）均已落盘；新增运行使用独立目录，不覆盖或重做已有版本。协议中的`prepared_not_executed`描述冻结准备时点，执行进展以新增运行和报告为准，本节不预填未完成分数。

| 项目 | V3.1冻结设置与比较边界 |
|---|---|
| 求解器 | 保留`gpt-6-astra / ultra`、Codex CLI `0.153.4`及原镜像`sha256:37d61bbb96578b73dc4017f48b46fec26231b5afb15f65386951ca2612c7d87e`；使用原生执行、读取和上下文管理 |
| 历史语料 | formal仍为36题×12模型的432条轨迹及381个图片；dev仍为按family排除后的26题×12模型的312条轨迹及100个图片。复用相同原文字节，只读挂载到原任务环境；测试轨迹与评分材料不进入库 |
| 默认查询策略 | 参考Lite固定commit`271f37e71f053bf0c99c05ce6d2fb53b841d922e`的默认策略，鼓励直接搜索及阅读。由agent自主生成关键词、正则、路径范围、管道和后续查询，不强制首查，不加入IR、多角度模板或排名要求 |
| 任务适配 | 库中是其他任务经验，包含失败尝试，不保证有当前题答案；保留WildClaw的任务工具、联网规则和产物要求。原镜像没有`rg`，将已冻结的标准ripgrep可执行文件只读挂到`/usr/local/bin/rg`；不改变镜像或工具调用语义 |
| 预算 | 求解与评分仍各1800秒。移除V3专用80次接纳调用、单次12000／总256000文本字节、图片字节、独立命令超时与语料容器资源限制；检索与解题共用原生工具、上下文和任务资源 |
| 调度与验收 | 保留V3的6道开发题与24道正式题及顺序，先开发后正式、串行、每题一次。开发验收检查工程可用性和有效评分，不设最低分数或最少检索次数；合法零分和超时结果保留，不因表现不佳重跑 |

Lite默认实现本来就使用Pi自带的终端工具，没有独立的关键词生成算法。因此这里迁移的是“让agent在可见文件库中自主搜索与阅读”的方式，不把Pi工具名称、接口实现或L3上下文管理重新写进Codex。上游默认问答提示中的“答案在库里”和禁止网页搜索不适合本项目，已经按历史经验迁移任务作必要改写。V3.1与V3同时改变检索接口、提示中的工程说明和专用预算；即使分数变化，也不能只归因于移除MCP或关键词质量。

**评分隔离保持独立于原生检索。** 求解容器整体停止并保存`solver-evidence`之后，实验代码使用原镜像、命令、环境、资源和持久挂载重建静止容器，仅移除`/corpus`。随后调用原`TaskRuntime.grade`，恢复基础设施和Social mock冻结状态，再引入gold与原评分程序，不重复任务warmup。实际Docker检查核对其余配置；新容器Hostname、静止入口stdout/stderr附着标志及OOM默认值`null→false`的已知变化单独登记。原rootfs只读，可写工作区、缓存、Codex和OpenClaw目录等持久挂载保持，评分采用相同任务产物。这层代码只负责容器生命周期和评分边界，不实现搜索或阅读工具。

新增报告分开记录分数、原生工具观察和环境证明。`native_environment.json`保存实际只读挂载、无MCP配置、ripgrep版本／哈希及评分去除语料的证据；保存的主／子会话用于核对模型设置和原生调用。检索使用从命令与路径引用事后观察，显式`/corpus`引用需要结合命令解释；变量、相对路径、脚本和复制文件会使完整计量不再可得。没有显式引用不等于零检索，命令返回文本也不能直接当作去重后的原轨迹阅读量，更不能据此认定采用。不会为了补齐这些指标重新引入工具包装或伪造检索账本。

**经验库的任务覆盖也是解释结果的必要条件。** [冻结划分](../experiment/manifests/split.json)将48个family整组分到构建侧27组、测试侧21组，没有跨侧family；432条轨迹来自36个构建任务各12个模型尝试，不代表432种任务。三种jigsaw和两种Link-a-Pix均在测试侧，构建侧有连点等其他图像操作任务；海报制作与裁切也在测试侧，构建侧有幻灯片和视频处理任务。会议论文检索则有较接近的构建任务，但其作者、年份筛选要求不同于测试题的Oral和第一署名机构判定。任务归属与原Prompt可查任务清单（历史文件：`experiment/manifests/task_manifest.json`，不在精简发布中）。本轮主要考察跨family的经验迁移，操作层近邻分布不均；不能从关键词命中直接推定方法适用，也不能仅凭没有同类构建任务就断言轨迹内完全不存在有用片段。后续分析应分别检查经验覆盖、定位、读取完整性与实际采用。

正式比较仍使用此前已逐题分析的24题，属于**探索性回归**。开发结果单列，未完成和评分故障保持null，不能进入已完成均值或替代为0；完整比较需要固定24题收集完毕并核验。V1/V2/V3原始分数保持原样。本节记录已授权方案、冻结边界及供给侧解释条件；执行完成后的结果与机制分析见13.12。

### 13.12 V3.1完整结果与检索机制分析（2026-09-10 UTC）

**原生检索接入成功；本轮没有确认稳定的整体提升。** 6道开发验证与24道正式比较均已完成，每题一次，30次求解均正常结束。正式24题平均分为0.7770，高于V3的0.7664，仍低于V1与V2。开发分数不混入正式均值，合法零分保持原样。成绩从96份正式原始结果独立重算，与四版本汇总（历史文件：`experiment/reports/dci_native/summary_zh.md`，不在精简发布中）一致；逐题原始文件、模型身份、命令位置和配置证据见配对数据（历史文件：`experiment/reports/dci_native/paired.json`，不在精简发布中）。

| 版本 | 历史经验访问方式 | 正式题数 | 平均分 |
|---|---|---:|---:|
| V1 / A0 | 无历史库 | 24 | 0.7979 |
| V2 / A3 | 受限关键词搜索、局部读取MCP，要求开局查询 | 24 | 0.7869 |
| V3 / DCI_TERMINAL | 自定义bash/read MCP，模型自主决定查询 | 24 | 0.7664 |
| V3.1 / DCI_NATIVE | Codex原生终端访问只读文件库，模型自主查询 | 24 | 0.7770 |

| V3.1相对版本 | 同题均分差 | 提高 / 持平 / 下降 | family bootstrap探索性95%区间 |
|---|---:|---:|---|
| V1 | −0.0209 | 4 / 15 / 5 | [−0.0979, +0.0237] |
| V2 | −0.0099 | 4 / 14 / 6 | [−0.0570, +0.0391] |
| V3 | +0.0106 | 5 / 16 / 3 | [−0.0293, +0.0549] |

三个区间均跨0。这是既有24题上的单次探索性回归；同时改变了检索接口、提示中的工程说明和专用预算，不能分离各因素的因果效应，family bootstrap也不估计重复生成的随机性。正式求解耗时合计V3.1为9178.9秒、V3为8386.5秒；主solver token分别为21,185,056和20,336,367。这些不是包含全部子代理、辅助请求和评分的完整成本，更不能直接换算订阅美元费用。

保存日志中，22道正式题出现显式语料访问候选，范围包括目录列表、文件路径、空读与正文查询。另2题没有显式候选，检索使用仍为未知。这个计数不代表22题都读过历史方法，也不能替代完整检索次数或采用量。原生工具和环境核验通过，已详细核对案例中的断点更具体地落在以下环节：

1. **候选的命中理由与后续读取条件不一致。** 图片分类用多个OR关键词筛出候选，随后只查其中未命中的`contact.sheet`，空读后没有补查；人物传记因`zh-hans`网址命中，得到手机条目的搜索摘要而非HTML提取方法。日程安排曾筛错文件扩展名，后来自主修正到`.txt`，仍未找到正文。聊天查询中的`sender.*internal`还可能跨越同一JSON行中的多个字段，不能当作精确的内部发件人过滤。这些是查询语义与相关性问题，取消MCP不会自动修复。见简短检索观察（历史文件：`experiment/reports/dci_native/README.md`，不在精简发布中）及聊天取证（历史文件：`experiment/reports/dci_native/cases/chat_escalation_routing.md`，不在精简发布中）。
2. **读窗由模型的命令决定，常常不足以覆盖操作和结果。** 人物传记只读了5行、898字节的搜索摘要；模糊仓库搜索的780字节窗口包含API操作，却在`toolResult`标题处结束，返回正文从下一行才开始。主页题用`cut`主动裁切代码行；其他任务还出现Codex对超长输出的截断。必须区分命令主动选小范围与工具截断，不能统一归为检索包装限制，更不能把一次读窗当作整条trajectory。见窗口与裁切记录（历史文件：`experiment/reports/dci_native/README.md`，不在精简发布中）、前三题（历史文件：`experiment/reports/dci_native/cases/first_three_formal.md`，不在精简发布中）及正式03–05（历史文件：`experiment/reports/dci_native/cases/formal03_05.md`，不在精简发布中）。
3. **历史建议传递有证据，新增行动与得分收益仍需另证。** 聊天题存在“历史窗口→子会话总结→主会话收到”的直接记录，但完整读取与首次重试在收到总结前已发生，当前任务技能也给出了类似要求。机构查询的当前JSON端点来自当前网页；人物传记的BeautifulSoup操作、模糊仓库搜索的GitHub API调用都早于历史读取。不能因为后续方法与历史相似就认定采用，也不能概括为从未传播过经验。V2对照中另有明确借用通用验证思路的记录，仍不等于证明分数因此提高。见采用链与V2对照（历史文件：`experiment/reports/dci_native/cases/formal03_05.md`，不在精简发布中）、聊天建议传递（历史文件：`experiment/reports/dci_native/cases/chat_escalation_routing.md`，不在精简发布中）。
4. **较大的分数变化需要检查当前解题过程和评分可见范围。** 服装图与产品海报相对V3分别提高0.4020、0.1560，但没有明确的历史经验促成改善证据：前者决定使用imagegen早于历史查询，后者没有读取历史正文。地点题下降0.2500，提交的错误城市可追溯到当前网络候选，不能解释为错用历史方法。聊天题下降0.0800，对应Jake与跨消息关联两项；完整报告中的Jake背景确实落在裁判读取的前10000字符之外。这不能等同于模型未识别背景，也不能据此推算一个假定的重评分数或断言全部扣分都由截断造成。原评分保持不变。见地点案例（历史文件：`experiment/reports/dci_native/cases/location_search.md`，不在精简发布中）和聊天评分输入核对（历史文件：`experiment/reports/dci_native/cases/chat_escalation_routing.md`，不在精简发布中）。

**后续讨论应继续使用原生工具，优先改查询和读取决策。** 本轮已经完成用户要求的默认DCI式自主文件检索适配；下一步不需要为了这些问题再实现bash/read工具。以下仅为待讨论的改进思路，未启动追加实验：

- 查询围绕当前操作、工具和约束组织；先确认候选究竟匹配哪个条件，再选择一致的正文查询，避免宽泛OR词、通用任务编号或跨字段正则产生错误相关性判断。
- 命中后检查是否读到完整操作、返回结果及验证；窗口停在命令或返回标题时继续读取，并判断当前环境是否适用。无需每次读整条trajectory，也不应把固定行数当作方法完整性的保证。
- 在明确阻塞或待决策处比较自主查询与针对性提示，把“找到方法、充分读取、明确采用、执行验证、最终评分”分别记录。先在构建/开发材料验证策略和操作级覆盖；新的确认性结论需要独立任务或重复运行设计，不能继续把已分析24题称为未见测试集。

全轮收尾通过完成核验（历史文件：`experiment/reports/dci_native/completion_verification.json`，不在精简发布中）：30份新增运行均有效，旧1626个文件保全，无多余运行；后台控制与监督进程已退出、请求和租约排空、任务容器清理，三项实验服务停止且端点释放。首个开发任务的信任配置追加以及中途控制进程中断均有独立审核记录；已完成结果经核验复用，没有重跑求解。V1/V2/V3原代码、协议、报告与原始分数保持原样。
