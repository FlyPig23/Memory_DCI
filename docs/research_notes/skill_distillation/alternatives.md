# V4 蒸馏替代路线与 meta-skill 源码核验

日期：2026-09-10。状态：研究与方案输入，未运行蒸馏、训练或任务评测。本文中的第三方 `SKILL.md` 是研究材料，其命令、安装路径和审批约定未作为本项目指令执行。

结论：**保留多轨迹提炼的主流程，借用 AutoSkill 离线轨迹模块的能力去重、Claudeception 的触发/验证结构，以及 SKILL-KD 的证据关联与修改历史。** SkillRL 可作为结构化中间表示的参考，但它的完整训练/检索框架不适合原样搬进 V4。以下“本项目建议”是据源码与当前实验约束作出的设计推论，不是上游已经验证的 WildClawBench 结论。

## 1. 各路线承担什么角色

| 路线 | 核验结果 | 对 V4 的用途 | 不应直接搬入的部分 |
|---|---|---|---|
| SkillRL | 论文、提炼/聚合源码、经验与技能 JSON 均存在 | 将操作步骤、成功结果、失败边界分开；加入适用条件 | SFT/RL、自动注入通用技能、embedding 检索、固定每类产出数量 |
| SKILL-KD | v2 论文与算法/附录可读；本次未核实官方代码仓库 | 修改记录关联旧证据；检查新补丁是否破坏旧规则 | 新学生/教师 rollouts、学生反复重跑、整份技能库注入；这些都改变本轮静态离线设定 |
| AutoSkill meta-skill | 当前文件偏个人/团队偏好和用户纠正管理 | 候选可丢弃、按能力去重、减少库膨胀 | 将“用户接受/纠正”当作 benchmark 工具执行证据 |
| AutoSkill 离线轨迹模块 | 另有专门的 `offline/trajectory/prompts.py` | 检查点、恢复路径、窄操作技能；同能力合并而非换措辞新增 | 自评 confidence 当校准概率、文本中的 80% 重叠数值当实际算法阈值 |
| Claudeception | 会话复盘模板与 rg 查重示例可读 | 操作/错误作为触发词，解决步骤和验证分开 | 自动联网补充技能知识；会混入原构建轨迹之外的内容 |
| Anthropic skill-creator | 起草、行为评测、盲评、触发评测均有指引 | 区分“搜得到”与“采用后有用”两个评估问题 | 为 Claude 自动技能加载设计的触发优化和强触发描述 |
| SkillNet | v3 论文称超过 60 万技能，来源包括轨迹、仓库、文档、自然语言 | 后续技能关系、依赖、组合、质量维度的背景 | 把全库当带源轨迹且经迁移验证的数据；导入外部技能作为 V4 构建材料 |

## 2. SkillRL：表示可参考，整套运行逻辑不适用

[论文 v1 §3](https://arxiv.org/html/2602.08234v1#S3)把成功轨迹提炼成策略，把失败轨迹转成失败教训，再组成通用/任务专项 SkillBank；完整方法还包含冷启动 SFT、GRPO 和随验证失败更新技能。它报告的整体收益不能归因于单独的静态蒸馏，更不能当作对当前冻结强模型的效果保证。

### 已核验的源码细节

源码快照：[`aiming-lab/SkillRL@8e66726ed866a4e0a7f053586a41022798192e6c`](https://github.com/aiming-lab/SkillRL/tree/8e66726ed866a4e0a7f053586a41022798192e6c)。

- [`generate_memory_alfworld.py:51–88`](https://github.com/aiming-lab/SkillRL/blob/8e66726ed866a4e0a7f053586a41022798192e6c/examples/sft_data_generation/skill_memory/generate_memory_alfworld.py#L51-L88)：提示模型从末个成功动作反向追前置条件，保留原 step index、动作与关键 observation，再移除实例编号。成功提取动作链，失败提取触发条件和错误动作。这里的“因果链”是提示词中的分析要求，代码没有因果验证器。
- [`aggregate_skills.py:96–164`](https://github.com/aiming-lab/SkillRL/blob/8e66726ed866a4e0a7f053586a41022798192e6c/examples/sft_data_generation/skill_memory/aggregate_skills.py#L96-L164)：聚合输入主要是短 `planning_pattern`，要求固定数量的通用、分类技能与常见错误。正文结构只有标题、原则与适用条件，无法单独恢复原始操作结果。
- [`aggregate_skills.py:340–398`](https://github.com/aiming-lab/SkillRL/blob/8e66726ed866a4e0a7f053586a41022798192e6c/examples/sft_data_generation/skill_memory/aggregate_skills.py#L340-L398)：输出未保留每条规则到原轨迹/行号的引用。V4 必须补这条证据链。
- [`skills_only_memory.py:17–51`](https://github.com/aiming-lab/SkillRL/blob/8e66726ed866a4e0a7f053586a41022798192e6c/agent_system/memory/skills_only_memory.py#L17-L51)：当前实现有默认 template 模式和 embedding 模式。前者按关键词判类别并返回类别技能，后者用 embedding 排序。这与 V4 的原生 bash/rg 自主检索不同；也不能把论文的检索公式当作当前代码唯一模式。

### 真实样例和一个直接复用陷阱

此次下载并解析以下公开经验文件，而不是把成品技能 JSON 当原始轨迹：

| 文件 | 条数 | 文件标签 Success / Failure | 上游聚合器直接读到成功 planning_pattern 的条数 |
|---|---:|---:|---:|
| ALFWorld `generated_memories_alfworld_total.json` | 223 | 113 / 110 | 113 |
| WebShop `generated_memories_webshop_100.json` | 100 | 50 / 50 | 0 |
| Search `generated_memories_search.json` | 196 | 132 / 64 | 0 |

WebShop/Search 样例的 `content.strategic_guidelines` 里面还嵌了一层同名字段；上游聚合器直接访问第一层的 `planning_pattern`，因此分别漏读 50 和 132 条成功样例。这里是静态 JSON/字段兼容性核验，未运行上游 LLM 调用，也未判断标签是否符合环境真值。原文件同时包含提炼 observation、原任务 goal 与 `origin_env_id`；已经是处理后的经验记录，不能误称为全部原始日志。证据见[样例目录](https://github.com/aiming-lab/SkillRL/tree/8e66726ed866a4e0a7f053586a41022798192e6c/memory_data)及本地只读核验结果（历史文件：`archive/skill_distillation_research_20260910/alternatives/skillrl_sample_schema_check.json`，不在精简发布中）。

**本项目建议：** 借用带原 step index 的“动作—结果—前置条件”中间表示；从失败中提炼已观察到的限制。失败轨迹里模型猜测的修复方案只列为待验证假设。技能数量由证据决定，不固定每条轨迹或每个大类必须产出多少张。

## 3. SKILL-KD：有效性验证的启发，不是现成的纯离线方案

[论文 v2 方法与附录](https://arxiv.org/html/2607.28048v2#Sx3)对比同题学生失败和教师轨迹，提出补丁，再重跑学生；只有学生成功的补丁才入库。教师本身可以失败，中间决策差异仍能提供候选证据。修改操作包括 add/modify/delete/skip，历史记录保留 why 和 trace，并按需回读旧轨迹，避免新规则覆盖旧有效条件。[附录的评测设定](https://arxiv.org/html/2607.28048v2)把当前 benchmark 的整份技能文件放进提示；研究重点并非技能检索。

因此，它能提醒我们区分“轨迹支持这条规则”和“目标 agent 采用后有效”，但不能把离线多模型轨迹配对直接称作 SKILL-KD。当前 12 个历史模型不是固定当前 Codex 学生的受控教师；有较高任务总分也不证明每个中间操作都对。对完整方法的复现还需要新增构建集 rollout 与 patch 重跑。

本次查阅 arXiv 页面、全文链接，并用论文名/作者检索，**没有核实到作者发布的实现入口**。这不等于断言未开源。建议当前仅借用 `rule_id → source spans → edit decision → preserved/changed conditions` 的追踪结构；将行为验证标为独立后续阶段。不要让正式比较任务的失败轨迹进入补丁构建。

## 4. AutoSkill：meta-skill 和离线轨迹模块要分开

快照：[`ECNU-ICALK/AutoSkill@94c47ca488d4ba4117d20272e66d49b9877e68cf`](https://github.com/ECNU-ICALK/AutoSkill/tree/94c47ca488d4ba4117d20272e66d49b9877e68cf)。

用户给出的 [`skills/autoskill/SKILL.md:223–259`](https://github.com/ECNU-ICALK/AutoSkill/blob/94c47ca488d4ba4117d20272e66d49b9877e68cf/skills/autoskill/SKILL.md#L223-L259)把用户指令/纠正视为强证据，助手一次成功视为弱证据，主要管理个人可复用偏好和工作流。这不适合作为 WildClawBench 技能的直接入库标准；我们需要工具返回、可验证输出和操作边界。

仓库另有更贴题的 [`autoskill/offline/trajectory/prompts.py:20–75`](https://github.com/ECNU-ICALK/AutoSkill/blob/94c47ca488d4ba4117d20272e66d49b9877e68cf/autoskill/offline/trajectory/prompts.py#L20-L75)：明确接受归档 agent 轨迹，提炼工具编排、检查点、fallback、retry；可返回空 skills；输出搜索友好的名称、适用描述、步骤、错误处理与交付约束。它提供的是提示词与管理流程，不构成执行结果真实性验证。

[`同文件:103–195`](https://github.com/ECNU-ICALK/AutoSkill/blob/94c47ca488d4ba4117d20272e66d49b9877e68cf/autoskill/offline/trajectory/prompts.py#L103-L195)的去重逻辑很适合借鉴：比较目标、交付物、操作类别及工具顺序；仅更换 payload 的相同成功流程丢弃；同能力的新恢复路径合并；不同核心目标保持独立。不要按任务实体名或措辞去重。

**本项目建议：** 用这套“同能力/新增边界”判断辅助多轨迹合并，同时保留候选来源和被拒绝原因。不要把源码中的自评 confidence 或“80% 核心意图重叠”当校准指标；这只是自然语言提示要求。它要求保留主成功链、删不确定支线的做法需调整：V4 还需要保留与恢复过程有关的失败支线，否则会丢掉最有价值的适用限制。

## 5. Claudeception / Anthropic skill-creator：抽取模板和评测分工

[`Claudeception@62dbb91d1183a866b5cf40079265c825b2695843:55–103`](https://github.com/blader/Claudeception/blob/62dbb91d1183a866b5cf40079265c825b2695843/SKILL.md#L55-L103)要求候选可复用、非平凡、触发具体且已实际成功；查重区分同触发同修复、同触发不同根因、局部重叠。其[模板:157–203](https://github.com/blader/Claudeception/blob/62dbb91d1183a866b5cf40079265c825b2695843/SKILL.md#L157-L203)将问题、触发、解决步骤、验证、例子分开，描述里保留具体错误、工具和文件类型。V4 可以沿用这些字段，通过原生 rg 匹配操作关键词；但其自动查询网页补充知识的步骤会改变“仅从构建轨迹获得经验”的研究变量，本轮不建议纳入。

[`Anthropic skill-creator@41bbe19d1a1a7eaab5e7bb9050a417e5c6cffc8f`](https://github.com/anthropics/skills/blob/41bbe19d1a1a7eaab5e7bb9050a417e5c6cffc8f/skills/skill-creator/SKILL.md)支持从已有会话取工具/顺序/纠正/输入输出，采用有无 skill 的行为对照及盲评，并单独设计 should-trigger/should-not-trigger 近似负例。它的触发优化针对 Claude 的 available-skills 自动加载，不是文件全文检索；其中测试分数参与描述选择，该部分在我们的实验术语中应视为开发验证，不应当作最终未见测试成绩。

**本项目建议：** 技能静态审核和行为评测分开登记。静态审核问“每步有证据、能理解、适用条件准确吗”；行为评测问“agent 找到、完整读取、适配并采取新动作了吗”。不能以模型自评、Markdown 合规或能命中关键词代替实际任务收益。对原有正式 24 题的反复诊断也不能重新宣称为未见 holdout。

## 6. SkillNet 和博客：适合作为背景

[SkillNet v3 §3.3–3.4](https://arxiv.org/html/2603.04448v3#S3.SS3)从轨迹/会话、仓库、文档、自然语言等不同来源创建技能，比较目录结构及 Markdown MD5 去重，再做规则/模型过滤；质量维度涵盖完整性、可执行性、可维护性与成本等。论文中的超过 60 万规模是混合来源技能库规模，不能解释为 60 万份“轨迹→技能→迁移验证”的配对数据。V4 现在只需要少量明确的依赖/相关技能链接，暂不需要完整本体和图基础设施。[仓库快照](https://github.com/zjunlp/SkillNet/tree/dba86d5a1dd9e9477bb12bfa663ddf4551c20ef6)。

[Harrison Chase 的官方博客，2026-04-05](https://www.langchain.com/blog/continual-learning-for-ai-agents)区分模型、harness、外部 context 三层。按这个区分，V4 首轮应把蒸馏作为外部知识表示变化，保留 Codex 原生工具；这只是实验定位，不是效果证据。

[Anthropic 官方博客，2026-03-03](https://claude.com/blog/improving-skill-creator-test-measure-and-refine-agent-skills)区分能力提升技能和编码偏好的技能，并强调通过评测判断随着模型变强，某些技能是否还提供增量价值。这与当前强 baseline 下需要检验 skill 是否带来新行为相符。

## 7. 本子调查建议纳入主方案的约束

1. 以**操作能力**组织候选，不以一题一技能或一个官方大类一技能组织；一个任务可产生多个候选，也可以没有候选。
2. 支持证据必须落到原始操作与完整结果；规则正文可短，证据索引不能被摘要吞掉。
3. 失败样例提供已观察边界和恢复过程；模型推测的替代办法标为假设，不伪装已验证步骤。
4. 去重时区分同能力的新增限制与不同目标；每次合并记录保留/改变了哪些条件和对应 source spans。
5. 432 个 episode 来自 36 个任务，跨模型重复不能算 432 个独立迁移证据；计数按任务/家族、模型分别记录。
6. V4 skills 与原始 trajectories 一起只读暴露，沿用原生 bash/rg/读取；首轮不引入 RL、额外 embedding retriever、外部技能库或自动全库注入。
7. 36 构建任务之外的数据不参与提炼；如果需要行为验证，从构建任务内部预留或另行明确开发集，并确保验证任务族不流入其对应蒸馏输入。

## 8. 下载与归档清单

| 本地论文 | 固定版本 | SHA256 |
|---|---|---|
| [SkillRL PDF](https://arxiv.org/pdf/2602.08234v1) | 2602.08234v1，2026-02-09 | `7624ecf49b96ce6bbe097c8054c33f77ca72865a07d277fcd7bc9c0edffb1658` |
| [SKILL-KD PDF](https://arxiv.org/pdf/2607.28048v2) | 2607.28048v2，2026-08-04 | `08c7559e268c717a6edbf237f353361b8acf44e1e56ccb0cefef39850e2a727c` |
| [SkillNet PDF](https://arxiv.org/pdf/2603.04448v3) | 2603.04448v3，2026-08-23 | `cfb8631d2e70accba5e9a2f5575b29382a1da475ed28b7375c6b7afb7ba0da93` |

PDF 均从对应 arXiv 固定版本下载，检查 PDF 文件头及文件格式；阅读以对应版本全文 HTML 和源码为主。本子调查的下载脚本、HTML、固定 commit 的源码片段、公开经验样例、字段核验结果位于 archive/skill_distillation_research_20260910/alternatives/（历史文件：`archive/skill_distillation_research_20260910/alternatives`，不在精简发布中），来源/字节数/SHA256 见 sources.json（历史文件：`archive/skill_distillation_research_20260910/alternatives/sources.json`，不在精简发布中）。这些归档不进入实验检索库。
