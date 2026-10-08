"""Post-hoc: what each solver trial did, read from its final /app snapshot, its public event log and the
verifier's test list. Analysis only; nothing here reaches a solver or a builder.

Usage: python -m side_experiments.hard_task_failure_replay.scripts.mechanisms r2_20261007 r3_20261007 v3r1 v3r2 v3r3
Writes manifests/mechanisms.json and prints one line per trial.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from .common import MANIFESTS, RUNS, TB, write_json

TASK_DEPS = TB / "prepared/tasks/make-doom-for-mips/environment/task-deps"
LAUNCH = {"setsid": r"\bsetsid\b", "nohup": r"\bnohup\b", "disown": r"\bdisown\b", "daemon_flag": r"--daemon\b|daemonize",
          "systemd": r"systemctl|systemd-run", "start_stop_daemon": r"start-stop-daemon", "tmux_screen": r"\btmux\b|\bscreen\b",
          "double_fork": r"os\.fork\(|os\.setsid\(", "cron_rc": r"crontab|/etc/rc\.local|\.bashrc|/etc/profile"}


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def commands(trial: Path) -> tuple[list[str], list[str]]:
    """Completed shell commands, and commands still running when the agent ended (left in the tool session)."""
    done, started = [], {}
    events = trial / "agent" / "codex.events.jsonl"
    for line in events.read_text().splitlines() if events.exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if item.get("type") != "command_execution":
            continue
        if event.get("type") == "item.started":
            started[item.get("id")] = item.get("command", "")
        elif event.get("type") == "item.completed":
            started.pop(item.get("id"), None)
            done.append(item.get("command", ""))
    return done, list(started.values())


# Places where a write makes code run inside later, unrelated processes (including a checker), or touches tests.
TAMPER = re.compile(r"\.pth$|sitecustomize\.py$|usercustomize\.py$|/etc/profile|bashrc$|\.profile$|/etc/rc\.local$|"
                    r"/etc/cron|crontab|/usr/(local/)?lib/python3[.0-9]*/[^/]+\.py$|^/tests/|/logs/verifier")
TAMPER_CMD = re.compile(r"(>|tee|cp |mv |install |ln )[^|;&]*(\.pth\b|sitecustomize|usercustomize|bashrc|/etc/profile|"
                        r"/etc/rc\.local|/etc/cron|/tests/)|crontab ")


def environment_tampering(trial: Path) -> list[str]:
    """Files written (apply_patch) or commands that write where later processes or the checker would pick them up."""
    hits = []
    events = trial / "agent" / "codex.events.jsonl"
    for line in events.read_text().splitlines() if events.exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if event.get("type") != "item.completed":
            continue
        if item.get("type") == "file_change":
            hits += [c["path"] for c in item.get("changes", []) if TAMPER.search(c.get("path", ""))]
        elif item.get("type") == "command_execution" and TAMPER_CMD.search(item.get("command", "")):
            hits.append("cmd: " + re.sub(r"\s+", " ", item["command"])[:140])
    return sorted(set(hits))


def failed_tests(trial: Path) -> list[str] | None:
    ctrf = trial / "verifier" / "ctrf.json"
    if not ctrf.exists():
        return None
    tests = json.loads(ctrf.read_text())["results"]["tests"]
    return [t["name"].split("::")[-1] for t in tests if t.get("status") != "passed"]


def task_features(task: str, workspace: Path, cmds: list[str], unfinished: list[str]) -> dict:
    joined = "\n".join(cmds + unfinished)
    if task == "pypi-server":
        # Commands that start something in the background; the mere mention of a tool (command -v setsid) is not use.
        starts = [c for c in cmds if re.search(r"&\s*(\n|$|;|disown|echo|sleep)|setsid |--daemon|start-stop-daemon", c)
                  and not re.search(r"command -v|--help|--version", c)]
        return {"launch_mentions": sorted(k for k, p in LAUNCH.items() if re.search(p, joined)),
                "launch_used": sorted(k for k, p in LAUNCH.items() if any(re.search(p, c) for c in starts)),
                "last_start": re.sub(r"\s+", " ", starts[-1])[:220] if starts else None,
                "left_in_tool_session": [re.sub(r"\s+", " ", c)[:160] for c in unfinished],
                "server": sorted(k for k, p in {"pypiserver": r"pypi-server|pypiserver", "http.server": r"http\.server",
                                                "custom_python": r"HTTPServer|BaseHTTPRequestHandler"}.items()
                                 if re.search(p, joined))}
    if task == "filter-js-from-html":
        code = (workspace / "filter.py").read_text(errors="replace") if (workspace / "filter.py").exists() else ""
        libs = sorted(k for k, p in {"bs4": r"from bs4|import bs4|BeautifulSoup", "html5lib": r"html5lib", "lxml": r"\blxml\b",
                                      "bleach": r"\bbleach\b", "html.parser": r"html\.parser|HTMLParser"}.items()
                      if re.search(p, code))
        style = ("bs4_reserialize" if "bs4" in libs else "html5lib" if "html5lib" in libs or "lxml" in libs
                 else "stdlib_tokenizer" if "html.parser" in libs else "regex_or_scanner" if code else "missing")
        return {"filter_py_bytes": len(code), "libs": libs, "style": style}
    if task == "dna-insert":
        fasta = workspace / "primers.fasta"
        records = re.findall(r">([^\n]*)\n([ACGTacgt\n]+)", fasta.read_text()) if fasta.exists() else []
        return {"primer_records": len(records), "primer_lengths": [len(s.replace("\n", "")) for _, s in records]}
    if task == "make-doom-for-mips":
        img = next(iter(sorted(workspace.rglob("doomgeneric_img.c"))), None)
        return {"vm_js_unchanged": sha(workspace / "vm.js") == sha(TASK_DEPS / "vm.js"),
                "img_c_unchanged": None if img is None else sha(img) == sha(TASK_DEPS / "doomgeneric_img.c"),
                "binary_present": (workspace / "doomgeneric_mips").is_file()}
    if task == "gcode-to-text":
        out = workspace / "out.txt"
        return {"answer": out.read_text(errors="replace").strip()[:80] if out.exists() else None}
    return {}


def trial_row(run: str, arm: str, trial: Path) -> dict:
    task = trial.name.split(f"{arm}-", 1)[1]
    reward_file = trial / "verifier" / "reward.txt"
    cmds, unfinished = commands(trial)
    return {"run": run, "arm": arm, "task_id": task,
            "reward": float(reward_file.read_text().strip()) if reward_file.exists() else None,
            "failed_tests": failed_tests(trial), "commands": len(cmds),
            "environment_tampering": environment_tampering(trial),
            **task_features(task, trial / "evidence" / "workspace", cmds, unfinished)}


def main() -> int:
    rows = []
    for run in sys.argv[1:]:
        for arm_dir in sorted(p for p in (RUNS / run).iterdir() if p.is_dir()):
            for trial in sorted(p for p in arm_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
                if (trial / "verifier" / "reward.txt").exists():
                    rows.append(trial_row(run, arm_dir.name, trial))
    write_json(MANIFESTS / "mechanisms.json", {"note": "post-hoc analysis; never shown to solvers or builders", "rows": rows})
    for row in sorted(rows, key=lambda r: (r["task_id"], r["arm"], r["run"])):
        extra = {k: v for k, v in row.items() if k not in ("run", "arm", "task_id", "reward", "commands")}
        print(f"{row['task_id']:20} {row['arm']:14} {row['run']:12} reward={row['reward']} {json.dumps(extra)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
