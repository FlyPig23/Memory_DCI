"""Audit resource absence; all solver tools remain native Codex tools."""
import json
from pathlib import Path
import subprocess
import tomllib

from experiment.src.experiment_protocol import atomic_json, sha256
from experiment.src.task_runtime import TaskRuntime, Mount, WORKSPACE, CODEX_HOME


def verify_mounts(actual, expected):
    binds = {m['Destination']: m for m in actual if m['Type'] == 'bind'}
    if len(binds) != len(expected) or set(binds) != {m.target for m in expected}:
        raise ValueError('Unexpected vanilla container bind mounts')
    for mount in expected:
        item = binds[mount.target]
        if Path(item['Source']).resolve() != mount.source.resolve() or item['RW'] != (not mount.readonly):
            raise ValueError('Vanilla mount differs from its allowlist')
    if any(m['Destination'] in {'/corpus', '/skills', '/opt/dci_tools.py', '/opt/dci-auth-token'} for m in actual):
        raise ValueError('Historical resources must not enter the vanilla environment')
    return [{'destination': m.target, 'source': str(m.source), 'readonly': m.readonly} for m in expected]


def equivalent_configuration(before, after):
    if sorted(before['Mounts'], key=lambda x:x['Destination']) != sorted(after['Mounts'], key=lambda x:x['Destination']):
        raise ValueError('Grading recreation changed mounts')
    a, b = dict(before['Config']), dict(after['Config'])
    a.pop('Hostname', None); b.pop('Hostname', None)
    for name in ('AttachStdout','AttachStderr'):
        if a.pop(name) is not False or b.pop(name) is not True:
            raise ValueError('Unexpected inert container attachment transition')
    for item in (a,b):
        env=item.get('Env',[])
        if len({x.split('=',1)[0] for x in env}) != len(env):
            raise ValueError('Duplicate environment names')
        item['Env']=sorted(env)
    if a != b:
        raise ValueError('Grading recreation changed configuration')
    a,b=dict(before['HostConfig']),dict(after['HostConfig'])
    for item in (a,b):
        item['Mounts']=sorted(item.get('Mounts',[]),key=lambda x:x['Target'])
        if item.get('OomKillDisable') is None:
            item['OomKillDisable']=False
    if a != b:
        raise ValueError('Grading recreation changed host configuration')


class VanillaRuntime(TaskRuntime):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.solver_args=None
        self.native_environment={'schema_version':1,'condition':'VANILLA_SOL',
            'run_id':self.run_dir.name,'solver':{},'grader':None}

    def docker(self,*args,**kwargs):
        if args and args[0]=='run':
            if self.solver_args is not None:
                raise ValueError('Only one solver container is permitted')
            self.solver_args=args
        return super().docker(*args,**kwargs)

    def inspect(self):
        return json.loads(self.docker('inspect',self.name).stdout)[0]

    def save_boundary(self):
        atomic_json(self.run_dir/'native_environment.json',self.native_environment)

    def expected_mounts(self):
        return [Mount(self.workspace,WORKSPACE,False),Mount(self.codex_home,CODEX_HOME,False),
            Mount(self.run_dir/'cache','/root/.cache',False),
            Mount(self.run_dir/'openclaw-home','/root/.openclaw',False),
            Mount(self.run_dir/'browser-pki','/root/.pki',False),*self.config.support_mounts]

    def prepare(self,config_text):
        if tomllib.loads(config_text).get('mcp_servers'):
            raise ValueError('Vanilla must not register MCP servers')
        super().prepare(config_text)
        info=self.inspect()
        mounts=verify_mounts(info['Mounts'],self.expected_mounts())
        installed=sorted(p.name for p in (self.codex_home/'skills').iterdir())
        if installed != sorted(Path(s).name for s in self.task.skills):
            raise ValueError('Only declared official task skills may be copied')
        absent=self.exec(['sh','-c','test ! -e /corpus && test ! -e /skills'],check=False)
        if absent.returncode:
            raise ValueError('Historical resource paths already exist')
        self.native_environment['solver']={'container_id':info['Id'],'resources_absent':True,
            'mounts_verified':True,'mounts':mounts,'official_task_skills':list(self.task.skills),
            'mcp_servers':[],'config_sha256':sha256(self.codex_home/'config.toml')}
        self.save_boundary()

    def freeze_agent(self):
        try:
            return super().freeze_agent()
        except subprocess.TimeoutExpired as error:
            # An observation timeout is not proof that Docker failed to stop.
            # Recheck this owned container; never restart or rerun the solver.
            command=list(error.cmd) if isinstance(error.cmd,(tuple,list)) else []
            if command[-4:] != ['stop','--time','5',self.name]:
                raise
            self._assert_owned()
            info=self.inspect()
            if info['State']['Running']:
                raise
            self.state='solver_stopped'; self._save_state()
            atomic_json(self.run_dir/'freeze_observation_recovery.json',{
                'status':'verified_stopped','container_id':info['Id'],
                'operation':'docker stop --time 5','observation_timeout_seconds':error.timeout,
                'solver_rerun':False})

    def grade(self,**kwargs):
        # Match V4's stopped-container recreation before its unchanged grader.
        if self.state!='solver_stopped' or not self.solver_args or self.native_environment['grader'] is not None:
            raise ValueError('Grading requires one completed solver freeze')
        self._assert_owned(); before=self.inspect()
        if before['State']['Running']:
            raise ValueError('Solver is still running')
        self.docker('rm',self.name); self.created=False
        args=['create',*[x for x in self.solver_args[1:] if x!='--detach']]
        self.docker(*args); self.created=True
        after=self.inspect()
        if after['State']['Running']:
            raise ValueError('Recreated grader container is already running')
        verify_mounts(after['Mounts'],self.expected_mounts())
        equivalent_configuration(before,after)
        self.native_environment['grader']={'resources_absent':True,'mounts_verified':True,
            'same_nonresource_configuration':True,'solver_stopped_before_recreation':True,
            'container_id_before':before['Id'],'container_id_after':after['Id'],'warmup_repeated':False}
        self.save_boundary()
        return super().grade(**kwargs)
