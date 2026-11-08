"""Real inert four-member owner admission; never proposes or registers a run."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import controller as ctrl
import owner_operation as op


class BundleOwnerTests(unittest.TestCase):
    def test_actual_frozen_preparation_and_final_binding(self):
        packet=Path(os.environ['BUNDLE_PACKET']).resolve()
        inputs=ctrl.read(ctrl.pin(packet/'FROZEN-OWNER-BUILD-INPUTS.json'))
        reference=op._json(packet/'reference-preparation/author/PACKAGE-IDENTITIES.json')
        previous=op._json(op.R2/'hyperloom-sglang-implementation-20261102-v1/optimization-continuation-v1/verify-serving-registration-v2/PLAN.json')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            for name in ('requests','replies'): (root/name).mkdir()
            runtime=ctrl.pin(packet/'RUNTIME-IDENTITY.json')
            sources={name:ctrl.pin(ctrl.INFRA/name)['sha256'] for name in ctrl.BUNDLE_SOURCES}
            started=int(time.time()*1000)
            bound=dict(previous['binding'],controller_sha256=ctrl.source_closure(sources),
                       runtime_configuration=runtime,runtime_sha256=runtime['sha256'],
                       serving_source_family='four_member',four_member_contract=ctrl.pin(packet/'FOUR-MEMBER-CONTRACT.json'),
                       verify_serving_sha256=ctrl.pin(HERE/'verify_serving.py')['sha256'],
                       observed_power_cap_microwatts=235000000,campaign_deadline_unix_ms=started+7660000,
                       requests_root=str(root/'requests'),replies_root=str(root/'replies'))
            _,request=op.exchange.submit(root/'requests',campaign_sha256=bound['campaign_sha256'],
                selection_sha256=bound['selection_sha256'],kind='serving',config=previous['benchmark_config'],
                sources=bound['sources'],caller_seconds=7160,campaign_deadline_unix_ms=bound['campaign_deadline_unix_ms'],
                now=started/1000)
            taken=ctrl.host.dispatch('take',bound,job_id=request['id'])
            ctrl.host.retain_snapshot(taken,root/'snapshot',bound)
            result=op.prepare(root/'snapshot',bound,destination=root/'preparation',
                owner_inputs=ctrl.pin(packet/'FROZEN-OWNER-BUILD-INPUTS.json'),
                support_inputs=ctrl.pin(packet/'SUPPORT-BUILD-TEMPLATE.json'))
            self.assertEqual(result['status'],'PREPARED_NOT_REGISTERED_OR_RUN',result)
            self.assertEqual(result['limits']['requests'],66)
            self.assertEqual(result['limits']['natural_grades'],60)
            self.assertFalse(result['registered'] or result['gpu_work'] or result['native_action'])
            self.assertEqual({k:result['package'][k] for k in op.PIN_FIELDS},inputs['frozen_packet']['pins'])
            self.assertEqual({k:result['package'][k] for k in op.PIN_FIELDS},{k:reference[k] for k in op.PIN_FIELDS})
            binding=ctrl.read(result['source_binding'])
            self.assertEqual(binding['four_member_contract'],bound['four_member_contract'])
            self.assertEqual(binding['evaluable_surface'],'four_member')
            self.assertFalse(binding['model_precision_context_flags_power_overlays_unchanged'])
            self.assertFalse((root/'preparation/REGISTRATION.json').exists())


if __name__=='__main__':unittest.main(verbosity=2)
