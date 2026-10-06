# 运行、隔离与评分说明

当前支持 [V1 / V1.1 基线](../README.md)与 [V5 工作流](../variants/dci_memory/README.md)。数据和固定上游版本见 [DATA.md](../../../../DATA.md)。本目录解释共享运行设施；具体配置以相应版本协议为准。

| 文档 | 用途 |
|---|---|
| [Codex 运行适配](CODEX_RUNTIME.md) | 任务容器、solver 证据与评分隔离 |
| [推理兼容层](INFERENCE_GATEWAY.md) | 辅助调用和 LLM 评分的实际兼容范围 |
| [私有推理访问](INFERENCE_ACCESS.md) | 客户端访问范围与认证隔离 |
| [评分错误契约](GRADER_ERROR_CONTRACTS.md) | 区分服务错误、缺失分数与有效零分 |
| [评分恢复](GRADING_RECOVERY.md) | 对同一冻结产物恢复基础设施失败评分 |
| [服务生命周期](SERVICE_CONTROL.md) | 项目服务的启动、状态检查与停止 |
| [工作模型配置](CURRENT_MODEL_CONFIG.md) | 模型配置变更的历史记录 |

这些文档部分写于较早阶段，可能提及已移除的 A3 / V2–V4 路径或旧默认模型。隔离与评分设计仍可参考，版本入口与实际代码决定当前运行方式。V1 为 Astra / ultra 历史基线；V1.1、V5 为 Sol / medium。

命令中的 `experiment/...` 均相对项目根目录。认证文件、私有运行状态与凭据不进入共享代码或数据。历史协议保持原始语义；新实验在准备阶段建立本轮校验值，不覆盖既有有效结果。

研究依据集中在 [V5 memory 笔记](../../../../docs/research_notes/retrieval_followup/codex_memory_structure_20260928.md)、[检索文献核查](../../../../docs/research_notes/retrieval_followup/README.md)与[论文索引](../../../../papers/README.md)。旧方案和开发笔记是知识资料，不是新实验的操作清单。
