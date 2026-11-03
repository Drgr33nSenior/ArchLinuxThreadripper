"""Profile selection cannot silently change the default component command."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'lib/workstation'))
from component_profiling import configure


class ProfileTests(unittest.TestCase):
    payload = ['/usr/bin/python3', '-I', '-B', '/mnt/review/gpu_smoke.py']

    def test_default_is_unchanged_and_returns_an_independent_command(self):
        plan = configure(self.payload)
        self.assertEqual(plan, {'argv': self.payload, 'mounts': [], 'environment': {}})
        self.assertIsNot(plan['argv'], self.payload)
        with self.assertRaises(ValueError): configure(self.payload, twin_sitepackages='/tmp/twin')
        with self.assertRaises(ValueError): configure('/usr/bin/python3')

    def test_rocprof_command_keeps_payload_and_uses_json_without_counters_or_child_sync(self):
        plan = configure(self.payload, route='rocprofv3', python='/usr/bin/python3',
                         rocm_root='/mnt/sdk', trace_directory='/mnt/output/trace')
        self.assertEqual(plan['argv'][-len(self.payload):], self.payload)
        self.assertEqual(plan['argv'][plan['argv'].index('--output-format') + 1], 'json')
        self.assertEqual(plan['argv'][plan['argv'].index('--process-sync') + 1], 'false')
        self.assertEqual(plan['mounts'], [])
        self.assertNotIn('--memory-copy-trace', plan['argv'])

    def test_kineto_uses_a_read_only_torch_overlay_and_no_tracer_preload(self):
        plan = configure(self.payload, route='kineto', twin_sitepackages='/home/developer/twin/sitepackages')
        self.assertEqual(plan['argv'], self.payload)
        self.assertEqual(plan['mounts'], ['--ro-bind', '/home/developer/twin/sitepackages/torch',
                                         '/usr/local/lib/python3.14/dist-packages/torch'])
        self.assertNotIn('LD_PRELOAD', plan['environment'])

    def test_incompatible_or_noncanonical_configuration_refuses(self):
        for kwargs in ({'route': 'automatic'}, {'route': 'rocprofv3'}, {'route': 'kineto'},
                       {'route': 'kineto', 'twin_sitepackages': 'relative'},
                       {'route': 'kineto', 'twin_sitepackages': '/tmp/../torch'},
                       {'route': 'kineto', 'twin_sitepackages': '/usr/local/lib/python3.14/dist-packages'},
                       {'route': 'rocprofv3', 'python': '/usr/bin/python3', 'rocm_root': '/mnt/sdk',
                        'trace_directory': '/mnt/output', 'twin_sitepackages': '/tmp/twin'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): configure(self.payload, **kwargs)


if __name__ == '__main__':
    unittest.main()
