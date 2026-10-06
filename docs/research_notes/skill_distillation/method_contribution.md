# 蒸馏skill与论文方法贡献的定位

协议日期：2026-09-10。方法与实现已建立；实际阶段、验收和产物统一见执行入口（历史文件：`experiment/benchmarks/wildclaw_bench/variants/dci_skills/README.md`，不在精简发布中）。方法贡献的论证与正式蒸馏、V4迁移结果分开记录。

用户确定将蒸馏方法本身写成可复用skill。产物为 distill-trajectory-skills（历史文件：`experiment/benchmarks/wildclaw_bench/skills/distill-trajectory-skills/SKILL.md`，不在精简发布中），与WildClawBench轨迹最终生成的操作skills分开保存。前者指导如何蒸馏，后者是该方法的输出。

可以在论文中论证的方向是：面向“原轨迹与操作skill共同供DCI按需检索”的设定，把提炼、证据核查、合并与发布写成可执行、可审计的外置方法。不是把一个现成meta-skill换名，也不能把“轨迹蒸馏成skill”本身宣称为首次提出。

| 本项目方法设计 | 需要交付的实证 |
|---|---|
| 全量记录覆盖并追踪未解决记录 | 全部432条的record/字符区间、实际模型输入、逐记录处理终态；分别报告输入覆盖和处理覆盖 |
| 区分procedure/recovery/failure_guard与观察结果 | 成功、失败与未知状态的规则实例，错误方向的拒收案例 |
| 从原调用ID恢复动作—结果关系，保留逐claim引用 | 原始source hash、恢复元数据、源坐标与规则引用链 |
| 同题多模型对照后按操作跨任务合并 | 全候选ledger、条件分支、来源task/family计数，合并前后实例 |
| 新skill与原轨迹共同可查 | 只读发布库、有效来源链接，以及后续真实原生DCI使用记录 |
| 方法也是skill，并在小样本中迭代后冻结 | skill及提示/代码版本、修改理由、冻结hash和正式重建记录 |

这些项是否具有足够新颖性、各自是否提高质量，需要与[Trace2Skill、SPARK及其他相关工作](README.md)逐项比较和后续消融。当前可以报告设计与实现；只有实际完成覆盖审计才能报告432条已处理，只有V4对照才能报告迁移收益。

开发阶段出现了一个值得保留的方法观察：arXiv来源抽查（历史文件：`experiment/benchmarks/wildclaw_bench/reports/dci_skills/dev-v1_arxiv_quality_review.md`，不在精简发布中）发现，实际执行的JSON条数检查被标为语义类别`check`，初版确定性门禁因为没有字面的`action`标签而拒收，单次修订也未解决。这里需要区分“证据用于检查什么”与“源记录中是否实际发起调用”。候选修订及验证（历史文件：`archive/skill_distillation_research_20260910/staged_dev_v2/README.md`，不在精简发布中）据此增加一条严格通路：检查引用必须位于真实ACTION参数内，并有同一来源、明确调用ID配对的RESULT；保留原标签和引用，再做独立语义审核。固定62块的静态重放中，13块的19个原候选因此变为待审核。这是早期样本中的资格变化，不是新增19个已验证技能，也不能外推为总体误拒率。实际采用与开发验收状态仍以执行入口为准。

另一个审核编号漏字案例（历史文件：`experiment/benchmarks/wildclaw_bench/reports/dci_skills/dev-v1_review_id_failure_analysis.json`，不在精简发布中）属于工程可靠性问题：审核判断完整，但重复的来源编号前缀少了一个字符。候选修订仅在完整后缀精确、唯一匹配且所有候选一一覆盖时校正该前缀，另存映射，保留原响应。它说明引用身份需要可靠传递；不宜把这项编号修复单独包装成研究创新，也不能借此改变审核结论或重复抽样。

之后的第二例诊断（历史文件：`experiment/benchmarks/wildclaw_bench/reports/dci_skills/dev-v1_repair_second_episode_id_diagnostic.md`，不在精简发布中）发现第二段重复编号连续漏写三个字符，原候选规则不适用。已采用的新修订要求另一段完整、其余身份字段逐字匹配、另一段仅连续删除1–3字符且整份响应一一对应；采用与验证证据（历史文件：`experiment/benchmarks/wildclaw_bench/reports/dci_skills/dev-v2_adoption_validation_followup.json`，不在精简发布中）单独保存。恢复后，两例真实恢复审计（历史文件：`experiment/benchmarks/wildclaw_bench/reports/dci_skills/dev-v2_actual_review_id_recovery.json`，不在精简发布中）已通过：六个旧请求精确复用，非编号审核字段不变，旧失败与拒收/修订保留。这仍是身份传递修复，不改变语义判断或提供新的技能迁移有效性证据，也不代表整个开发集通过。

后续未解决记录检查（历史文件：`docs/research_notes/skill_distillation/unresolved_record_scope.md`，不在精简发布中）进一步发现：完整原文已经送入模型、模型也返回了一项处理意见，仍可能没有完成该记录的处理。固定早期快照的103条基础完成轨迹中，108条不同源记录至少一次被标为`unresolved`；其中27条有其它非未解决的处理意见，仍需要核对原先每一项缺口是否真正得到回答。由此增加独立的补读与终态审核：保留旧意见，依据完整记录、明确动作—结果配对和必要图片作出新的有依据判断；发现新操作则进入提炼、独立审核和单次修订，不能直接把补读笔记发布成skill。

该阶段的完整方法reference（历史文件：`experiment/benchmarks/wildclaw_bench/skills/distill-trajectory-skills/references/record_resolution.md`，不在精简发布中）及合并、冻结环节已通过本地检查并采用。原本没有unresolved的小样本直通路径已实际验收；dev-v2完整基础提炼后，非零开发流程已实际启动（历史文件：`experiment/benchmarks/wildclaw_bench/runs/dci_skills/distillation/dev-v2-record-resolution/launch.json`，不在精简发布中），处理317次未决判断对应的298个唯一记录，其完整终态验收仍在后续。旧部分审计中的269处保留为历史观察。补读新发现须有独立操作范围判断；通过原有候选审核后还要逐claim核对范围，最后检查所有实际处置。相同证据的旧拒绝不能通过新编号洗成新尝试。同一episode按record顺序推进，每个后续record看见先前实际接受/拒绝结果；跨episode可以并行。正式报告应同时给出文本输入覆盖、记录处理覆盖、候选去向和skill证据质量；真实任务结果未知，可以与该记录已经处理完同时成立。

这项设计需要用真实运行评估价值与成本，不能仅凭新增审核阶段或测试数量宣称研究贡献已经成立。与只看摘要的蒸馏比较时，可关注：源缺口是否被发现、是否增加了有据的操作候选、是否防止超范围规则，以及补读成本。后续对照仍需固定输入库、模型、任务与预算，不把工程编号修复和能力增益混为一谈。

后续论文应分别披露离线提炼成本、在线检索成本、技能证据质量和任务得分。源轨迹有证据不等于当前模型可复现；当前模型可复现也不等于未见任务迁移有效。重复源任务的12个模型不应当作12份独立任务的泛化证据。
