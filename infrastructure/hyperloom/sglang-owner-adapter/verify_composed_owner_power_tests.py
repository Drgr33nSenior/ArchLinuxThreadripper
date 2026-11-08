"""Production entry and readout predicates, with inert observations only."""
import ast
import copy
import hashlib
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT=Path(__file__).resolve().parent
C=ROOT/'component'
sys.path.insert(0,str(C))
import power_gate
import power_contract


def observation(stamp,watts=235):
    raw=str(watts*1000000)+'\n'
    return dict(schema=1,mode='read-only-post-component-power-comparability-v1',
        required_for_component_acceptance=False,cap_changed=False,status='OBSERVED',
        all_300w=watts==300,observation=dict(observed_monotonic=stamp,devices=[
            dict(bdf=bdf,path='/sys/devices/pci0000:00/'+bdf+'/hwmon/hwmon0/power1_cap',
                 raw=raw,raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),microwatts=watts*1000000)
            for bdf in power_contract.BDFS]))


CARDS=[dict(card=0,started_monotonic=2.,released_monotonic=3.),
       dict(card=1,started_monotonic=4.,released_monotonic=5.)]


class OwnerPowerTests(unittest.TestCase):
    def test_actual_entry_refuses_before_component_and_after_completion(self):
        self.assertIn("pattern='test_owner*.py'",(ROOT/'native_source_gate.py').read_text())
        tree=ast.parse((C/'component_entry.py').read_bytes())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run')
        body=next(n.body for n in fn.body if isinstance(n,ast.Try))
        start=next(i for i,n in enumerate(body) if isinstance(n,ast.Assign) and ast.unparse(n.targets[0])=='power')
        end=next(i for i,n in enumerate(body) if isinstance(n,ast.Assign) and ast.unparse(n.targets[0])=="report['power_gate']")
        code=compile(ast.Module(body=body[start:end+1],type_ignores=[]),'actual-entry-power','exec')
        for pre,post,want_calls in ((235,235,1),(300,235,0),(235,300,1)):
            samples=iter([observation(1,pre),observation(6,post)]);calls=[];report={}
            observer=types.SimpleNamespace(validate_optional=power_contract.validate_optional,optional_observation=lambda:next(samples))
            payload=types.SimpleNamespace(run=lambda deadline:(calls.append(deadline) or dict(cards=copy.deepcopy(CARDS))))
            def load(path,pin,name):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),pin)
                return {'power_contract.py':observer,'power_gate.py':power_gate,'stage_probe.py':payload}[path.name]
            sources={n:hashlib.sha256((C/n).read_bytes()).hexdigest() for n in ('power_contract.py','power_gate.py','stage_probe.py')}
            env=dict(ROOT=C,package=dict(sources=sources),load=load,report=report,
                     os=types.SimpleNamespace(write=lambda *a:None),deadline=10)
            if pre==post==235:
                exec(code,env);self.assertTrue(report['power_gate']['point_observations_only'])
            else:
                with self.assertRaises(ValueError):exec(code,env)
            self.assertEqual(len(calls),want_calls)

    def test_actual_offline_reader_recomputes_power_and_interval_join(self):
        fn=next(n for n in ast.parse((C/'mx_readout.py').read_bytes()).body if isinstance(n,ast.FunctionDef) and n.name=='complete_entry')
        node=next(n for n in fn.body if isinstance(n,ast.Expr) and 'exact frozen power and card-interval join' in ast.unparse(n))
        code=compile(ast.Module(body=[node],type_ignores=[]),'actual-readout-power','exec')
        report=dict(power_admission=observation(1),power_comparability=observation(6))
        report['power_gate']=power_gate.validate_pair(power_contract,report['power_admission'],report['power_comparability'],CARDS)
        def need(ok,reason):
            if not ok:raise ValueError(reason)
        common=types.SimpleNamespace(load=lambda name:{'power_contract':power_contract,'power_gate':power_gate}[name])
        def check(value,cards):exec(code,dict(need=need,common=common,report=value,payload=dict(cards=cards)))
        check(report,CARDS)
        for key in ('power_admission','power_comparability','power_gate','cards'):
            bad=copy.deepcopy(report);cards=copy.deepcopy(CARDS)
            if key=='cards':cards[1]['released_monotonic']=7
            elif key=='power_gate':bad[key]['required_microwatts']=300000000
            else:bad[key]=observation(1 if key=='power_admission' else 6,300)
            with self.assertRaises(ValueError):check(bad,cards)


if __name__=='__main__':unittest.main(verbosity=2)
