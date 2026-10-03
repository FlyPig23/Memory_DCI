# 当前工作模型配置

从 2026-09-21 起，项目源代码中面向后续运行的默认工作模型统一为：

- 模型：`gpt-5.6-sol`
- reasoning effort：`medium`
- provider：`openai`，通过本地 Codex CLI 的 ChatGPT 登录调用

已同步的活动入口包括 `experiment/src/experiment_protocol.py`、`experiment/src/codex_backend.py`、`experiment/src/codex_inference_gateway.py`、`experiment/scripts/service_control.py` 以及轨迹蒸馏调用链 `experiment/variants/dci_skills/inference.py`。

已经完成或冻结的历史运行仍保留原模型、原推理强度和原始方法 hash，不被追溯改写。切换后的新调用统一使用上述配置；如果续跑任务按实验约定保留了旧模型已经完成的片段，这些片段会作为带 provenance 的历史结果直接沿用，不会伪装成新模型请求，也不会为满足模型统一而重跑。
