# 检索与 memory 研究资料

当前代码入口是 [V1 / V1.1 / V5](../../../README.md)。本目录保存设计依据和当时的文献核查；涉及旧版实验的内容是历史讨论，不属于当前运行流程。

| 文件 | 用途 |
|---|---|
| [V5 memory 结构](codex_memory_structure_20260928.md) | Codex、Reflexion、MemGPT、A-Mem 与每题独立记忆设计 |
| [DR-DCI / Codex 工作流核查](dr_dci_codex_workflow_audit_20260925.md) | 查询约定、技能发现和独立记忆整理的设计依据 |
| [检索机制问答](retrieval_mechanics_qa.md) | 查询、局部读取、适用性判断与采用 |
| [文献笔记](literature_review.md) | 相关研究的机制和局限 |
| [DCI-Agent-Lite 核查](dci_lite_review.md) | 提示词、终端原语和查询循环 |
| [DR-DCI 核查](dr_dci_review.md) | 检索、去重、读取与工作区管理 |
| [文献来源清单](literature_sources.json) | 原文链接与版本 |
| [Lite 来源](dci_lite_source_snapshot.json) / [DR 来源](dr_dci_sources.json) / [工作流来源](workflow_audit_sources_20260925.json) | 固定 commit 和源码定位 |

原框架保留于 [研究报告](../../../reports/DCI_upgrade_report.md)。旧实验运行目录和蒸馏产物不随精简代码发布。
