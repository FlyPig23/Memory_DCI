# V8 公共实现

这里保存 [Terminal-Bench 4.0 V8](../benchmarks/terminal_bench_4/README.md) 使用的基础实现；运行入口、数据、协议和结果均在该基准目录。

- `codex_backend.py`：Codex 配置与会话解析。
- `task_runtime.py`：容器及求解、评分隔离。
- `memory/tools.py`：五个可执行 DCI/memory 工具；V8 的 stdio MCP 服务由 `../benchmarks/terminal_bench_4/engine/serve_memory.py` 提供，工具为 `reason`、`DCI_search_task`、`DCI_search_trajectory`、`DCI_search_memory`、`distill`。
- `memory/prompt.txt`、`memory/review_prompt.txt`：求解与独立复盘提示词。
- `memory/seed.py`：冻结初始经验的校验与恢复。
- `dataset_io.py`：下载、哈希校验和安全恢复。

检索工具执行模型提供的终端命令，memory 写入需保留来源与明确的更新决定。每次求解使用独立 memory 副本，题后更新不回写冻结 seed，也不传给后续运行。
