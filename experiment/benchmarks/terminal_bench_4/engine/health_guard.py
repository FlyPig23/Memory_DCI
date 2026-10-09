"""Stop infrastructure-only trials without retrying or interpreting their score.

JWT decoding checks local expiry metadata only; it is not an authentication
probe. Configured models/tools and a started session do not prove inference.
The returned audit contains categories and counts, never credentials/log text.
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
import re
import time


class AuthValidityError(RuntimeError):
    pass


class InfrastructureFailure(RuntimeError):
    pass


def ensure_auth_lifetime(auth_path, solver_seconds, review_seconds=180, *, margin_seconds=300, now=None):
    """Fail before creating a trial when its copied access token expires too soon."""
    try:
        limits = (solver_seconds, review_seconds, margin_seconds)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value < 0 for value in limits):
            raise ValueError
        auth = json.loads(Path(auth_path).read_text())
        token = auth['tokens']['access_token']
        if not isinstance(token, str) or len(token.split('.')) != 3:
            raise ValueError
        encoded = token.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        expires = payload['exp']
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise ValueError
        required = sum(limits)
        remaining = expires - (time.time() if now is None else now)
        if remaining <= required:
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, UnicodeError):
        # Neither parsing failures nor exception chains may expose token bytes.
        raise AuthValidityError('Access-token validity is insufficient or unverifiable for a fresh trial') from None
    return {'expires_at': expires, 'remaining_seconds': remaining, 'required_seconds': required}


_FAILURES = {
    'authentication': re.compile(r'(?i)(refresh[_ ]token.{0,100}(already.{0,15}used|expired|revoked)|'
                                 r'could not be refreshed|refresh_token_reused|unauthorized|'
                                 r'invalid[_ ]api[_ ]key|authentication (failed|error)|'
                                 r'(access|bearer) token.{0,60}(expired|invalid)|\b401\b)'),
    'tls': re.compile(r'(?i)(unknownissuer|invalid peer certificate|certificate verify failed|'
                      r'certificate verification failed|self[- ]signed certificate|untrusted issuer)'),
    'network': re.compile(r'(?i)(connection failed|error sending request|network (is )?unreachable|'
                          r'connection (reset|refused|timed out)|name resolution|dns (error|failure)|'
                          r'stream disconnected before completion|failed to connect)'),
}
_TOOL_ITEMS = {'function_call', 'custom_tool_call', 'mcp_tool_call', 'command_execution',
               'web_search_call', 'local_shell_call', 'computer_call', 'image_generation_call'}
_TOKEN_KEYS = {'input_tokens', 'output_tokens', 'total_tokens', 'reasoning_output_tokens',
               'cached_input_tokens', 'input_token_count', 'output_token_count'}


def _positive_usage(value):
    if not isinstance(value, dict):
        return False
    return any((key in _TOKEN_KEYS and isinstance(item, (int, float))
                and not isinstance(item, bool) and math.isfinite(item) and item > 0)
               or (isinstance(item, dict) and _positive_usage(item))
               for key, item in value.items())


def _has_content(item):
    return any(item.get(key) for key in ('content', 'text', 'message', 'summary'))


def _actual_item(item):
    if not isinstance(item, dict):
        return False
    kind = item.get('type')
    if kind in _TOOL_ITEMS:
        return True
    if kind == 'message' and item.get('role') == 'assistant':
        return _has_content(item)
    if kind in {'agent_message', 'agent_reasoning', 'reasoning'}:
        return _has_content(item)
    # These wrappers are emitted by the native JSON stream or session ledger.
    if kind in {'response_item', 'event_msg'}:
        return _actual_item(item.get('payload'))
    if kind in {'item.started', 'item.updated', 'item.completed', 'item_completed'}:
        return _actual_item(item.get('item'))
    if kind in {'turn.completed', 'token_count'}:
        return _positive_usage(item.get('usage') or item.get('info') or {})
    return False


def _diagnostic(item):
    """Only inspect error envelopes, never user prompts or retrieved documents."""
    if not isinstance(item, dict):
        return ''
    kind = item.get('type')
    if kind in {'response_item', 'event_msg'}:
        return _diagnostic(item.get('payload'))
    if kind not in {'error', 'turn.failed', 'task_complete', 'error_notification'}:
        return ''
    error = item.get('error')
    if isinstance(error, dict):
        return str(error.get('message', '')) + ' ' + str(error.get('codex_error_info', ''))
    return str(error or item.get('message') or '')


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def inspect_trial(directory):
    """A phase is blocked only with infrastructure errors AND no model output.

    Missing/null usage alone is inconclusive. A real attempt can receive a zero
    reward or encounter a transient transport error and must retain its result.
    """
    phases = {}
    for phase in ('agent', 'review'):
        logs = Path(directory) / phase
        actual = _positive_usage(_read_json(logs / 'usage.json'))
        failures = set()

        def observe(text):
            failures.update(name for name, pattern in _FAILURES.items() if pattern.search(text))

        for name in ('codex.session.jsonl', 'codex.events.jsonl'):
            path = logs / name
            if not path.is_file():
                continue
            with path.open(errors='replace') as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    actual = actual or _actual_item(item)
                    observe(_diagnostic(item))
        stderr = logs / 'codex.stderr.log'
        if stderr.is_file():
            with stderr.open(errors='replace') as stream:
                for line in stream:
                    observe(line)
        phases[phase] = {'actual_inference': actual, 'failure_kinds': sorted(failures),
                         'blocked': bool(failures) and not actual}
    return {'blocked': any(phase['blocked'] for phase in phases.values()), 'phases': phases}
