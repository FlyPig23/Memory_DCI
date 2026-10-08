"""Render an ATIF trajectory as indented text, in the main pool's body format.

The main pool renders every ATIF field without summarizing. Its renderer is
not part of the published code, so this module reproduces the body format and
``test_side.py`` checks it byte-for-byte against pool files. Two differences
are deliberate: the header says the file is a failed attempt at the same task,
and inline base64 images are replaced by a short placeholder that keeps their
size, because megabytes of base64 are unreadable for a solver.
"""
from __future__ import annotations

import json
import re

INDENT = "  "
DATA_URI = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")


def _scalar(value) -> str:
    if isinstance(value, str):
        return value if value else '""'
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return json.dumps(value)
    raise TypeError(f"unsupported scalar {type(value).__name__}")


def _emit(value, depth: int, out: list[str], elide_images: bool) -> None:
    pad = INDENT * depth
    if isinstance(value, dict):
        if not value:
            out.append(pad + "{}")
        for key, item in value.items():
            out.append(f"{pad}{key}:")
            _emit(item, depth + 1, out, elide_images)
    elif isinstance(value, list):
        if not value:
            out.append(pad + "[]")
        for index, item in enumerate(value):
            out.append(f"{pad}[{index}]")
            _emit(item, depth + 1, out, elide_images)
    else:
        text = _scalar(value)
        if elide_images and isinstance(value, str):
            text = DATA_URI.sub(lambda m: f"[inline image omitted: {len(m.group(0))} characters]", text)
        # Internal blank lines keep the indentation; a trailing newline adds no line.
        for line in text.splitlines() or [""]:
            out.append(pad + line)


def render_body(trajectory: dict, *, elide_images: bool = False) -> str:
    out: list[str] = []
    _emit(trajectory, 0, out, elide_images)
    return "\n".join(out) + "\n"


def render_failed_attempt(trajectory: dict, *, task_id: str, model: str, agent: str,
                          trial_id: str, version_status: str) -> str:
    header = [
        f"# Failed attempt at this task: {task_id}",
        f"Model: {model}; agent: {agent}",
        f"Official reward: 0; trial ID: {trial_id}",
        "Official scored execution: True",
        f"Source task compatibility: {version_status}",
        "This attempt was officially graded and failed. Hidden test results are not included,",
        "so this record does not say why it failed. All ATIF fields below are rendered verbatim.",
    ]
    return "\n".join(header) + "\n\n" + render_body(trajectory, elide_images=True)
