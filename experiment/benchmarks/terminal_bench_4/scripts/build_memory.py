"""V8's frozen Opus same-task analysis builder, using the benchmark's own runtime.

The engine retains its original V7 schema names and prompt text. This public
entry point defaults to Terminal-Bench 4.0 and does not require the TB2.1 tree.
"""
from experiment.benchmarks.terminal_bench_4.engine.v7_memory import main


if __name__ == "__main__":
    raise SystemExit(main())
