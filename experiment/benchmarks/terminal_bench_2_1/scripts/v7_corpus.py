"""V7 corpus: each test task's pool holds only its own officially failed attempts.

sources  -> manifests/v7_failure_sources.json, host-only reward-0 selection
download -> data/historical_trials/<id>/ official bodies or official absence evidence
build    -> prepared/v7_corpus/<build_id>/{index.json, audit/, pools/<task>/}
decontam-report / verify read a finished build.

No model calls or Docker. Successful attempts never reach a pool, a card or a
manifest. Hidden tests, reference solutions and task README files are read on
the host only to flag contaminated trajectories; their text is never written.
Relative path options resolve against --base (this benchmark directory).
"""
from __future__ import annotations

import argparse
import base64
import bisect
from collections import Counter
import concurrent.futures as cf
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import threading
import time
import urllib.error
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
import urllib.request

from . import package_data

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parents[2]
HUB = 'https://hub.harborframework.com'
USER_AGENT = 'TerminalBench21-PublicResearchArchive/1.0'
SOURCES = 'v7_failure_sources.json'
DOWNLOAD_AUDIT = 'v7_download_audit.json'
DECISIONS = 'decontamination_decisions.json'
SOURCE_INPUTS = ('manifests/split.json', 'reports/official_test_trials.csv',
                 'data/discovery/registry_trials.json', 'manifests/historical/public_trials.json')
ROW_FIELDS = ('task_id', 'trial_id', 'job_id', 'model_name', 'agent_name', 'agent_version',
              'error_type', 'registry_trajectory_path')
SELECTION = ('official_test_trials.csv rows of test tasks with reward 0, is_scored True and '
             'version_status exact; null and positive rewards are excluded')
RECORD_STATUSES = ('available', 'raw_fallback', 'unavailable', 'excluded')
RAW_FORMATS = ('raw_unparsed_json', 'malformed_atif_json', 'native_agent_log')
INDEX_KIND = 'v7_same_task_corpus'
POOL_KIND = 'same_task_failed_official_trajectories'
TRAJECTORY_FORMAT = 'all-field ATIF text rendering; original raw/native fallbacks preserved byte-for-byte'
TRAJECTORY_PATH = 'trajectories/<task_id>/<model_alias>/<attempt_id>/<task_id>_0.txt'
MANIFEST_KEYS = frozenset({
    'schema_version', 'benchmark', 'kind', 'task_id', 'trajectory_count', 'source_record_count',
    'confirmed_unavailable_count', 'raw_unparsed_trajectory_count', 'successful_trajectories_included',
    'trajectory_format', 'trajectory_path', 'mutable_memory', 'cross_task_memory'})
TITLE = '# Historical failed attempt at this task: '
SCORED_LINE = 'Official scored execution: True; attempt: 1; parent scored trial: self'
COMPATIBILITY = 'Source task compatibility: exact_task_version; target task version: '
NOTE_LINE = 'The reward is a source-run outcome, not a guarantee for every historical action.'
ATIF_LINE = 'All ATIF fields below are rendered without summarizing or distilling; embedded images become local files.'
NATIVE_LINE = 'Official source format: original native agent log, retained byte-for-byte without conversion.'
UNPARSED_LINE = 'Official source format: original unparsed trajectory data, retained byte-for-byte without repair.'
REASON_PREFIX = 'Official source availability reason: '
UNAVAILABLE_ROW = 'NO TRAJECTORY BODY AVAILABLE (official source confirmed)'
COUNT_LINE = '{} officially graded failed attempts at this task (official reward 0). Successful attempts are not included.'
KEEP_LINE = 'Keep failures and partial runs. Check actual actions and observations before adopting a method.'
CARD_ROW = re.compile(r'- (.+) \| score=0 \| model=(.*) \| scored=True \| attempt=(attempt-\d{3,})')
ATTEMPT = re.compile(r'attempt-(\d{3,})')
IMAGE = re.compile(r'([0-9a-f]{64})\.(png|jpeg|webp)')
BUILD_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
# Case-insensitive: host names and benchmark names are not case-sensitive references.
FETCH = re.compile(r'github\.com/search|google\.com/search|duckduckgo\.com|laude-institute/terminal-bench|'
                   r'harbor-framework/(?:terminal-bench|harbor)|api\.github\.com/search|original-tasks/|'
                   r'terminal-bench-(?:1|2|science|core)|hub\.harborframework\.com|supabase\.co', re.IGNORECASE)
TEST_REFERENCES = ('test_outputs.py', 'solve.sh', '/solution/', 'tests/test_')
TOKEN = re.compile(r'\w+|[^\w\s]')
KEY_LINE = re.compile(r'([A-Za-z_][\w.-]*):')
SOLUTION_SHINGLE, TESTS_SHINGLE = 20, 12
THRESHOLDS = {'fetch_hits': 1, 'test_file_refs': 1, 'solution_overlap': 3, 'tests_overlap_max_run': 60}
TEXT_LIMIT = 2_000_000
OFFSET_LIMIT = 10
LOCAL = threading.local()


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def sha(path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(path):
    return json.loads(Path(path).read_bytes())


def atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.part.{os.getpid()}.{threading.get_ident()}')
    temporary.write_bytes(data)
    temporary.replace(path)


def save(path: Path, value) -> None:
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def alias(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9._-]+', '-', text).strip('-')


def valid_alias(name: str) -> bool:
    return bool(re.fullmatch(r'[a-zA-Z0-9._-]+', name)) and name not in ('.', '..')


def unique_index(rows, key, label):
    result = {}
    for row in rows:
        value = row[key]
        if value in result:
            raise ValueError(f'Duplicate {label}: {value}')
        result[value] = row
    return result


def _protocol():
    """protocol imports this module on V7 paths, so import it at call time."""
    from . import protocol
    return protocol


def _under(base: Path, value) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(base) / path


def _relative(path: Path, base: Path) -> str:
    path = Path(path).absolute()
    return path.relative_to(Path(base).absolute()).as_posix() if path.is_relative_to(Path(base).absolute()) else path.as_posix()


# ---------------------------------------------------------------- 2. sources

def select_failures(split: dict, rows: list[dict]) -> list[dict]:
    """Official scored reward-0 rows of test tasks at the exact task version only."""
    ids = [row['trial_id'] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate trial in the official test-trial table')
    test = set(split['test_task_ids'])
    return [row for row in rows if row['task_id'] in test and row['reward'] == '0'
            and row['is_scored'] == 'True' and row['version_status'] == 'exact']


def failure_sources(base: Path = BASE) -> dict:
    """Join each selected failure to its registry job and public trial row; refuse any doubt."""
    base = Path(base)
    split = load(base / 'manifests/split.json')
    with (base / 'reports/official_test_trials.csv').open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    registry = unique_index(load(base / 'data/discovery/registry_trials.json'), 'id', 'registry trial')
    public = unique_index(load(base / 'manifests/historical/public_trials.json')['trials'], 'id', 'public trial')
    selected = []
    for row in select_failures(split, rows):
        trial_id, task_id = row['trial_id'], row['task_id']
        source, trial = registry.get(trial_id), public.get(trial_id)
        if source is None or trial is None:
            raise ValueError(f'Failure source lacks its registry/public join: {trial_id}')
        if type(trial.get('reward')) not in (int, float) or trial['reward'] != 0 or trial.get('is_scored') is not True:
            raise ValueError(f'Public trial is not an official scored failure: {trial_id}')
        name = str(trial.get('task_name') or '')
        if (name.rsplit('/', 1)[-1] != task_id or source.get('task_name') != name
                or not source.get('job_id') or source['job_id'] != trial.get('job_id')):
            raise ValueError(f'Registry/public trial identity mismatch: {trial_id}')
        # The V7 header states "attempt: 1; parent scored trial: self"; never let it be false.
        if trial.get('attempt') != 1 or trial.get('parent_scored_trial_id'):
            raise ValueError(f'Retry attempts are not supported by the V7 header: {trial_id}')
        if not isinstance(trial.get('model_name'), str) or not valid_alias(alias(trial['model_name'])):
            raise ValueError(f'Unusable model name: {trial_id}')
        path = source.get('trajectory_path')
        if path is not None and path != f'trials/{trial_id}/trajectory.json':
            raise ValueError(f'Unexpected registry trajectory path: {trial_id}')
        # Registry lock/config hold environment dictionaries and are never copied.
        selected.append({'task_id': task_id, 'trial_id': trial_id, 'job_id': source['job_id'],
                         'model_name': trial['model_name'], 'agent_name': trial.get('agent_name'),
                         'agent_version': trial.get('agent_version'), 'error_type': trial.get('error_type'),
                         'registry_trajectory_path': path})
    selected.sort(key=lambda r: (r['task_id'], r['trial_id']))
    counts = Counter(r['task_id'] for r in selected)
    return {'schema_version': 1, 'kind': 'v7_failure_sources', 'created_at': now(),
            'inputs_sha256': {name: sha(base / name) for name in SOURCE_INPUTS},
            'selection': SELECTION, 'row_count': len(selected),
            'counts_by_task': {task: counts.get(task, 0) for task in split['test_task_ids']},
            'rows': selected}


def write_sources(base: Path = BASE, manifests_root: Path | None = None, *, replace: bool = False) -> tuple[dict, bool]:
    """Write the frozen failure-source manifest; an unchanged selection keeps the existing file."""
    base = Path(base)
    path = Path(manifests_root or base / 'manifests') / SOURCES
    manifest = failure_sources(base)
    if path.exists():
        previous = load(path)
        if {k: v for k, v in previous.items() if k != 'created_at'} == {k: v for k, v in manifest.items() if k != 'created_at'}:
            return previous, False
        if not replace:
            raise ValueError(f'{SOURCES} exists with a different selection; pass --replace to rewrite it')
    save(path, manifest)
    return manifest, True


def load_sources(path: Path, *, base: Path | None = None) -> dict:
    """Load and structurally check the source manifest; with base, also re-hash its inputs."""
    manifest = load(path)
    rows, counts = manifest.get('rows'), manifest.get('counts_by_task')
    if (manifest.get('schema_version') != 1 or manifest.get('kind') != 'v7_failure_sources'
            or not isinstance(rows, list) or not isinstance(counts, dict) or len(rows) != manifest.get('row_count')
            or set(manifest.get('inputs_sha256', {})) != set(SOURCE_INPUTS)):
        raise ValueError('Not a V7 failure-source manifest')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(ROW_FIELDS) or row['trial_id'] in seen or row['task_id'] not in counts:
            raise ValueError('Malformed V7 failure-source row')
        seen.add(row['trial_id'])
    if Counter(r['task_id'] for r in rows) != Counter({k: v for k, v in counts.items() if v}):
        raise ValueError('V7 failure-source counts disagree with its rows')
    if base is not None:
        for name, digest in manifest['inputs_sha256'].items():
            if sha(Path(base) / name) != digest:
                raise ValueError(f'Failure-source input changed since sources ran: {name}')
    return manifest


# --------------------------------------------------------------- 3. download

class HTTPStatusError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f'HTTP {status}: {url}')
        self.status = status


def http_get(url: str) -> bytes:
    """One anonymous GET; never sends credentials (no auth, no ~/.netrc)."""
    try:
        import requests
    except ImportError:  # stdlib fallback; no new dependency
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise HTTPStatusError(exc.code, url) from None
    if not hasattr(LOCAL, 'session'):
        session = requests.Session()
        session.trust_env = False  # no .netrc credentials; environment proxies are kept below
        session.proxies.update(urllib.request.getproxies())
        session.headers['User-Agent'] = USER_AGENT
        LOCAL.session = session
    response = LOCAL.session.get(url, timeout=(15, 90))
    if response.status_code >= 400:
        raise HTTPStatusError(response.status_code, url)
    return response.content


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def request(url: str) -> bytes:
    """Four tries with min(2**n, 8) s backoff; 400/401/403/404 are final answers."""
    last = None
    for attempt in range(4):
        try:
            return http_get(url)
        except (HTTPStatusError, OSError) as exc:  # requests errors are OSError subclasses
            last = exc
            if isinstance(exc, HTTPStatusError) and exc.status in (400, 401, 403, 404):
                break
            if attempt < 3:
                _sleep(min(2 ** attempt, 8))
    raise RuntimeError(str(last))


def viewer_url(trial_id: str, job_id: str) -> str:
    query = urlencode({'jobId': job_id, 'trajectory_path': f'trials/{trial_id}/trajectory.json'})
    return f'{HUB}/api/trials/{trial_id}/trajectory?{query}'


def _bench(data_root: Path) -> Path:
    """Sidecar paths are relative to the benchmark directory holding data/historical_trials."""
    data_root = Path(data_root).absolute()
    if len(data_root.parents) < 2:
        raise ValueError('Data root must be at least two directories deep')
    return data_root.parents[1]


def safe_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError(f'Unsafe public filename: {value!r}')
    return Path(*path.parts)


def download(url: str, path: Path, *, bench: Path, size: int | None = None, kind: str | None = None) -> dict:
    """Port of the archived downloader: reuse a verified copy, else fetch, validate and record provenance."""
    provenance = path.with_name(path.name + '.source.json')
    if path.exists() and provenance.exists():
        previous = load(provenance)
        if (previous.get('url') == url and path.stat().st_size == previous.get('bytes')
                and (size is None or size == path.stat().st_size) and sha(path) == previous.get('sha256')):
            return previous
    data = request(url)
    if size is not None and len(data) != size:
        raise ValueError(f'Size mismatch: {len(data)} != {size}: {url}')
    if kind:
        value = json.loads(data)
        if kind == 'atif' and not (isinstance(value, dict) and isinstance(value.get('steps'), list)):
            raise ValueError('Public trajectory is null or is not valid ATIF')
    info = {'url': url, 'retrieved_at': now(), 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'path': path.relative_to(bench).as_posix()}
    atomic(path, data)
    save(provenance, info)
    return info


def recover_missing(trial: dict, directory: Path, bench: Path) -> dict:
    """Port of audit_missing(): recover a null viewer body from original files, or prove absence.

    A network failure is never proof of absence. An index at the UI's 100-file
    boundary is not considered a complete absence proof either.
    """
    base = HUB + '/api/trials/' + trial['id']
    query = urlencode({'jobId': trial['job_id']})
    viewer = viewer_url(trial['id'], trial['job_id'])
    out = {'trial_id': trial['id'], 'task_name': trial['task_name'], 'status': 'unresolved'}
    evidence = ['trajectory_missing_response.json', 'files.json', 'summary.json']
    try:
        download(viewer, directory / 'trajectory_missing_response.json', bench=bench, kind='json')
        value = json.loads((directory / 'trajectory_missing_response.json').read_bytes())
        if isinstance(value, dict) and isinstance(value.get('steps'), list):
            source = download(viewer, directory / 'trajectory.json', bench=bench, kind='atif')
            return {**out, 'status': 'available', 'recovery': 'viewer_retry', 'source': source}
        if value is not None:
            return {**out, 'reason': 'Unexpected non-null viewer response; not classified as unavailable'}
        download(base + '/files?' + query, directory / 'files.json', bench=bench, kind='json')
        download(base + '/summary?' + query, directory / 'summary.json', bench=bench, kind='json')
        files = json.loads((directory / 'files.json').read_bytes())['files']
        originals = [f for f in files if f['path'].endswith('trajectory.json') and not f.get('is_dir')]
        for file in originals:
            url = base + '/files/' + quote(file['path'], safe='/') + '?' + query
            raw_path = directory / 'native' / safe_path(file['path'])
            source = download(url, raw_path, bench=bench, size=file.get('size'))
            try:
                parsed = json.loads(raw_path.read_bytes())
            except json.JSONDecodeError as exc:
                out.update(status='official_raw_unparsed', source_path=raw_path.relative_to(bench).as_posix(),
                           source_sha256=source['sha256'], source_bytes=source['bytes'], source_url=url,
                           source_format='malformed_atif_json',
                           reason='Official original trajectory is malformed JSON; retained byte-for-byte without repair: ' + str(exc),
                           evidence_paths=[(directory / n).relative_to(bench).as_posix() for n in evidence],
                           inspected_at=now())
                save(directory / 'trajectory_raw_fallback.json', out)
                return out
            if isinstance(parsed, dict) and isinstance(parsed.get('steps'), list):
                source = download(url, directory / 'trajectory.json', bench=bench, size=file.get('size'), kind='atif')
                return {**out, 'status': 'available', 'recovery': 'original_atif', 'source': source}
        agent_files = [f for f in files if f['path'].startswith('agent/') and not f.get('is_dir')]
        if not originals and len(agent_files) == 1 and agent_files[0]['path'] in (
                'agent/claude-code.txt', 'agent/codex.txt', 'agent/gemini-cli.txt'):
            file = agent_files[0]
            url = base + '/files/' + quote(file['path'], safe='/') + '?' + query
            raw_path = directory / 'native' / safe_path(file['path'])
            source = download(url, raw_path, bench=bench, size=file.get('size'))
            out.update(status='official_raw_unparsed', source_format='native_agent_log',
                       source_path=raw_path.relative_to(bench).as_posix(), source_sha256=source['sha256'],
                       source_bytes=source['bytes'], source_url=url, inspected_at=now(),
                       reason='Official viewer has no ATIF; complete original native agent log retained byte-for-byte, without summarization or conversion.',
                       evidence_paths=[(directory / n).relative_to(bench).as_posix() for n in evidence])
            save(directory / 'trajectory_raw_fallback.json', out)
            return out
        if originals or agent_files:
            return {**out, 'reason': 'Original agent files exist; further inspection needed, not unavailable'}
        if len(files) >= 100:
            return {**out, 'reason': 'Potentially truncated public file index; insufficient absence evidence'}
        summary = json.loads((directory / 'summary.json').read_bytes())
        reason = 'Official viewer returns HTTP 200 JSON null; public file index contains no trajectory or agent execution files.'
        failure = (summary.get('exception_info') or {}).get('exception_type') or trial.get('error_type')
        if failure:
            reason += ' Official runtime status: ' + str(failure) + '.'
        paths = [directory / n for n in evidence + ['trial_metadata.json']]
        out.update(status='official_unavailable', reason=reason, inspected_at=now(),
                   evidence_paths=[p.relative_to(bench).as_posix() for p in paths],
                   evidence_sha256={p.relative_to(bench).as_posix(): sha(p) for p in paths})
        save(directory / 'trajectory_unavailable.json', out)
        return out
    except Exception as exc:  # recorded per trial; never classified as absence
        return {**out, 'error': str(exc)}


def fetch_trial(row: dict, trial: dict, data_root: Path) -> dict:
    """Acquire one failed trial: the viewer ATIF, else the audit_missing recovery or absence proof."""
    bench = _bench(data_root)
    directory = Path(data_root).absolute() / row['trial_id']
    directory.mkdir(parents=True, exist_ok=True)
    save(directory / 'trial_metadata.json', trial)
    if row['registry_trajectory_path'] is not None:
        url = viewer_url(row['trial_id'], row['job_id'])
        out = {'trial_id': trial['id'], 'task_name': trial['task_name'], 'mode': 'atif', 'files': [], 'errors': []}
        try:
            out['files'].append(download(url, directory / 'trajectory.json', bench=bench, kind='atif'))
        except Exception as exc:
            out['errors'].append({'url': url, 'error': str(exc)})
        out['status'] = 'partial' if out['errors'] else 'complete'
        out['finished_at'] = now()
        save(directory / 'atif_download.json', out)
        if not out['errors']:
            return {'action': 'downloaded', 'reason': None}
    result = recover_missing(trial, directory, bench)
    return {'action': 'recovered' if result['status'] != 'unresolved' else 'failed',
            'reason': result.get('reason') or result.get('error')}


def _contained(directory: Path, path: Path) -> Path:
    """A regular file inside one trial directory, reached without symbolic links."""
    directory = Path(directory)
    try:
        relative = Path(path).relative_to(directory)
    except ValueError:
        raise ValueError(f'Source evidence escapes its trial directory: {path}') from None
    if not relative.parts or '..' in relative.parts or directory.is_symlink():
        raise ValueError(f'Invalid source evidence path: {path}')
    current = directory
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'Symbolic link in source evidence: {path}')
    if not current.is_file():
        raise ValueError(f'Missing source evidence file: {relative.as_posix()}')
    return current


def validate_download(path: Path, row: dict, data_root: Path, *, expected_url: str | None = None,
                      atif: bool = False) -> dict:
    """Bind downloaded bytes to this official Hub trial/job and source URL (archived validate_download)."""
    data_root = Path(data_root).absolute()
    bench, directory, trial_id = _bench(data_root), data_root / row['trial_id'], row['trial_id']
    path = _contained(directory, Path(path))
    sidecar = _contained(directory, path.with_name(path.name + '.source.json'))
    record = load(sidecar)
    relative = path.relative_to(bench).as_posix()
    value = sha(path)
    if (record.get('path') != relative or type(record.get('bytes')) is not int
            or record['bytes'] != path.stat().st_size or record.get('sha256') != value):
        raise ValueError(f'Download provenance hash/size/path mismatch: {trial_id}')
    url = urlsplit(str(record.get('url') or ''))
    prefix = '/api/trials/' + trial_id + '/'
    query = parse_qs(url.query)
    route = unquote(url.path)
    if (url.scheme != 'https' or url.netloc != 'hub.harborframework.com' or url.fragment
            or not route.startswith(prefix) or query.get('jobId') != [row['job_id']]
            or (expected_url is not None and record['url'] != expected_url)):
        raise ValueError(f'Download provenance URL/trial/job mismatch: {trial_id}')
    endpoint = route[len(prefix):]
    if atif and endpoint != 'trajectory' and not (endpoint.startswith('files/') and endpoint.endswith('/trajectory.json')):
        raise ValueError(f'ATIF provenance points to a different artifact: {trial_id}')
    if endpoint == 'trajectory' and query.get('trajectory_path') != [f'trials/{trial_id}/trajectory.json']:
        raise ValueError(f'ATIF URL targets a different trajectory: {trial_id}')
    if endpoint.startswith('files/') and 'native' in path.relative_to(directory).parts:
        if endpoint != 'files/' + path.relative_to(directory / 'native').as_posix():
            raise ValueError(f'Native source URL/path mismatch: {trial_id}')
    return {'source_path': relative, 'source_sha256': value, 'source_bytes': record['bytes'],
            'source_url': record['url'], 'retrieved_at': record.get('retrieved_at')}


def _metadata(directory: Path, row: dict) -> dict:
    value = load(_contained(directory, directory / 'trial_metadata.json'))
    if (not isinstance(value, dict) or value.get('id') != row['trial_id'] or value.get('job_id') != row['job_id']
            or str(value.get('task_name') or '').rsplit('/', 1)[-1] != row['task_id']
            or value.get('model_name') != row['model_name'] or type(value.get('reward')) not in (int, float)
            or value.get('reward') != 0 or value.get('is_scored') is not True):
        raise ValueError('Trial metadata contradicts its failed source row')
    return value


def _raw_fallback(directory: Path, row: dict, metadata: dict, data_root: Path) -> dict:
    bench = _bench(data_root)
    record = load(_contained(directory, directory / 'trajectory_raw_fallback.json'))
    relative = record.get('source_path')
    if (record.get('trial_id') != row['trial_id'] or record.get('status') != 'official_raw_unparsed'
            or record.get('task_name') != metadata.get('task_name') or not isinstance(relative, str)
            or Path(relative).is_absolute()):
        raise ValueError('Invalid official raw fallback')
    path = _contained(directory, bench / relative)
    if sha(path) != record.get('source_sha256'):
        raise ValueError('Raw fallback source hash mismatch')
    source = validate_download(path, row, data_root, expected_url=record.get('source_url'))
    fmt = record.get('source_format', 'raw_unparsed_json')
    if (source['source_bytes'] != record.get('source_bytes') or source['source_url'] != record.get('source_url')
            or not record.get('reason') or fmt not in RAW_FORMATS):
        raise ValueError('Raw fallback marker/provenance mismatch')
    if not record.get('evidence_paths'):
        raise ValueError('Raw fallback lacks original evidence')
    for item in record['evidence_paths']:
        if not isinstance(item, str) or Path(item).is_absolute():
            raise ValueError('Invalid raw fallback evidence path')
        validate_download(_contained(directory, bench / item), row, data_root)
    native = path.relative_to(directory / 'native').as_posix()
    indexed = load(_contained(directory, directory / 'files.json')).get('files', [])
    if not any(f.get('path') == native and not f.get('is_dir') and f.get('size') == source['source_bytes'] for f in indexed):
        raise ValueError('Raw fallback is absent from the official file index')
    return {**source, 'status': 'raw_fallback', 'source_format': fmt, 'source_reason': record['reason']}


def _unavailable(directory: Path, row: dict, metadata: dict, data_root: Path) -> dict:
    bench = _bench(data_root)
    marker = _contained(directory, directory / 'trajectory_unavailable.json')
    record = load(marker)
    if (record.get('trial_id') != row['trial_id'] or record.get('status') != 'official_unavailable'
            or record.get('task_name') != metadata.get('task_name') or not record.get('evidence_paths')):
        raise ValueError('Invalid unavailability marker')
    for item in record['evidence_paths']:
        if not isinstance(item, str) or Path(item).is_absolute():
            raise ValueError('Invalid unavailability evidence path')
        path = _contained(directory, bench / item)
        if sha(path) != (record.get('evidence_sha256') or {}).get(item):
            raise ValueError('Unavailability evidence hash mismatch')
        if path.name != 'trial_metadata.json':
            validate_download(path, row, data_root)
    indexed = load(_contained(directory, directory / 'files.json')).get('files', [])
    missing = load(_contained(directory, directory / 'trajectory_missing_response.json'))
    if (missing is not None or len(indexed) >= 100 or any(
            str(f.get('path') or '').startswith('agent/') or str(f.get('path') or '').endswith('trajectory.json')
            for f in indexed)):
        raise ValueError('Insufficient official absence evidence')
    return {'status': 'unavailable', 'source_path': marker.relative_to(bench).as_posix(),
            'source_sha256': sha(marker), 'source_bytes': marker.stat().st_size,
            'source_url': viewer_url(row['trial_id'], row['job_id']),
            'retrieved_at': record.get('inspected_at'), 'source_reason': record.get('reason')}


def trial_state(row: dict, data_root: Path) -> dict:
    """Classify one failure source from local files only; trust neither a path nor a score alone."""
    data_root = Path(data_root).absolute()
    directory = data_root / row['trial_id']
    state = {'status': 'unresolved', 'reason': None, 'source_path': None, 'source_sha256': None,
             'source_bytes': None, 'source_url': None, 'retrieved_at': None, 'source_format': None,
             'source_reason': None}
    if not directory.exists() and not directory.is_symlink():
        return {**state, 'reason': 'no local trial directory'}
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('Trial path is not a regular directory')
        names = ('trajectory.json', 'trajectory_raw_fallback.json', 'trajectory_unavailable.json')
        present = [name for name in names if (directory / name).exists() or (directory / name).is_symlink()]
        if not present:
            return {**state, 'reason': 'no local body or availability marker'}
        if len(present) > 1:
            raise ValueError('Conflicting source availability: ' + ', '.join(present))
        metadata = _metadata(directory, row)
        if present[0] == 'trajectory.json':
            info = validate_download(directory / 'trajectory.json', row, data_root, atif=True)
            return {**state, **info, 'status': 'available', 'source_format': 'atif_json'}
        if present[0] == 'trajectory_raw_fallback.json':
            return {**state, **_raw_fallback(directory, row, metadata, data_root)}
        return {**state, **_unavailable(directory, row, metadata, data_root)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {**state, 'status': 'invalid', 'reason': str(exc)}


def download_sources(base: Path = BASE, *, manifests_root: Path | None = None, data_root: Path | None = None,
                     workers: int = 4, limit: int | None = None, trial_ids: list[str] | None = None) -> dict:
    """Fetch every failure source without a verified local body or absence proof; write the audit."""
    base = Path(base)
    if not 1 <= workers <= 4:
        raise ValueError('Use 1-4 bounded network workers')
    manifests = Path(manifests_root or base / 'manifests')
    data_root = Path(data_root or base / 'data/historical_trials').absolute()
    sources_path = manifests / SOURCES
    sources = load_sources(sources_path, base=base)
    public = unique_index(load(base / 'manifests/historical/public_trials.json')['trials'], 'id', 'public trial')
    rows = sources['rows']
    if trial_ids is not None and set(trial_ids) - {r['trial_id'] for r in rows}:
        raise ValueError('Requested trials are not failure sources')
    manifests.mkdir(parents=True, exist_ok=True)
    with (manifests / 'v7_downloader.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another V7 downloader owns v7_downloader.lock') from None
        states = {r['trial_id']: trial_state(r, data_root) for r in rows}
        pending = [r for r in rows if states[r['trial_id']]['status'] in ('unresolved', 'invalid')
                   and (trial_ids is None or r['trial_id'] in trial_ids)]
        if limit is not None:
            pending = pending[:limit]
        actions = {r['trial_id']: ('reused', None) if states[r['trial_id']]['status'] not in ('unresolved', 'invalid')
                   else ('not_attempted', None) for r in rows}

        def one(row):
            result = fetch_trial(row, public[row['trial_id']], data_root)
            return row, result

        with cf.ThreadPoolExecutor(workers) as executor:
            for row, result in executor.map(one, pending):
                actions[row['trial_id']] = (result['action'], result['reason'])
                states[row['trial_id']] = trial_state(row, data_root)
        trials = []
        for row in rows:
            state = states[row['trial_id']]
            status = 'unresolved' if state['status'] in ('unresolved', 'invalid') else state['status']
            action, reason = actions[row['trial_id']]
            trials.append({'task_id': row['task_id'], 'trial_id': row['trial_id'], 'status': status, 'action': action,
                           'sha256': state['source_sha256'], 'bytes': state['source_bytes'],
                           'retrieved_at': state['retrieved_at'], 'url': state['source_url'],
                           'source_path': state['source_path'],
                           'reason': (reason or state['reason']) if status == 'unresolved' else state['source_reason']})
        counts = Counter(t['status'] for t in trials)
        audit = {'schema_version': 1, 'kind': 'v7_download_audit', 'updated_at': now(),
                 'source_manifest_sha256': sha(sources_path), 'data_root': _relative(data_root, base),
                 'counts': {k: counts.get(k, 0) for k in ('available', 'raw_fallback', 'unavailable', 'unresolved')},
                 'complete': not counts.get('unresolved'), 'trials': trials}
        save(manifests / DOWNLOAD_AUDIT, audit)
    return audit


# ------------------------------------------------------------------ 4. build

def render(value, output, images, trial_id, depth=0):
    """Render every JSON field, retaining multiline commands/observations.

    Exact port of the archived prepare_pool.render(); only the image side effect
    differs: blobs are collected in ``images`` (name -> bytes) so the caller can
    place images/<attempt_id>/<name> for published attempts only.
    """
    indent = '  ' * depth
    if isinstance(value, dict):
        if not value:
            output.append(indent+'{}')
        for key, item in value.items():
            output.append(indent+str(key)+':')
            render(item, output, images, trial_id, depth+1)
    elif isinstance(value, list):
        if not value:
            output.append(indent+'[]')
        for i, item in enumerate(value):
            output.append(indent+f'[{i}]')
            render(item, output, images, trial_id, depth+1)
    elif isinstance(value, str):
        match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)', value)
        if match:
            blob = base64.b64decode(match[2], validate=False)
            name = hashlib.sha256(blob).hexdigest()+'.'+match[1]
            images[name] = blob
            value = f'/pool/images/{trial_id}/{name}'
        output.extend(indent+line for line in (value.splitlines() or ['""']))
    else:
        output.append(indent+json.dumps(value, ensure_ascii=False))


def attempt_header(task_id: str, source: dict, attempt_id: str, task_version_id: str) -> list[str]:
    """The eight V7 header lines (the last blank); no Hub trial ID appears."""
    return [TITLE + task_id,
            f'Model: {source["model_name"]}; agent: {source["agent_name"]} {source["agent_version"]}',
            f'Official reward: 0; attempt ID: {attempt_id}',
            SCORED_LINE,
            COMPATIBILITY + task_version_id,
            NOTE_LINE, ATIF_LINE, '']


def render_atif(data: dict, header: list[str], image_key: str) -> tuple[bytes, dict, bool]:
    """Header plus the all-field body; returns (bytes, images, whether unencodable text was escaped)."""
    lines, images = list(header), {}
    render(data, lines, images, image_key)
    text = '\n'.join(lines) + '\n'
    try:
        return text.encode('utf-8'), images, False
    except UnicodeEncodeError:  # lone surrogates from JSON escapes; escape rather than drop text
        return text.encode('utf-8', 'backslashreplace'), images, True


def render_raw(header: list[str], source_format: str, reason: str, raw: bytes) -> bytes:
    """Header lines 1-6, source format, availability reason, blank line, then the original bytes."""
    line = NATIVE_LINE if source_format == 'native_agent_log' else UNPARSED_LINE
    return ('\n'.join(header[:6] + [line, REASON_PREFIX + reason, '']) + '\n').encode() + raw


def card_prefix(row: dict, instruction: str) -> list[str]:
    """Title, preclassification and verbatim instruction, exactly as the training cards begin."""
    classification = []
    if row.get('class_label'):
        classification = [
            '## Preclassification (researcher-defined; not an official benchmark category)',
            f'Class: {row["class_id"]} · {row["class_label"]}',
            f'Fine family: {row.get("fine_family", "unspecified")}',
            'Transfer tags: ' + ', '.join(row.get('transfer_tags', [])),
            'Use the current task constraints to judge applicability; shared category is not proof of transfer.', '']
    return [f'# {row["task_id"]}', '', *classification, instruction, '']


def task_card(row: dict, instruction: str, listed: list[dict]) -> str:
    lines = card_prefix(row, instruction) + ['## Historical trajectories', COUNT_LINE.format(len(listed)), KEEP_LINE, '']
    for item in sorted(listed, key=lambda item: item['pool_file'] or item['attempt_id']):
        location = '/pool/' + item['pool_file'] if item['pool_file'] else UNAVAILABLE_ROW
        lines.append(f'- {location} | score=0 | model={item["model_name"]} | scored=True | attempt={item["attempt_id"]}')
    return '\n'.join(lines) + '\n'


def pool_manifest(task_id: str, *, trajectories: int, listed: int, unavailable: int, raw: int) -> dict:
    return {'schema_version': 1, 'benchmark': 'Terminal-Bench 2.1', 'kind': POOL_KIND, 'task_id': task_id,
            'trajectory_count': trajectories, 'source_record_count': listed,
            'confirmed_unavailable_count': unavailable, 'raw_unparsed_trajectory_count': raw,
            'successful_trajectories_included': False, 'trajectory_format': TRAJECTORY_FORMAT,
            'trajectory_path': TRAJECTORY_PATH, 'mutable_memory': '/memory', 'cross_task_memory': False}


def pool_readme(task_id: str, manifest: dict) -> str:
    return (f'# TB2.1 V7 same-task failure pool: {task_id}\n\n'
            f'This pool holds only officially graded failed attempts (official reward 0) at the current task: '
            f'{manifest["trajectory_count"]} trajectory bodies from {manifest["source_record_count"]} listed source records.\n'
            f'{manifest["confirmed_unavailable_count"]} officially unavailable bodies are marked on the task card, never fabricated.\n'
            'No successful attempt is included, and no other task, hidden test or reference solution.\n'
            'The pool is immutable; all mutable experience belongs in /memory for this task only.\n')


def write_task_pool(pool: Path, row: dict, instruction: str, items: list[dict]) -> dict:
    """Write one solver-visible pool from prepared attempts.

    Each item has status, attempt_id, model_alias and model_name; published
    bodies ('available'/'raw_fallback') also carry sanitized 'data' bytes and
    'images'. Other statuses except 'unavailable' are neither written nor listed.
    Sets item['pool_file'] and returns the pool manifest.
    """
    pool, task_id = Path(pool), row['task_id']
    (pool / 'tasks').mkdir(parents=True)
    (pool / 'trajectories' / task_id).mkdir(parents=True)
    listed = []
    for item in items:
        item['pool_file'] = None
        if item['status'] in ('available', 'raw_fallback'):
            if not valid_alias(item['model_alias']) or not ATTEMPT.fullmatch(item['attempt_id']):
                raise ValueError('Unsafe model alias or attempt ID')
            relative = f'trajectories/{task_id}/{item["model_alias"]}/{item["attempt_id"]}/{task_id}_0.txt'
            (pool / relative).parent.mkdir(parents=True)
            (pool / relative).write_bytes(item['data'])
            for name, blob in sorted(item.get('images', {}).items()):
                image = pool / 'images' / item['attempt_id'] / name
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(blob)
            item['pool_file'] = relative
        if item['status'] in ('available', 'raw_fallback', 'unavailable'):
            listed.append(item)
    bodies = [item for item in listed if item['pool_file']]
    manifest = pool_manifest(task_id, trajectories=len(bodies), listed=len(listed),
                             unavailable=len(listed) - len(bodies),
                             raw=sum(item['status'] == 'raw_fallback' for item in bodies))
    (pool / 'tasks' / f'{task_id}.md').write_bytes(task_card(row, instruction, listed).encode())
    (pool / 'manifest.json').write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode())
    (pool / 'README.md').write_bytes(pool_readme(task_id, manifest).encode())
    for path in pool.rglob('*'):
        if path.is_file():
            path.chmod(0o444)
    return manifest


def _text(path: Path) -> tuple[str | None, str | None]:
    """UTF-8 text of a task file, or why decontamination skips it."""
    if path.is_symlink():
        raise ValueError(f'Symbolic link in a task package: {path}')
    if path.stat().st_size > TEXT_LIMIT:
        return None, 'larger_than_2_mb'
    data = path.read_bytes()
    if b'\0' in data:
        return None, 'binary'
    try:
        return data.decode('utf-8'), None
    except UnicodeDecodeError:
        return None, 'binary'


def shingles(texts: list[str], size: int) -> set[tuple[str, ...]]:
    result = set()
    for text in texts:
        tokens = TOKEN.findall(text)
        result.update(tuple(tokens[i:i + size]) for i in range(len(tokens) - size + 1))
    return result


def reference_material(task_dir: Path) -> tuple[dict, list[dict]]:
    """Host-only shingle sets: solution and tests/README text minus agent-visible text."""
    task_dir = Path(task_dir)
    groups = {'tests': task_dir / 'tests', 'solution': task_dir / 'solution', 'environment': task_dir / 'environment'}
    paths = {name: sorted(p for p in path.rglob('*') if p.is_file()) if path.is_dir() else []
             for name, path in groups.items()}
    paths['readme'] = [task_dir / 'README.md'] if (task_dir / 'README.md').is_file() else []
    paths['instruction'] = [task_dir / 'instruction.md']
    texts, files = {name: [] for name in paths}, []
    for group, items in paths.items():
        for path in items:
            text, skipped = _text(path)
            files.append({'group': group, 'path': path.relative_to(task_dir).as_posix(),
                          'bytes': path.stat().st_size, 'sha256': sha(path), 'skipped': skipped})
            if text is not None:
                texts[group].append(text)
    visible = texts['instruction'] + texts['environment']
    references = {'solution': shingles(texts['solution'], SOLUTION_SHINGLE) - shingles(visible, SOLUTION_SHINGLE),
                  'tests': shingles(texts['tests'] + texts['readme'], TESTS_SHINGLE) - shingles(visible, TESTS_SHINGLE)}
    return references, files


def _field_hint(text: str, newlines: list[int], line: int) -> str:
    """Enclosing rendered ATIF key names of a 1-based line: a review hint built from key names only."""
    def get(number):
        start = newlines[number - 2] + 1 if number > 1 else 0
        return text[start:newlines[number - 1] if number <= len(newlines) else len(text)]

    current = get(line)
    depth, path = len(current) - len(current.lstrip(' ')), []
    for number in range(line - 1, 0, -1):
        if depth == 0:
            break
        candidate = get(number)
        indent = len(candidate) - len(candidate.lstrip(' '))
        if indent < depth and candidate.strip():
            depth = indent
            key = KEY_LINE.fullmatch(candidate.strip())
            if key:
                path.append(key[1])
    return '/'.join(reversed(path))


def contamination(text: str, references: dict) -> dict:
    """Leakage metrics for one rendered trajectory; offsets are line numbers, never reference text."""
    fetch = [(m.start(), m.group()) for m in FETCH.finditer(text)]
    refs = sorted((m.start(), m.group()) for literal in TEST_REFERENCES
                  for m in re.finditer(re.escape(literal), text))
    tokens = TOKEN.findall(text)
    solution = references['solution']
    solution_hits = _positions(tokens, solution, SOLUTION_SHINGLE)
    shared, solution_at = set(), []
    for i in solution_hits:
        shingle = tuple(tokens[i:i + SOLUTION_SHINGLE])
        if shingle not in shared:
            shared.add(shingle)
            solution_at.append(i)
    best, span = _longest_run(_positions(tokens, references['tests'], TESTS_SHINGLE), TESTS_SHINGLE)
    metrics = {'fetch_hits': len(fetch), 'test_file_refs': len(refs),
               'solution_overlap': len(shared), 'tests_overlap_max_run': best}
    reasons = [name for name, minimum in THRESHOLDS.items() if metrics[name] >= minimum]
    newlines = [m.start() for m in re.finditer('\n', text)] if (fetch or refs or solution_at or span) else []
    starts = [m.start() for m in TOKEN.finditer(text)] if (solution_at or span) else []

    def line(offset):
        return bisect.bisect_left(newlines, offset) + 1

    def at(offset, **extra):
        number = line(offset)
        return {'line': number, **extra, 'field_hint': _field_hint(text, newlines, number)}

    offsets = {'fetch_hits': [at(o, match=m) for o, m in fetch[:OFFSET_LIMIT]],
               'test_file_refs': [at(o, match=m) for o, m in refs[:OFFSET_LIMIT]],
               'solution_overlap': [at(starts[i]) for i in solution_at[:OFFSET_LIMIT]],
               'tests_overlap_max_run': ({'line_start': line(starts[span[0]]), 'line_end': line(starts[span[1]]),
                                          'field_hint': _field_hint(text, newlines, line(starts[span[0]]))}
                                         if span else None)}
    # Review aids only; the flag rule above is the spec's.
    review = {'solution_overlap_max_run': _longest_run(solution_hits, SOLUTION_SHINGLE)[0],
              'solution_coverage': round(len(shared) / len(solution), 4) if solution else 0.0}
    return {**metrics, 'flagged': bool(reasons), 'reasons': reasons, 'offsets': offsets, 'review': review}


def _positions(tokens: list[str], shingle_set: set, size: int) -> list[int]:
    if not shingle_set:
        return []
    firsts = {s[0] for s in shingle_set}
    return [i for i in range(len(tokens) - size + 1)
            if tokens[i] in firsts and tuple(tokens[i:i + size]) in shingle_set]


def _longest_run(marked: list[int], size: int) -> tuple[int, tuple[int, int] | None]:
    """Consecutive matching shingles form one run of shared consecutive tokens."""
    best, span, start = 0, None, None
    for position, i in enumerate(marked):
        if start is None or i != marked[position - 1] + 1:
            start = i
        if i - start + size > best:
            best, span = i - start + size, (start, i + size - 1)
    return best, span


def load_decisions(path: Path, sources: dict) -> tuple[dict, str | None]:
    """Reviewed decontamination decisions: {trial_id: {"decision": "keep"|"exclude", "reason": ...}}."""
    path = Path(path)
    if not path.exists():
        return {}, None
    value = load(path)
    known = {row['trial_id'] for row in sources['rows']}
    if not isinstance(value, dict):
        raise ValueError('Decontamination decisions must be a JSON object')
    for trial_id, decision in value.items():
        if trial_id not in known:
            raise ValueError(f'Decontamination decision for an unknown failure source: {trial_id}')
        if (not isinstance(decision, dict) or decision.get('decision') not in ('keep', 'exclude')
                or not isinstance(decision.get('reason'), str) or not decision['reason'].strip()):
            raise ValueError(f'Decontamination decision needs keep/exclude and a reason: {trial_id}')
    return value, sha(path)


def _fixtures(base: Path) -> tuple[set, list]:
    """Public FAKE_* benchmark fixtures preserved by the sanitizer, when their source verifies."""
    try:
        rows = load(Path(base) / 'manifests/task_files.json')
        expected = {f"prepared/tasks/{t['task_name']}/{f['path']}": f['sha256'] for t in rows for f in t['files']}
        return package_data.benchmark_fixtures(Path(base), expected)
    except (OSError, KeyError, TypeError, ValueError, SyntaxError):
        return set(), []


def task_instruction(base: Path, row: dict) -> tuple[Path, str]:
    relative = row['source_path']
    if relative != 'prepared/tasks/' + row['task_id']:
        raise ValueError('Task path differs from its fixed package location')
    directory = _protocol().local_path(base, relative)
    if sha(directory / 'instruction.md') != row['instruction_sha256']:
        raise ValueError(f'Official instruction changed: {row["task_id"]}')
    return directory, (directory / 'instruction.md').read_text(encoding='utf-8')


def prepare_attempt(item: dict, source: dict, state: dict, row: dict, *, data_root: Path, references: dict,
                    known: set, fixtures: set, decision: dict | None) -> dict:
    """Render, sanitize and scan one body; decide whether it is published."""
    raw = (_bench(data_root) / state['source_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != state['source_sha256']:
        raise ValueError(f'Source body changed during the build: {source["trial_id"]}')
    header = attempt_header(row['task_id'], source, item['attempt_id'], row['task_version_id'])
    escaped = False
    if state['status'] == 'raw_fallback':
        rendered, images = render_raw(header, state['source_format'], state['source_reason'], raw), {}
    else:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get('steps'), list):
            raise ValueError(f'Invalid ATIF: {source["trial_id"]}')
        rendered, images, escaped = render_atif(value, header, item['attempt_id'])
    data, counts = package_data.sanitize(rendered, known, fixtures)
    if package_data.sanitize(data, known, fixtures)[1]:
        raise ValueError(f'Credential pattern remains after sanitizing: {source["trial_id"]}')
    if any(package_data.sanitize(blob, known, fixtures)[1] for blob in images.values()):
        raise ValueError(f'Credential candidate inside an embedded image requires review: {source["trial_id"]}')
    check = contamination(data.decode('utf-8', 'replace'), references)
    choice = (decision or {}).get('decision')
    excluded = choice == 'exclude' or (check['flagged'] and choice != 'keep')
    return {**item, 'status': 'excluded' if excluded else state['status'], 'data': data, 'images': images,
            'rendered_sha256': hashlib.sha256(rendered).hexdigest(), 'sanitized_sha256': hashlib.sha256(data).hexdigest(),
            'redactions': dict(sorted(counts.items())), 'unencodable_text_escaped': escaped,
            'decontamination': check, 'decision': decision,
            'exclusion_reason': None if not excluded else ('reviewed_decision' if choice == 'exclude' else 'decontamination_flag')}


def build_task(row: dict, sources: list[dict], states: dict, decisions: dict, *, base: Path, data_root: Path,
               pool: Path, pool_path: str, build_id: str, known: set, fixtures: tuple[set, list]) -> tuple[dict, dict]:
    task_id = row['task_id']
    task_dir, instruction = task_instruction(base, row)
    references, reference_files = reference_material(task_dir)
    ordered = sorted(sources, key=lambda s: (alias(s['model_name']), s['trial_id']))
    items = []
    for number, source in enumerate(ordered, 1):
        state = states[source['trial_id']]
        item = {'attempt_id': f'attempt-{number:03d}', 'trial_id': source['trial_id'],
                'model_name': source['model_name'], 'model_alias': alias(source['model_name']),
                'status': state['status'], 'decontamination': None, 'decision': decisions.get(source['trial_id']),
                'exclusion_reason': None}
        if state['status'] in ('available', 'raw_fallback'):
            item = prepare_attempt(item, source, state, row, data_root=data_root, references=references,
                                   known=known, fixtures=fixtures[0], decision=decisions.get(source['trial_id']))
        items.append(item)
    manifest = write_task_pool(pool, row, instruction, items)
    checked = verify_task_pool(pool, task_id)
    for item in items:
        item['pool_sha256'] = item['sanitized_sha256'] if item['pool_file'] else None
    records = [{'attempt_id': i['attempt_id'], 'trial_id': i['trial_id'], 'model_name': i['model_name'],
                'status': i['status'], 'pool_file': i['pool_file'],
                'source_sha256': states[i['trial_id']]['source_sha256'], 'pool_sha256': i['pool_sha256']}
               for i in items]
    record = {'pool_path': pool_path, 'pool_manifest_sha256': checked['pool_manifest_sha256'],
              'pool_files_sha256': checked['files_sha256'], 'trajectory_count': manifest['trajectory_count'],
              'source_record_count': manifest['source_record_count'],
              'excluded_count': sum(i['status'] == 'excluded' for i in items), 'records': records}
    by_id = {s['trial_id']: s for s in sources}
    audit_records = []
    for item in items:
        source, state = by_id[item['trial_id']], states[item['trial_id']]
        audit_records.append({
            'attempt_id': item['attempt_id'], 'trial_id': item['trial_id'], 'job_id': source['job_id'],
            'model_name': source['model_name'], 'model_alias': item['model_alias'], 'agent_name': source['agent_name'],
            'agent_version': source['agent_version'], 'error_type': source['error_type'], 'status': item['status'],
            'source_status': state['status'], 'source_format': state['source_format'],
            'source_path': state['source_path'], 'source_sha256': state['source_sha256'],
            'source_bytes': state['source_bytes'], 'source_url': state['source_url'],
            'source_reason': state['source_reason'] or state['reason'], 'pool_file': item['pool_file'],
            'pool_sha256': item['pool_sha256'], 'sanitized_sha256': item.get('sanitized_sha256'),
            'rendered_sha256': item.get('rendered_sha256'), 'redactions': item.get('redactions', {}),
            'unencodable_text_escaped': item.get('unencodable_text_escaped', False),
            'images': [{'name': n, 'sha256': hashlib.sha256(b).hexdigest(), 'bytes': len(b)}
                       for n, b in sorted(item.get('images', {}).items())],
            'decontamination': item['decontamination'], 'decision': item['decision'],
            'exclusion_reason': item['exclusion_reason']})
    audit = {'schema_version': 1, 'kind': 'v7_task_audit', 'build_id': build_id, 'task_id': task_id,
             'task_version_id': row['task_version_id'], 'instruction_sha256': row['instruction_sha256'],
             'attempt_order': 'sorted by (model_alias, trial_id); attempt-NNN from 1',
             'sanitizer': {'known_local_credential_values': len(known),
                           'public_fixture_exceptions': fixtures[1], 'patterns': sorted(package_data.PATTERNS)},
             'decontamination': {'thresholds': THRESHOLDS, 'solution_shingle_tokens': SOLUTION_SHINGLE,
                                 'tests_shingle_tokens': TESTS_SHINGLE, 'fetch_pattern': FETCH.pattern,
                                 'fetch_case_insensitive': True, 'test_file_references': list(TEST_REFERENCES),
                                 'files': reference_files},
             'counts': dict(Counter(i['status'] for i in items)), 'records': audit_records}
    return record, audit


def _check_download_audit(audit: dict, states: dict, sources_sha: str, data_root: str) -> None:
    if (audit.get('kind') != 'v7_download_audit' or audit.get('source_manifest_sha256') != sources_sha
            or audit.get('data_root') != data_root):
        raise ValueError('Download audit belongs to another failure-source manifest or data root')
    recorded = {row['trial_id']: row['status'] for row in audit.get('trials', [])}
    for trial_id, state in states.items():
        if recorded.get(trial_id) != state['status']:
            raise ValueError(f'Download audit disagrees with local data for {trial_id}; re-run download')


def build_corpus(out: Path, *, base: Path = BASE, manifests_root: Path | None = None, data_root: Path | None = None,
                 decisions_path: Path | None = None, tasks: list[str] | None = None, allow_unresolved: bool = False,
                 credentials_root: Path = ROOT) -> dict:
    """Build every test task's pool, audit and the host-only index; never overwrite a build."""
    base = Path(base).absolute()
    out = _under(base, out).absolute()
    if out.parent.name != 'v7_corpus' or out.parents[1].name != 'prepared' or not BUILD_ID.fullmatch(out.name):
        raise ValueError('Build output must be <root>/prepared/v7_corpus/<build_id>')
    if out.exists() or out.is_symlink():
        raise FileExistsError(f'Refuse to overwrite an existing V7 corpus build: {out}')
    build_id = out.name
    manifests = Path(manifests_root or base / 'manifests')
    data_root = Path(data_root or base / 'data/historical_trials').absolute()
    split_path = base / 'manifests/split.json'
    split = load(split_path)
    test = list(split['test_task_ids'])
    rows = {row['task_id']: row for row in split['tasks']}
    selected = test if tasks is None else [task for task in test if task in set(tasks)]
    if tasks is not None and set(tasks) - set(test):
        raise ValueError('Only frozen test tasks have V7 pools')
    sources_path = manifests / SOURCES
    sources = load_sources(sources_path, base=base)
    if set(sources['counts_by_task']) != set(test):
        raise ValueError('Failure sources cover a different test-task set')
    by_task = {task: [] for task in test}
    for source in sources['rows']:
        by_task[source['task_id']].append(source)
    if decisions_path and not _under(base, decisions_path).exists():
        # A mistyped explicit path must not silently mean "no reviewed decisions".
        raise FileNotFoundError(f'Decontamination decisions file not found: {decisions_path}')
    decisions, decisions_sha = load_decisions(_under(base, decisions_path) if decisions_path else out.parent / DECISIONS, sources)
    states = {s['trial_id']: trial_state(s, data_root) for task in selected for s in by_task[task]}
    invalid = sorted(t for t, s in states.items() if s['status'] == 'invalid')
    if invalid:
        raise ValueError(f'Local evidence fails validation for {len(invalid)} sources (first {invalid[0]}: '
                         f'{states[invalid[0]]["reason"]}); re-run download')
    unresolved = sorted(t for t, s in states.items() if s['status'] == 'unresolved')
    if unresolved and not allow_unresolved:
        raise ValueError(f'{len(unresolved)} failure sources are unresolved; run download (no partial corpus published)')
    audit_path = manifests / DOWNLOAD_AUDIT
    audit_sha = None
    if audit_path.exists():
        _check_download_audit(load(audit_path), states, sha(sources_path), _relative(data_root, base))
        audit_sha = sha(audit_path)
    known = package_data.local_credentials(Path(credentials_root))
    fixtures = _fixtures(base)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f'.{build_id}.building.', dir=out.parent))
    try:
        staging.chmod(0o755)
        records = {}
        for task_id in selected:
            pool_path = f'prepared/v7_corpus/{build_id}/pools/{task_id}'
            record, audit = build_task(rows[task_id], by_task[task_id], states, decisions, base=base,
                                       data_root=data_root, pool=staging / 'pools' / task_id, pool_path=pool_path,
                                       build_id=build_id, known=known, fixtures=fixtures)
            records[task_id] = record
            save(staging / 'audit' / f'{task_id}.json', audit)
        index = {'schema_version': 1, 'kind': INDEX_KIND, 'build_id': build_id, 'created_at': now(),
                 'complete': tasks is None and not unresolved,
                 'split_sha256': sha(split_path), 'source_manifest_sha256': sha(sources_path),
                 'download_audit_sha256': audit_sha, 'decisions_sha256': decisions_sha, 'tasks': records}
        save(staging / 'index.json', index)
        for path in staging.rglob('*'):
            if path.is_file():
                path.chmod(0o444)
        if out.exists() or out.is_symlink():
            raise FileExistsError(f'Refuse to overwrite an existing V7 corpus build: {out}')
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return index


# ----------------------------------------------------------- 4.7 verification

def _check_manifest(manifest, task_id: str) -> None:
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise ValueError('V7 pool manifest has unexpected or missing fields')
    if (manifest['schema_version'] != 1 or manifest['benchmark'] != 'Terminal-Bench 2.1'
            or manifest['kind'] != POOL_KIND or manifest['task_id'] != task_id
            or manifest['cross_task_memory'] is not False or manifest['successful_trajectories_included'] is not False
            or manifest['mutable_memory'] != '/memory' or manifest['trajectory_path'] != TRAJECTORY_PATH
            or manifest['trajectory_format'] != TRAJECTORY_FORMAT):
        raise ValueError('V7 pool manifest is not this task\'s failure-only pool')
    counts = ('trajectory_count', 'source_record_count', 'confirmed_unavailable_count', 'raw_unparsed_trajectory_count')
    if (any(type(manifest[key]) is not int or manifest[key] < 0 for key in counts)
            or manifest['source_record_count'] != manifest['trajectory_count'] + manifest['confirmed_unavailable_count']
            or manifest['raw_unparsed_trajectory_count'] > manifest['trajectory_count']):
        raise ValueError('V7 pool manifest counts are inconsistent')


def _check_header(path: Path, task_id: str, attempt_id: str) -> bool:
    """True for a raw/native fallback, False for rendered ATIF; refuse anything else."""
    with path.open('rb') as stream:
        head = stream.read(65536).split(b'\n')[:9]
    try:
        lines = [line.decode('utf-8') for line in head]
    except UnicodeDecodeError:
        raise ValueError('V7 trajectory header must be UTF-8') from None
    lines += [None] * (9 - len(lines))
    if (lines[0] != TITLE + task_id or not str(lines[1]).startswith('Model: ')
            or lines[2] != f'Official reward: 0; attempt ID: {attempt_id}' or lines[3] != SCORED_LINE
            or not str(lines[4]).startswith(COMPATIBILITY) or lines[5] != NOTE_LINE):
        raise ValueError(f'V7 trajectory header is not an official scored failure of {task_id}: {attempt_id}')
    if lines[6] == ATIF_LINE and lines[7] == '':
        return False
    if lines[6] in (NATIVE_LINE, UNPARSED_LINE) and str(lines[7]).startswith(REASON_PREFIX) and lines[8] == '':
        return True
    raise ValueError(f'V7 trajectory header has an unknown source format: {attempt_id}')


def _pool_trajectories(pool: Path, task_id: str) -> dict:
    found = {}
    for model_dir in sorted((pool / 'trajectories' / task_id).iterdir()):
        if not model_dir.is_dir() or not valid_alias(model_dir.name) or not any(model_dir.iterdir()):
            raise ValueError('V7 trajectories must be <task_id>/<model_alias>/<attempt_id>/ directories')
        for attempt_dir in sorted(model_dir.iterdir()):
            match = ATTEMPT.fullmatch(attempt_dir.name)
            if (not attempt_dir.is_dir() or not match or attempt_dir.name != f'attempt-{int(match[1]):03d}'
                    or attempt_dir.name in found):
                raise ValueError('V7 trajectories need unique attempt-NNN directories')
            entries = [p.name for p in attempt_dir.iterdir()]
            path = attempt_dir / f'{task_id}_0.txt'
            if entries != [path.name] or not path.is_file():
                raise ValueError(f'V7 attempt directory must hold exactly {task_id}_0.txt')
            found[attempt_dir.name] = {'path': path, 'relative': path.relative_to(pool).as_posix(),
                                       'raw': _check_header(path, task_id, attempt_dir.name)}
    return found


def _check_images(pool: Path, trajectories: dict) -> None:
    images = pool / 'images'
    if not images.exists():
        return
    if not images.is_dir() or not any(images.iterdir()):
        raise ValueError('V7 pool images must be a non-empty directory')
    for attempt_dir in images.iterdir():
        files = list(attempt_dir.iterdir()) if attempt_dir.is_dir() else []
        if attempt_dir.name not in trajectories or not files:
            raise ValueError('V7 pool image directory has no published trajectory')
        text = trajectories[attempt_dir.name]['path'].read_bytes()
        for image in files:
            match = IMAGE.fullmatch(image.name)
            if not image.is_file() or not match or sha(image) != match[1]:
                raise ValueError('Invalid V7 pool image')
            if f'/pool/images/{attempt_dir.name}/{image.name}'.encode() not in text:
                raise ValueError('V7 pool image is not referenced by its trajectory')


def _check_card(pool: Path, task_id: str, manifest: dict, trajectories: dict) -> None:
    text = (pool / 'tasks' / f'{task_id}.md').read_bytes().decode('utf-8')
    marker = '\n## Historical trajectories\n'
    if not text.startswith(f'# {task_id}\n') or marker not in text:
        raise ValueError('V7 task card is malformed')
    section = text.rsplit(marker, 1)[1].split('\n')
    if section[:2] != [COUNT_LINE.format(manifest['source_record_count']), KEEP_LINE]:
        raise ValueError('V7 task card count differs from its manifest')
    seen, bodies, unavailable = set(), set(), 0
    for line in filter(None, section[2:]):
        match = CARD_ROW.fullmatch(line)
        if not match or match[3] in seen:
            raise ValueError('V7 task card lists an unexpected or repeated record')
        seen.add(match[3])
        if match[1] == UNAVAILABLE_ROW and match[3] not in trajectories:
            unavailable += 1
        elif match[3] in trajectories and match[1] == '/pool/' + trajectories[match[3]]['relative']:
            bodies.add(match[3])
        else:
            raise ValueError('V7 task card lists a path that is not a pool trajectory')
    if bodies != set(trajectories) or unavailable != manifest['confirmed_unavailable_count']:
        raise ValueError('V7 task card does not list exactly the pool records')


def verify_task_pool(pool_dir: Path, task_id: str, expected: dict | None = None) -> dict:
    """Refuse anything but this task's own card and officially failed attempts (spec 4.7)."""
    pool = Path(pool_dir)
    if pool.is_symlink() or not pool.is_dir():
        raise ValueError('V7 pool must be a regular directory')
    for directory, dirs, files in os.walk(pool):
        if any(os.path.islink(os.path.join(directory, name)) for name in dirs + files):
            raise ValueError('Symbolic links are not allowed in a V7 pool')
    top = {p.name for p in pool.iterdir()}
    required = {'manifest.json', 'README.md', 'tasks', 'trajectories'}
    if not required <= top or top - required - {'images'}:
        raise ValueError('V7 pool top level must be manifest.json, README.md, tasks, trajectories and optional images')
    if not ((pool / 'manifest.json').is_file() and (pool / 'README.md').is_file()
            and (pool / 'tasks').is_dir() and (pool / 'trajectories').is_dir()):
        raise ValueError('V7 pool entries have the wrong file types')
    manifest = load(pool / 'manifest.json')
    _check_manifest(manifest, task_id)
    if [p.name for p in (pool / 'tasks').iterdir()] != [f'{task_id}.md'] or not (pool / 'tasks' / f'{task_id}.md').is_file():
        raise ValueError('V7 pool tasks/ must hold exactly the current task card')
    if [p.name for p in (pool / 'trajectories').iterdir()] != [task_id] or not (pool / 'trajectories' / task_id).is_dir():
        raise ValueError('V7 pool trajectories/ must hold exactly the current task directory')
    trajectories = _pool_trajectories(pool, task_id)
    if (len(trajectories) != manifest['trajectory_count']
            or sum(t['raw'] for t in trajectories.values()) != manifest['raw_unparsed_trajectory_count']):
        raise ValueError('V7 pool trajectory count differs from its manifest')
    _check_images(pool, trajectories)
    _check_card(pool, task_id, manifest, trajectories)
    files = _protocol().inventory(pool)
    manifest_sha = sha(pool / 'manifest.json')
    if expected is not None and (files != expected.get('pool_files_sha256')
                                 or manifest_sha != expected.get('pool_manifest_sha256')
                                 or expected.get('trajectory_count') != len(trajectories)):
        raise ValueError(f'V7 pool differs from its frozen corpus record: {task_id}')
    return {'pool_manifest_sha256': manifest_sha, 'files_sha256': files, 'trajectory_count': len(trajectories)}


def _official_rows(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(newline='', encoding='utf-8') as stream:
        return {row['trial_id']: row for row in csv.DictReader(stream)}


def _check_records(task_id: str, record: dict, checked: dict, sources: dict, official: dict) -> None:
    items = record.get('records')
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(f'V7 corpus records are malformed: {task_id}')
    ids = [item.get('trial_id') for item in items]
    attempts = [item.get('attempt_id') for item in items]
    if len(set(ids)) != len(ids) or set(ids) != set(sources):
        raise ValueError(f'V7 corpus records differ from the failure sources: {task_id}')
    if len(set(attempts)) != len(attempts) or not all(isinstance(a, str) and ATTEMPT.fullmatch(a) for a in attempts):
        raise ValueError(f'V7 corpus attempt IDs are malformed: {task_id}')
    counts, published = Counter(), set()
    for item in items:
        status, trial_id = item.get('status'), item['trial_id']
        if status not in RECORD_STATUSES:
            raise ValueError(f'V7 corpus record has an unpublishable status: {trial_id}')
        row = official.get(trial_id)
        if row is not None and (row.get('reward') != '0' or row.get('is_scored') != 'True'):
            raise ValueError(f'V7 corpus record is not an official scored failure: {trial_id}')
        if item.get('model_name') != sources[trial_id]['model_name']:
            raise ValueError(f'V7 corpus record model differs from its source: {trial_id}')
        relative = item.get('pool_file')
        if status in ('available', 'raw_fallback'):
            parts = PurePosixPath(relative).parts if isinstance(relative, str) else ()
            if (len(parts) != 5 or parts[3] != item['attempt_id']
                    or checked['files_sha256'].get(relative) != item.get('pool_sha256')):
                raise ValueError(f'V7 corpus record does not match its pool file: {trial_id}')
            published.add(relative)
        elif relative is not None:
            raise ValueError(f'Unpublished V7 record names a pool file: {trial_id}')
        counts[status] += 1
    if published != {name for name in checked['files_sha256'] if name.startswith('trajectories/')}:
        raise ValueError(f'V7 pool holds trajectories without a corpus record: {task_id}')
    bodies = counts['available'] + counts['raw_fallback']
    if (record.get('trajectory_count') != bodies or record.get('source_record_count') != bodies + counts['unavailable']
            or record.get('excluded_count') != counts['excluded']):
        raise ValueError(f'V7 corpus record counts are inconsistent: {task_id}')


def verify_corpus(build_root: Path, split: dict) -> dict:
    """Check a frozen corpus against the split, failure sources and official table; return its index."""
    root = Path(build_root).absolute()
    if root.parent.name != 'v7_corpus' or root.parents[1].name != 'prepared':
        raise ValueError('V7 corpus must live at prepared/v7_corpus/<build_id>')
    base = root.parents[2]
    _protocol().local_path(base, f'prepared/v7_corpus/{root.name}')
    if root.is_symlink() or not root.is_dir():
        raise ValueError('V7 corpus must be a regular directory')
    index = load(root / 'index.json')
    if (index.get('schema_version') != 1 or index.get('kind') != INDEX_KIND or index.get('build_id') != root.name
            or index.get('complete', True) is not True):
        raise ValueError('Not a complete V7 corpus index')
    tests, tasks = list(split['test_task_ids']), index.get('tasks')
    if len(set(tests)) != len(tests) or not isinstance(tasks, dict) or set(tasks) != set(tests):
        raise ValueError('V7 corpus must cover exactly the frozen test tasks')
    pools = root / 'pools'
    if pools.is_symlink() or not pools.is_dir() or {p.name for p in pools.iterdir()} != set(tests):
        raise ValueError('V7 corpus must hold exactly one pool per test task')
    sources_path = base / 'manifests' / SOURCES
    sources = load_sources(sources_path)
    if sha(sources_path) != index.get('source_manifest_sha256') or set(sources['counts_by_task']) != set(tests):
        raise ValueError('V7 corpus was built from another failure-source manifest')
    split_path = base / 'manifests/split.json'
    if split_path.exists() and sha(split_path) != index.get('split_sha256'):
        raise ValueError('V7 corpus belongs to a different split')
    by_task = {task: {} for task in tests}
    for row in sources['rows']:
        by_task[row['task_id']][row['trial_id']] = row
    official = _official_rows(base / 'reports/official_test_trials.csv')
    for task_id in tests:
        record = tasks[task_id]
        if not isinstance(record, dict) or record.get('pool_path') != f'prepared/v7_corpus/{root.name}/pools/{task_id}':
            raise ValueError(f'V7 corpus pool path differs: {task_id}')
        checked = verify_task_pool(pools / task_id, task_id, record)
        _check_records(task_id, record, checked, by_task[task_id], official)
    return index


# --------------------------------------------------------------- reporting

def decontam_report(build_root: Path) -> list[dict]:
    """Flagged trajectories for human review: metrics and line offsets, never reference text."""
    items = []
    for path in sorted((Path(build_root) / 'audit').glob('*.json')):
        audit = load(path)
        for record in audit['records']:
            check = record.get('decontamination')
            if check and check['flagged']:
                items.append({'task_id': audit['task_id'], 'attempt_id': record['attempt_id'],
                              'trial_id': record['trial_id'], 'model_name': record['model_name'],
                              'published': record['status'] != 'excluded', 'decision': record['decision'],
                              'reasons': check['reasons'], 'metrics': {k: check[k] for k in THRESHOLDS},
                              'review': check.get('review'), 'offsets': check['offsets'],
                              'source_path': record['source_path']})
    return items


def _build_root(base: Path, value: str) -> Path:
    return Path(base) / 'prepared/v7_corpus' / value if '/' not in value else _under(base, value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    def command(name, help_text):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument('--base', type=Path, default=BASE, help='benchmark directory holding the read-only inputs')
        return sub

    sub = command('sources', 'write manifests/v7_failure_sources.json')
    sub.add_argument('--manifests-root', type=Path, help='output directory (default <base>/manifests)')
    sub.add_argument('--replace', action='store_true', help='rewrite a manifest whose selection changed')
    sub = command('download', 'fetch missing failed test-task bodies from the official Hub')
    sub.add_argument('--manifests-root', type=Path, help='holds v7_failure_sources.json; receives the audit')
    sub.add_argument('--data-root', type=Path, help='default <base>/data/historical_trials')
    sub.add_argument('--workers', type=int, default=4)
    sub.add_argument('--limit', type=int, help='fetch at most this many missing sources')
    sub.add_argument('--trials', nargs='+', help='fetch only these failure-source trial IDs')
    sub = command('build', 'build per-task pools, audits and index.json')
    sub.add_argument('--out', type=Path, required=True, help='prepared/v7_corpus/<build_id>')
    sub.add_argument('--manifests-root', type=Path, help='holds v7_failure_sources.json and the download audit')
    sub.add_argument('--data-root', type=Path, help='default <base>/data/historical_trials')
    sub.add_argument('--decisions', type=Path, help='default prepared/v7_corpus/decontamination_decisions.json')
    sub.add_argument('--tasks', nargs='+', help='diagnostic subset; the index is then marked incomplete')
    sub.add_argument('--allow-unresolved', action='store_true', help='diagnostic only; unresolved sources are not listed')
    sub = command('decontam-report', 'print flagged trajectories of a build for human review')
    sub.add_argument('--build', required=True, help='build ID or path')
    sub = command('verify', 'verify a finished build against the split and failure sources')
    sub.add_argument('--build', required=True, help='build ID or path')
    args = parser.parse_args(argv)
    base = args.base.absolute()
    under = lambda value: _under(base, value) if value is not None else None
    if args.command == 'sources':
        manifest, written = write_sources(base, under(args.manifests_root), replace=args.replace)
        print(json.dumps({'written': written, 'row_count': manifest['row_count'],
                          'tasks_with_failures': sum(1 for n in manifest['counts_by_task'].values() if n),
                          'zero_failure_tasks': [t for t, n in manifest['counts_by_task'].items() if not n]}))
    elif args.command == 'download':
        audit = download_sources(base, manifests_root=under(args.manifests_root), data_root=under(args.data_root),
                                 workers=args.workers, limit=args.limit, trial_ids=args.trials)
        print(json.dumps({'counts': audit['counts'], 'complete': audit['complete'],
                          'actions': dict(Counter(t['action'] for t in audit['trials']))}))
        if not audit['complete']:
            print('Some failure sources remain unresolved; see v7_download_audit.json')
            return 1
    elif args.command == 'build':
        index = build_corpus(args.out, base=base, manifests_root=under(args.manifests_root),
                             data_root=under(args.data_root), decisions_path=under(args.decisions),
                             tasks=args.tasks, allow_unresolved=args.allow_unresolved)
        for task_id, record in index['tasks'].items():
            print(json.dumps({'task_id': task_id, 'trajectories': record['trajectory_count'],
                              'listed': record['source_record_count'], 'excluded': record['excluded_count']}))
        print(json.dumps({'build_id': index['build_id'], 'complete': index['complete'], 'tasks': len(index['tasks'])}))
    elif args.command == 'decontam-report':
        items = decontam_report(_build_root(base, args.build))
        for item in items:
            print(json.dumps(item, ensure_ascii=False))
        print(json.dumps({'flagged': len(items), 'published': sum(i['published'] for i in items),
                          'by_reason': dict(Counter(r for i in items for r in i['reasons']))}))
    else:
        index = verify_corpus(_build_root(base, args.build), load(base / 'manifests/split.json'))
        print(json.dumps({'build_id': index['build_id'], 'verified_tasks': len(index['tasks']),
                          'trajectories': sum(r['trajectory_count'] for r in index['tasks'].values())}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
