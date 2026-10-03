"""No-model regression checks for the new isolation and stop-observation audit."""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from experiment.src.task_runtime import TaskRuntime,Mount
from experiment.variants.vanilla_sol.runtime import VanillaRuntime,verify_mounts

class RuntimeTests(unittest.TestCase):
    def test_extra_corpus_mount_is_rejected(self):
        expected=[Mount(Path('/tmp/vanilla-workspace'),'/tmp_workspace',False)]
        good=[{'Type':'bind','Destination':'/tmp_workspace','Source':'/tmp/vanilla-workspace','RW':True}]
        verify_mounts(good,expected)
        with self.assertRaises(ValueError):
            verify_mounts(good+[{'Type':'bind','Destination':'/corpus','Source':'/tmp/corpus','RW':False}],expected)

    def test_stop_observation_timeout_only_recovers_a_verified_stopped_container(self):
        rt=object.__new__(VanillaRuntime); rt.name='hangxiao-skill-dci-test'
        rt.run_dir=Path('/tmp/vanilla-audit-test'); rt._assert_owned=Mock(); rt._save_state=Mock()
        rt.inspect=Mock(return_value={'Id':'owned','State':{'Running':False}})
        timeout=subprocess.TimeoutExpired(['docker','stop','--time','5',rt.name],30)
        with patch.object(TaskRuntime,'freeze_agent',side_effect=timeout), patch('experiment.variants.vanilla_sol.runtime.atomic_json') as save:
            rt.freeze_agent()
            self.assertEqual(rt.state,'solver_stopped'); save.assert_called_once()
        rt.inspect.return_value['State']['Running']=True
        with patch.object(TaskRuntime,'freeze_agent',side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired): rt.freeze_agent()

    def test_unrelated_timeout_is_not_swallowed(self):
        rt=object.__new__(VanillaRuntime); rt.name='hangxiao-skill-dci-test'
        with patch.object(TaskRuntime,'freeze_agent',side_effect=subprocess.TimeoutExpired(['docker','inspect',rt.name],30)):
            with self.assertRaises(subprocess.TimeoutExpired): rt.freeze_agent()

if __name__=='__main__': unittest.main()
