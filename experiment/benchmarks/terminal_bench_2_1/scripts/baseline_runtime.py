"""TB2.1 vanilla Codex adapter, independent of the immutable V5 run inputs.

Only the official task instruction and its official solving deadline form the
solver prompt. No historical resources, memory, custom MCP tools, or reviewer
enter the task. The caller selects the explicit official or unlimited verifier policy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import tarfile
import tempfile
import time
import tomllib

from harbor.agents.base import BaseAgent
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.constants import MAIN_SERVICE_NAME
from harbor.models.agent.context import AgentContext
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.trial.hooks import TrialEvent
from harbor.trial.trial import Trial

from experiment.src.codex_backend import CodexSettings, extract_usage, read_jsonl, render_config, select_main_session
from .harbor_runtime import (
    bind, clean_private_runtime, command, forbidden_evidence, hash_tree,
)
from .runtime import inspect_task

ROOT = Path(__file__).resolve().parents[4]
BASE = Path(__file__).resolve().parents[1]
MODEL, EFFORT, CLI_VERSION = 'gpt-5.6-sol', 'medium', '0.153.4'
# Infrastructure probes include Docker Compose startup/communication overhead.
# These limits never extend the official model solving deadline.
INFRA_TIMEOUT_SECONDS, SETUP_TIMEOUT_SECONDS = 120, 900
PYTHON = '/opt/baseline-python/bin/python3'
SUPERVISOR = '/opt/baseline/baseline_supervisor.py'
HOME = '/opt/baseline-home'
CONTROL = '/opt/baseline-control'
HISTORY_PATHS = ('/pool', '/memory', '/corpus', '/skills', '/opt/v5/tools.py', '/opt/v5/serve_memory.py')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_config():
    return render_config(CodexSettings(model=MODEL, reasoning_effort=EFFORT,
        cli_version=CLI_VERSION, binary='/opt/codex/codex', mcp_servers=()))


def validate_config(text, *, workspace=None):
    initial = make_config()
    transition = 'unchanged'
    append = ('\n[projects.' + json.dumps(workspace, ensure_ascii=False) + ']\ntrust_level = "trusted"\n') if workspace else None
    if text != initial:
        if not workspace or not workspace.startswith('/') or text != initial + append:
            raise ValueError('Baseline Codex configuration differs beyond the exact native workspace trust append')
        transition = 'exact_workspace_trust_append'
    config = tomllib.loads(text)
    if transition != 'unchanged':
        if config.pop('projects') != {workspace: {'trust_level': 'trusted'}}:
            raise ValueError('Unexpected native workspace trust configuration')
    if config != tomllib.loads(make_config()):
        raise ValueError('Baseline Codex configuration differs from the frozen empty-MCP configuration')
    if config.get('mcp_servers'):
        raise ValueError('Baseline may not register MCP servers')
    for name in ('memories', 'apps', 'plugins', 'remote_plugin', 'multi_agent'):
        if config['features'].get(name) is not False:
            raise ValueError('Baseline must disable ' + name)
    return {'config_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'initial_config_sha256': hashlib.sha256(initial.encode()).hexdigest(),
            'config_transition': transition,
            'accepted_native_workspace_trust': workspace if transition != 'unchanged' else None,
            'config_audit_scope': 'Observed initial/final configuration; not continuous monitoring',
            'mcp_servers': [],
            'global_memory': False, 'additional_skills': [], 'model': MODEL, 'reasoning_effort': EFFORT}


def solver_prompt(instruction, seconds):
    if not isinstance(seconds, (float, int)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Official solving deadline must be finite and positive')
    return f'The official solving deadline for this task is {seconds:g} seconds.\n\n' + instruction


def codex_command(workspace, output):
    return ['/opt/codex/codex', 'exec', '--json', '--skip-git-repo-check',
            '--dangerously-bypass-approvals-and-sandbox', '--cd', workspace,
            '--output-last-message', output, '-']


def support_mounts(home, control, python_root):
    runtime_bin = BASE / 'runtime/bin'
    return [bind(home, HOME, False), bind(control, CONTROL),
            bind(runtime_bin / 'codex', '/opt/codex/codex'),
            bind(runtime_bin / 'codex-code-mode-host', '/opt/codex/codex-code-mode-host'),
            bind(python_root, '/opt/baseline-python'),
            bind(Path(__file__).with_name('baseline_supervisor.py'), SUPERVISOR),
            bind(BASE / 'runtime/bin/rg', '/opt/baseline-bin/rg')]


def validate_mounts(actual, expected):
    """Exact actual Docker mount allowlist, including Harbor's three log mounts."""
    if len(actual) != len(expected) or len({m['Destination'] for m in actual}) != len(actual):
        raise ValueError('Unexpected or duplicate baseline mounts')
    expected_by_target = {m['target']: m for m in expected}
    if len(expected_by_target) != len(expected):
        raise ValueError('Duplicate allowlisted mount target')
    records = []
    for observed in actual:
        wanted = expected_by_target.get(observed['Destination'])
        if (wanted is None or observed['Type'] != 'bind'
                or Path(observed['Source']).resolve() != Path(wanted['source']).resolve()
                or observed['RW'] != (not wanted.get('read_only', False))):
            raise ValueError('Actual baseline Docker mount differs from allowlist')
        records.append({'target': wanted['target'], 'source': wanted['source'],
                        'read_only': wanted.get('read_only', False)})
    return sorted(records, key=lambda r: r['target'])


async def docker_metadata(environment):
    ids = await environment._run_docker_compose_command(['ps', '--all', '--quiet'], timeout_sec=INFRA_TIMEOUT_SECONDS)
    container_ids = ids.stdout.split()
    if not container_ids:
        raise RuntimeError('No task containers available for isolation audit')
    _, raw, _ = await command(['docker', 'inspect', *container_ids], timeout=INFRA_TIMEOUT_SECONDS)
    containers = json.loads(raw)
    return {c['Config']['Labels'].get('com.docker.compose.service'): c for c in containers}


async def audit_isolation(agent, environment, *, stage):
    contract = json.loads((agent.run_dir / 'runtime_contract.json').read_text())
    # During solving this fresh home is owned by the official task user, which
    # can be root. Read only the known configuration through the container.
    observed_config = await environment.exec(shlex.join(['cat', HOME + '/config.toml']), timeout_sec=INFRA_TIMEOUT_SECONDS, user='root')
    if observed_config.return_code:
        raise RuntimeError('Cannot read the isolated baseline configuration')
    config_audit = validate_config(observed_config.stdout, workspace=agent.workspace if stage == 'after_solver' else None)
    containers = await docker_metadata(environment)
    if set(containers) != {MAIN_SERVICE_NAME}:
        raise ValueError('Baseline supports only the frozen single-service TB2.1 tasks')
    main = containers[MAIN_SERVICE_NAME]
    mounts = validate_mounts(main['Mounts'], contract['expected_mounts'])
    probe = 'import json,os; print(json.dumps([p for p in ' + repr(HISTORY_PATHS) + ' if os.path.lexists(p)]))'
    result = await environment.exec(shlex.join([PYTHON, '-c', probe]), timeout_sec=INFRA_TIMEOUT_SECONDS)
    if result.return_code or json.loads(result.stdout):
        raise ValueError('Historical-resource paths present or resource-absence probe failed')
    record = {'valid': True, 'stage': stage, 'container_id': main['Id'], 'image_id': main['Image'],
              'resources_absent': True, 'history_paths_checked': list(HISTORY_PATHS),
              'mounts_verified': True, 'mounts': mounts, 'condition': 'vanilla',
              'reviewer_enabled': False, **config_audit,
              'docker_resources': {key: main['HostConfig'].get(key)
                  for key in ('NanoCpus', 'Memory', 'MemorySwap', 'CpuQuota', 'CpuPeriod')},
              'official_working_dir': main['Config'].get('WorkingDir'),
              'official_user': main['Config'].get('User'),
              'compose_project': main['Config']['Labels'].get('com.docker.compose.project'),
              'compose_service': main['Config']['Labels'].get('com.docker.compose.service'),
              'container_ids': [main['Id']],
              'network_names': sorted(main.get('NetworkSettings', {}).get('Networks', {}))}
    write_json(agent.run_dir / ('isolation_' + stage + '.json'), record)
    if stage == 'before_solver':
        write_json(agent.run_dir / 'isolation.json', record)
    return record


def capture_session(home, logs):
    """Persist the native ledger; inspect only configuration and token metadata."""
    logs = Path(logs)
    logs.mkdir(parents=True, exist_ok=True)
    session = select_main_session(list((Path(home) / 'sessions').rglob('*.jsonl')))
    if session:
        shutil.copyfile(session, logs / 'codex.session.jsonl')
    source = logs / 'codex.session.jsonl' if session else logs / 'codex.events.jsonl'
    usage = extract_usage(source, cumulative=bool(session))
    models, efforts = set(), set()
    for event in read_jsonl(source):
        if event.get('type') == 'turn_context':
            value = event.get('payload', {})
            models.add(value.get('model'))
            efforts.add(value.get('effort', value.get('reasoning_effort')))
    model_audit = {'models': sorted(models, key=str), 'efforts': sorted(efforts, key=str),
                   'model_effort_verified': models == {MODEL} and efforts == {EFFORT},
                   'tools': [], 'tools_scope': 'Custom MCP tools; native Codex tools remain enabled',
                   'mcp_servers': [], 'initial_distilled_skills': 0, 'global_memory': False,
                   'reasoning_content_inspected': False, 'source': source.name,
                   'native_session_available': bool(session)}
    write_json(logs / 'usage.json', usage)
    write_json(logs / 'model_audit.json', model_audit)
    return usage, model_audit


class TerminalBenchBaselineAgent(BaseAgent):
    def __init__(self, *args, run_dir, task_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_dir = Path(run_dir)
        self.task_id = task_id
        self.private_home = self.run_dir / 'private/solve-home'
        self.workspace = None

    @staticmethod
    def name():
        return 'terminal-bench-vanilla-codex'

    def version(self):
        return CLI_VERSION

    async def setup(self, environment):
        if self.mcp_servers or self.skills_dir:
            raise ValueError('The frozen baseline tasks may not inject MCP or skills')
        containers = await docker_metadata(environment)
        main = containers[MAIN_SERVICE_NAME]
        write_json(self.run_dir / 'environment_identity.json', {
            'container_ids': [c['Id'] for c in containers.values()],
            'compose_project': main['Config']['Labels'].get('com.docker.compose.project'),
            'network_names': sorted(main.get('NetworkSettings', {}).get('Networks', {})),
            'image_id': main['Image']})
        where = await environment.exec('pwd', timeout_sec=INFRA_TIMEOUT_SECONDS)
        self.workspace = (where.stdout or '').strip()
        if where.return_code or not self.workspace.startswith('/') or '\n' in self.workspace:
            raise ValueError('Task lacks a unique absolute workspace')
        if {p.name for p in self.private_home.iterdir()} != {'auth.json'}:
            raise ValueError('Baseline Codex home must initially contain only isolated authentication')
        (self.private_home / 'config.toml').write_text(make_config())
        identity = await environment.exec('id -u; id -g', timeout_sec=INFRA_TIMEOUT_SECONDS)
        uid, gid = [int(v) for v in identity.stdout.splitlines()]
        prepared = await environment.exec(
            f'chown -R {uid}:{gid} {HOME} && chmod 700 {HOME} && chmod 600 {HOME}/auth.json',
            timeout_sec=INFRA_TIMEOUT_SECONDS, user='root')
        if prepared.return_code:
            raise RuntimeError('Cannot prepare private baseline Codex home')
        version = await environment.exec('/opt/codex/codex --version', timeout_sec=INFRA_TIMEOUT_SECONDS, env={'CODEX_HOME': HOME})
        if version.return_code or version.stdout.strip() != f'codex-cli {CLI_VERSION}':
            raise ValueError('Pinned Codex CLI version mismatch')
        probe = await environment.exec(shlex.join([PYTHON, '-c', 'import sys; assert sys.version_info[:2] == (3,12)']), timeout_sec=INFRA_TIMEOUT_SECONDS)
        if probe.return_code:
            raise RuntimeError('Pinned standalone Python is unavailable')
        await audit_isolation(self, environment, stage='before_solver')
        write_json(self.logs_dir / 'setup.json', {'workspace': self.workspace, 'cli_version': CLI_VERSION,
                   'model': MODEL, 'reasoning_effort': EFFORT, 'fresh_codex_home': True,
                   'task_environment_python_path_unchanged': True, 'custom_tools': []})

    async def run(self, instruction, environment, context: AgentContext):
        contract = json.loads((self.run_dir / 'runtime_contract.json').read_text())
        deadline = contract['solver_timeout_seconds']
        prompt = solver_prompt(instruction, deadline)
        (self.run_dir / 'control/task.txt').write_text(instruction)
        (self.run_dir / 'control/solver_prompt.txt').write_text(prompt)
        write_json(self.run_dir / 'prompt_audit.json', {
            'official_instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(),
            'solver_prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'only_official_instruction_and_deadline': True,
            'additional_resources': [], 'custom_tools': [], 'reviewer': False})
        log_dir = str(self.environment_logs_dir)
        argv = [PYTHON, SUPERVISOR, 'supervise', '--prompt', CONTROL + '/solver_prompt.txt',
                '--events', log_dir + '/codex.events.jsonl', '--stderr', log_dir + '/codex.stderr.log',
                '--timeout', str(deadline), '--', *codex_command(self.workspace, log_dir + '/final.txt')]
        started, status, code = time.monotonic(), 'cancelled', None
        try:
            result = await environment.exec('export PATH="$PATH:/opt/baseline-bin"; ' + shlex.join(argv),
                cwd=self.workspace, env={'CODEX_HOME': HOME}, timeout_sec=None)
            code = result.return_code
            status = 'completed' if code == 0 else 'timeout' if code == 124 else 'execution_error'
            if code == 124:
                raise asyncio.TimeoutError('Official solver deadline exceeded')
            if code:
                raise NonZeroAgentExitCodeError(f'Codex exited {code}')
        finally:
            write_json(self.logs_dir / 'execution.json', {'status': status, 'exit_code': code,
                'elapsed_seconds': time.monotonic() - started, 'model': MODEL, 'reasoning_effort': EFFORT})
            context.metadata = {'model': MODEL, 'reasoning_effort': EFFORT, 'status': status}

    def populate_context_post_run(self, context):
        file = self.logs_dir / 'usage.json'
        if file.exists():
            usage = json.loads(file.read_text())
            context.n_input_tokens = usage.get('input_tokens')
            context.n_output_tokens = usage.get('output_tokens')
            context.n_cache_tokens = usage.get('cached_input_tokens')


def private_evidence(path):
    value = PurePosixPath(path)
    extra = (HOME, CONTROL, '/opt/baseline', '/opt/baseline-python', '/opt/baseline-bin')
    return forbidden_evidence(path) or any(value == PurePosixPath(p) or PurePosixPath(p) in value.parents for p in extra)


async def snapshot_path(container, source, destination, scratch, *, single_file=False):
    """Use /home-backed scratch and exclude credentials/runtime directories."""
    # Missing deliverables are valid observations of a solver's output. They
    # must not be confused with infrastructure failure or filtered from scores.
    probe = '''import json,os,stat,sys
try:
    metadata=os.lstat(sys.argv[1])
except (FileNotFoundError, NotADirectoryError):
    print(json.dumps({"exists":False}))
else:
    print(json.dumps({"exists":True,"symlink":stat.S_ISLNK(metadata.st_mode),"resolved":os.path.realpath(sys.argv[1])}))
'''
    code, observed, _ = await command(['docker', 'exec', '--user', '0', container,
                                      PYTHON, '-c', probe, source], timeout=INFRA_TIMEOUT_SECONDS, check=False)
    if code:
        return {'path': source, 'status': 'failed', 'failure_stage': 'existence_probe', 'exit_code': code}
    try:
        existence = json.loads(observed)
    except (TypeError, ValueError):
        return {'path': source, 'status': 'failed', 'failure_stage': 'invalid_existence_probe'}
    if existence.get('exists') is False:
        return {'path': source, 'status': 'absent_verified', 'probe': 'container_lstat'}
    if existence.get('exists') is not True:
        return {'path': source, 'status': 'failed', 'failure_stage': 'invalid_existence_probe'}
    # Never follow an explicitly requested symlink, including a path whose
    # parent resolves into private runtime material.
    if existence.get('symlink') or private_evidence(existence.get('resolved', source)):
        return {'path': source, 'status': 'excluded_symlink_or_private_target', 'probe': 'container_lstat'}
    with tempfile.TemporaryDirectory(prefix='capture-', dir=scratch) as temporary:
        archive = Path(temporary) / 'snapshot.tar'
        code, _, _ = await command(['docker', 'cp', f'{container}:{source}', '-'], timeout=600,
                                   check=False, stdout_path=archive)
        if code:
            return {'path': source, 'status': 'failed', 'failure_stage': 'docker_copy', 'exit_code': code}
        count = 0
        with tarfile.open(archive) as stream:
            for member in stream:
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Unsafe archive path')
                parts = relative.parts[1:]
                if (single_file and parts) or not member.isfile() or private_evidence(str(PurePosixPath(source).joinpath(*parts))):
                    continue
                target = Path(destination).joinpath(*parts) if parts else Path(destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                count += 1
        return {'path': source, 'status': 'copied', 'files': count}


async def snapshot_public(trial):
    agent, environment = trial.agent, trial.agent_environment
    evidence = agent.run_dir / 'evidence'
    evidence.mkdir(exist_ok=True)
    scratch = agent.run_dir / 'snapshot-tmp'
    scratch.mkdir(exist_ok=True)
    containers = await docker_metadata(environment)
    main = containers[MAIN_SERVICE_NAME]
    workspace = agent.workspace
    records, started = [], time.time()
    if workspace not in ('/', '/root', '/home', '/tmp', '/opt', '/usr', '/var'):
        records.append(await snapshot_path(main['Id'], workspace, evidence / 'workspace', scratch))
    else:
        _, diff, _ = await command(['docker', 'diff', main['Id']], timeout=INFRA_TIMEOUT_SECONDS)
        candidates = [line.partition(' ')[2] for line in diff.splitlines()
                      if line[:1] in ('A', 'C') and not private_evidence(line.partition(' ')[2])
                      and not line.partition(' ')[2].startswith(('/usr/', '/var/', '/etc/', '/bin/', '/lib/', '/sbin/'))]
        probe = 'import json,os,stat,sys; print(json.dumps([p for p in json.loads(sys.argv[1]) if os.path.lexists(p) and stat.S_ISREG(os.lstat(p).st_mode)]))'
        found = await environment.exec(shlex.join([PYTHON, '-c', probe, json.dumps(candidates)]), timeout_sec=INFRA_TIMEOUT_SECONDS, user='root')
        if found.return_code:
            raise RuntimeError('Cannot identify changed public task files')
        for source in json.loads(found.stdout):
            records.append(await snapshot_path(main['Id'], source, evidence / 'workspace' / source.lstrip('/'), scratch, single_file=True))
    for item in trial.task.config.artifacts:
        source = item if isinstance(item, str) else item.source
        service = MAIN_SERVICE_NAME if isinstance(item, str) else item.service or MAIN_SERVICE_NAME
        path = source if source.startswith('/') else str(PurePosixPath(workspace) / source)
        if not private_evidence(path) and service in containers:
            records.append(await snapshot_path(containers[service]['Id'], path,
                evidence / 'artifacts' / service / path.lstrip('/'), scratch))
    records.append(await snapshot_path(main['Id'], '/logs/artifacts', evidence / 'published', scratch))
    shutil.copyfile(agent.run_dir / 'control/task.txt', evidence / 'task.txt')
    manifest = {'workspace': workspace, 'paths': records, 'taken_before_hidden_grading': True,
                'symlinks_followed': False, 'services_paused': [], 'task_services_kept_running': True,
                'snapshot_consistency': 'Individual copies while task services remain live; not globally atomic',
                'capture_started_at': started, 'capture_finished_at': time.time(), 'files_sha256': hash_tree(evidence)}
    write_json(evidence / 'snapshot_manifest.json', manifest)
    return manifest


async def cleanup_successful_trial(trial_dir, output):
    """Remove only this stopped, fully audited trial after its result is durable."""
    isolation = output.get('isolation') or {}
    frozen = output.get('freeze') or {}
    model = output.get('model_audit') or {}
    usage = output.get('usage') or {}
    reward = (((output.get('harbor') or {}).get('verifier_result') or {}).get('rewards') or {}).get('reward')
    eligible = (isinstance(reward, (int, float)) and not isinstance(reward, bool) and math.isfinite(reward)
                and isolation.get('valid') is True and frozen.get('solver_stopped') is True
                and frozen.get('snapshot_valid') is True and model.get('model_effort_verified') is True
                and usage.get('usage_available') is True
                and all(isinstance(usage.get(k), int) for k in ('input_tokens', 'output_tokens')))
    record = {'eligible': eligible, 'result_written_before_cleanup': True,
              'baseline_result_sha256': sha(Path(trial_dir) / 'baseline_result.json'),
              'container_ids': isolation.get('container_ids', []),
              'compose_project': isolation.get('compose_project'), 'deleted_container_ids': [],
              'deleted_network_names': [], 'status': 'retained_for_recovery'}
    if eligible:
        try:
            ids = isolation['container_ids']
            if len(ids) != 1 or not isolation.get('compose_project'):
                raise ValueError('Owned trial container identity is incomplete')
            _, raw, _ = await command(['docker', 'inspect', *ids])
            current = json.loads(raw)
            if len(current) != 1 or any(c['Id'] not in ids or c['State']['Running']
                    or c['Config']['Labels'].get('com.docker.compose.project') != isolation['compose_project']
                    or c['Config']['Labels'].get('com.docker.compose.service') != MAIN_SERVICE_NAME for c in current):
                raise ValueError('Trial ownership or stopped-state verification failed')
            await command(['docker', 'rm', '--volumes', *ids], timeout=120)
            record['deleted_container_ids'] = ids
            for name in isolation.get('network_names', []):
                _, raw, _ = await command(['docker', 'network', 'inspect', name])
                info = json.loads(raw)[0]
                if ((info.get('Labels') or {}).get('com.docker.compose.project') == isolation['compose_project']
                        and not info.get('Containers')):
                    await command(['docker', 'network', 'rm', name])
                    record['deleted_network_names'].append(name)
            record['status'] = 'audited_trial_removed'
        except Exception as exc:
            record.update(status='cleanup_incomplete', error_type=type(exc).__name__, error=str(exc))
    write_json(Path(trial_dir) / 'environment_cleanup.json', record)
    return record


async def run_trial(task_path, trial_dir, task_id, *, python_root, auth_file, environment_config, cleanup_image, verifier_policy="official"):
    task_path, trial_dir, python_root = map(lambda p: Path(p).resolve(), (task_path, trial_dir, python_root))
    if trial_dir.exists():
        raise FileExistsError('Refuse to overwrite or rerun an existing baseline trial')
    if verifier_policy not in {'official', 'unlimited'}:
        raise ValueError('Unknown verifier policy')
    spec = inspect_task(task_path)
    verifier_timeout = None if verifier_policy == 'unlimited' else spec['verifier']['timeout_sec']
    if (task_path / 'environment/docker-compose.yaml').exists() or spec['environment'].get('gpus') or spec.get('verifier', {}).get('environment'):
        raise ValueError('Baseline supports the frozen single-service CPU tasks with a shared verifier')
    config = dict(environment_config or {})
    if set(config) - {'extra_docker_compose', 'type'}:
        raise ValueError('Baseline rejects extra environment overrides')
    if config.get('type', 'docker') != 'docker':
        raise ValueError('Baseline requires the official Docker environment')
    allowed_transport = (BASE / 'prepared/transport/docker-compose-ca.yaml').resolve()
    if [Path(x).resolve() for x in config.get('extra_docker_compose', [])] != [allowed_transport]:
        raise ValueError('Baseline requires the pinned transport-only Compose overlay')
    trial_dir.mkdir(parents=True)
    (trial_dir / 'control').mkdir()
    home = trial_dir / 'private/solve-home'
    home.mkdir(parents=True, mode=0o700)
    shutil.copyfile(auth_file, home / 'auth.json')
    os.chmod(home / 'auth.json', 0o600)
    contract = {'condition': 'vanilla', 'task_id': task_id, 'model': MODEL, 'reasoning_effort': EFFORT,
        'cli_version': CLI_VERSION, 'harbor_version': '0.23.0',
        'infrastructure_probe_timeout_seconds': INFRA_TIMEOUT_SECONDS,
        'agent_setup_timeout_seconds': SETUP_TIMEOUT_SECONDS,
        'solver_timeout_seconds': spec['agent']['timeout_sec'],
        'official_verifier_timeout_seconds': spec['verifier']['timeout_sec'], 'verifier_timeout_seconds': verifier_timeout,
        'verifier_timeout_policy': verifier_policy,
        'review_timeout_seconds': None, 'reviewer_enabled': False, 'tool_names': [], 'mcp_servers': [],
        'historical_resources': [], 'global_memory': False, 'cross_task_memory': False, 'initial_skills': 0,
        'solver_rollouts': 1, 'native_codex_tools_preserved': True, 'task_services_paused': False,
        'snapshot_atomic_across_services': False,
        'environment_cleanup_policy': 'Retain failed audits or unscored trials; remove only stopped scored trials after complete audits and durable result'}
    config.update(type='docker', delete=False, kwargs={'keep_containers': True},
                  mounts=support_mounts(home, trial_dir / 'control', python_root))
    trial, frozen = None, None
    try:
        trial = await Trial.create(TrialConfig(
            task=TaskConfig(path=task_path), trial_name=trial_dir.name, trials_dir=trial_dir.parent,
            agent=AgentConfig(import_path=__name__ + ':TerminalBenchBaselineAgent', model_name=MODEL,
                              override_setup_timeout_sec=SETUP_TIMEOUT_SECONDS,
                              kwargs={'run_dir': str(trial_dir), 'task_id': task_id}),
            environment=EnvironmentConfig(**config), verifier=VerifierConfig()))
        if trial._agent_timeout_sec != spec['agent']['timeout_sec']:
            raise ValueError('Effective solver deadline differs from official task')
        if trial._verifier_timeout_sec != spec['verifier']['timeout_sec']:
            raise ValueError('Original verifier deadline differs from official task')
        trial._verifier_timeout_sec = verifier_timeout
        if trial._verifier_timeout_sec != verifier_timeout:
            raise ValueError('Requested verifier deadline did not take effect')
        expected = list(trial._agent_env_mounts)
        expected.append(bind(BASE / 'prepared/transport/ca-certificates.crt', '/opt/v5-ca/ca-certificates.crt'))
        contract.update(expected_mounts=expected, effective_solver_timeout_verified=True,
                        effective_verifier_timeout_verified=True)
        write_json(trial_dir / 'runtime_contract.json', contract)

        async def agent_end(_event):
            nonlocal frozen
            frozen = {'taken_before_hidden_grading': True, 'solver_stopped': False,
                      'snapshot_valid': False, 'task_services_kept_running': True,
                      'snapshot_atomic_across_services': False}
            try:
                stopped = await trial.agent_environment.exec(shlex.join([PYTHON, SUPERVISOR, 'freeze']), timeout_sec=INFRA_TIMEOUT_SECONDS, user='root')
                if stopped.return_code:
                    raise RuntimeError('Cannot stop Codex controls before hidden grading')
                frozen.update(solver_stopped=True, stopped_solver_processes=json.loads(stopped.stdout))
                write_json(trial_dir / 'freeze.json', frozen)
                owned = await trial.agent_environment.exec(f'chown -R {os.getuid()}:{os.getgid()} {HOME}', timeout_sec=INFRA_TIMEOUT_SECONDS, user='root')
                if owned.return_code:
                    raise RuntimeError('Cannot read task-private native ledger')
                # Model and token audits survive a later artifact-copy failure.
                capture_session(home, trial.agent.logs_dir)
                if trial.result.agent_result is not None:
                    trial.agent.populate_context_post_run(trial.result.agent_result)
                try:
                    audit = await audit_isolation(trial.agent, trial.agent_environment, stage='after_solver')
                    write_json(trial_dir / 'isolation.json', audit)
                except Exception as exc:
                    write_json(trial_dir / 'isolation.json', {'valid': False, 'stage': 'after_solver',
                               'error_type': type(exc).__name__, 'reason': str(exc)})
                try:
                    manifest = await snapshot_public(trial)
                    frozen.update(snapshot_valid=all(r['status'] in {'copied', 'absent_verified'} for r in manifest['paths']),
                                  evidence_manifest='evidence/snapshot_manifest.json')
                except Exception as exc:
                    frozen.update(snapshot_error_type=type(exc).__name__, snapshot_error=str(exc))
                write_json(trial_dir / 'freeze.json', frozen)
            except Exception as exc:
                frozen.update(boundary_error_type=type(exc).__name__, boundary_error=str(exc))
                write_json(trial_dir / 'freeze.json', frozen)
                raise

        async def verification_start(_event):
            if trial._verifier_timeout_sec != verifier_timeout or not frozen or not frozen.get('solver_stopped'):
                raise RuntimeError('Verifier boundary or configured deadline is invalid')
            write_json(trial_dir / 'verifier_policy_audit.json', {
                'official_timeout_seconds': spec['verifier']['timeout_sec'], 'effective_timeout_seconds': verifier_timeout, 'policy': verifier_policy,
                'solver_stopped': True, 'checked_at': time.time(), 'official_tests_modified': False})

        trial.add_hook(TrialEvent.AGENT_END, agent_end)
        trial.add_hook(TrialEvent.VERIFICATION_START, verification_start)
        result = await trial.run()
        def saved(path):
            file = trial_dir / path
            return json.loads(file.read_text()) if file.exists() else None
        output = {'task_id': task_id, 'condition': 'vanilla', 'harbor': result.model_dump(mode='json'),
                  'freeze': saved('freeze.json'), 'isolation': saved('isolation.json'),
                  'model_audit': saved('agent/model_audit.json'), 'usage': saved('agent/usage.json'),
                  'environment_identity': saved('environment_identity.json')}
        write_json(trial_dir / 'baseline_result.json', output)
        await cleanup_successful_trial(trial_dir, output)
        return output
    finally:
        await clean_private_runtime(trial_dir, cleanup_image)
