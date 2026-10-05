"""Native trust persistence is permitted; changes to experiment controls are not."""
import unittest

from experiment.benchmarks.terminal_bench_2_1.scripts.baseline_runtime import make_config, validate_config


class ConfigTransitionTests(unittest.TestCase):
    def test_unchanged_and_exact_official_workspace_trust(self):
        original = make_config()
        self.assertEqual(validate_config(original)['config_transition'], 'unchanged')
        final = original + '\n[projects."/app"]\ntrust_level = "trusted"\n'
        audit = validate_config(final, workspace='/app')
        self.assertEqual(audit['config_transition'], 'exact_workspace_trust_append')
        self.assertEqual(audit['model'], 'gpt-5.6-sol')
        self.assertEqual(audit['reasoning_effort'], 'medium')

    def test_trust_is_not_accepted_before_solver_or_for_another_workspace(self):
        final = make_config() + '\n[projects."/app"]\ntrust_level = "trusted"\n'
        for workspace in (None, '/root'):
            with self.subTest(workspace=workspace), self.assertRaises(ValueError):
                validate_config(final, workspace=workspace)

    def test_model_memory_extra_trust_or_mcp_changes_still_fail(self):
        original = make_config()
        append = '\n[projects."/app"]\ntrust_level = "trusted"\n'
        altered = [original.replace('"medium"', '"high"') + append,
                   original.replace('memories = false', 'memories = true') + append,
                   original + append + '\n[projects."/other"]\ntrust_level = "trusted"\n',
                   original + append + '\n[mcp_servers.extra]\ncommand = "extra"\n']
        for value in altered:
            with self.subTest(config=value), self.assertRaises(ValueError):
                validate_config(value, workspace='/app')


if __name__ == '__main__':
    unittest.main()
