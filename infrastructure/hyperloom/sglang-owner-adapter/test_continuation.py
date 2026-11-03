"""Read-only retained-result checks and CPU-only continuation ordering."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import continuation
import optimizer_runtime as runtime
import owner_operation as op
import hyperloom_exchange as exchange

I=op.R2/'hyperloom-sglang-implementation-20261102-v1'
PREVIOUS=I/'campaign-v3'
REFERENCE=dict(root=str(PREVIOUS),manifest_sha256='7b8f26fe2c7b9f62f1d52bb12dce831938f31fe747d6126f6bfa9460eee689ae',
               workload_preflight=op._pin(I/'campaign-workload-preflight-v1/RESULT.json'))


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.bound=op._json(PREVIOUS/'BINDING.json')
        self.bound['workload_sha256']='0b01b0312b496447be762c453b07efe80f90fdd273bd9badaf8c42c4b5d9519d'
        self.now=1793616546249+600000

    def test_actual_released_component_is_carried_with_its_original_identity(self):
        result=continuation.validate(REFERENCE,self.bound,self.now)
        self.assertEqual(result['result']['index'],197)
        self.assertEqual(result['deadline_unix_ms'],1793630946249)
        self.assertTrue(result['report']['correct_both_cards'])
        self.assertFalse(result['report']['eligible_for_serving'])
        self.assertEqual(result['request']['id'],'0f3248435f37748bb2a9c297084a8722')

    def test_foreign_identity_expired_clock_and_wrong_workload_refuse(self):
        for key in ('campaign_sha256','controller_sha256','selection_sha256','workload_sha256'):
            changed=dict(self.bound,**{key:'0'*64})
            with self.subTest(key=key),self.assertRaises(ValueError):
                continuation.validate(REFERENCE,changed,self.now)
        with self.assertRaisesRegex(ValueError,'unexpired'):
            continuation.validate(REFERENCE,self.bound,1793630946249)

    def test_admitted_serving_failure_cannot_be_retried(self):
        read=op._json
        def mutated(path,*args,**kwargs):
            value=read(path,*args,**kwargs)
            if str(path).endswith('8544077aaef4adc444e74c128b3330f8/OUTCOME.json'):
                value.update(owner_started=True)
            return value
        with patch.object(op,'_json',side_effect=mutated),self.assertRaises(ValueError):
            continuation.validate(REFERENCE,self.bound,self.now)

    def descriptor(self,root):
        observed=continuation.validate(REFERENCE,self.bound,self.now)
        (root/'inputs').mkdir()
        reference=root/'inputs/completed-component.json'
        reference.write_bytes(exchange.canonical(dict(schema=1,new_measurement=False,
            request=observed['request'],result=observed['result'])))
        seal=root/'inputs/completed-component-seal.sha256';seal.write_bytes(Path(observed['seal_path']).read_bytes())
        settings=dict(completed_component=dict(path=str(reference),sha256=runtime.digest(reference)),
                      source_pins={'inputs/completed-component-seal.sha256':runtime.digest(seal)})
        return settings,observed

    def test_native_reader_authenticates_same_campaign_result_and_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();settings,_=self.descriptor(root)
            completed=runtime.completed_component(root,settings,self.bound)
            self.assertEqual(completed['index'],197);self.assertFalse(completed['new_measurement'])
            with self.assertRaises(ValueError):
                runtime.completed_component(root,settings,dict(self.bound,campaign_sha256='0'*64))
            (root/'inputs/completed-component-seal.sha256').write_text('altered')
            with self.assertRaises(ValueError):runtime.completed_component(root,settings,self.bound)

    def test_continuation_submits_profile_then_serving_and_preserves_c4(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();settings,observed=self.descriptor(root)
            completed=runtime.completed_component(root,settings,self.bound)
            (root/'candidate').mkdir();(root/'results').mkdir()
            source=root/'candidate/extend_attention_fwd_candidate.py';source.write_text('# selected fixture\n')
            reviewed=op.R2/'phase2-attn-c4-accumulator-preparation-v2/source/extend_attention_fwd_candidate.py'
            (root/'inputs/qualification-candidate.py').write_bytes(reviewed.read_bytes())
            bound=dict(self.bound,selected_source_set_sha256=runtime.digest(source))
            calls=[]
            def benchmark(config,output,binding):
                self.assertNotIn('workstation_component',config['benchmark'])
                self.assertEqual(source.read_text(),'# selected fixture\n')
                calls.append('profile' if 'profiler' in config['benchmark'] else 'serving')
                return {'fixture':True}
            runtime.qualify(root,bound,benchmark=benchmark,source_set=lambda _:runtime.digest(source),completed=completed)
            self.assertEqual(calls,['profile','serving'])
            self.assertEqual(op._json(root/'results/COMPONENT-REUSED.json')['index'],197)
            self.assertFalse((root/'results/qualification-component').exists())


if __name__=='__main__':unittest.main()
