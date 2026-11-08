"""Exercise the new predicate with the byte-pinned protected observer validator."""
import copy
import hashlib
from pathlib import Path
import unittest

import power_gate
import test_power_observer_fixture as observer


def observation(stamp, watts=235):
    raw=str(watts*1000000)+'\n'
    return dict(schema=1,mode='read-only-post-component-power-comparability-v1',
        required_for_component_acceptance=False,cap_changed=False,status='OBSERVED',
        all_300w=watts==300,observation=dict(observed_monotonic=stamp,devices=[
            dict(bdf=bdf,path='/sys/devices/pci0000:00/'+bdf+'/hwmon/hwmon0/power1_cap',
                 raw=raw,raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                 microwatts=watts*1000000) for bdf in observer.BDFS]))


class PowerTests(unittest.TestCase):
    def test_protected_validator_is_byte_identical_and_success_is_point_only(self):
        self.assertEqual(hashlib.sha256(Path(observer.__file__).read_bytes()).hexdigest(),
            '4552caaeb4bd263ece4b7cb226012a8e7ff91c1ed55360be055089700d2a3436')
        result=power_gate.validate_pair(observer,observation(1),observation(10),[
            dict(card=0,started_monotonic=2,released_monotonic=4),
            dict(card=1,started_monotonic=5,released_monotonic=9)])
        self.assertTrue(result['point_observations_only'])
        self.assertFalse(result['cap_changed'])
        self.assertEqual(result['required_microwatts'],235000000)

    def test_unverified_wrong_cap_raw_and_device_are_refused(self):
        missing=observation(1)
        missing.update(status='UNVERIFIED',observation=None,all_300w=None,reason='unavailable')
        wrong_raw=observation(1);wrong_raw['observation']['devices'][1]['raw']='300000000\n'
        wrong_device=observation(1);wrong_device['observation']['devices'][0]['bdf']='0000:ff:00.0'
        one_wrong=observation(1);one_wrong['observation']['devices'][1]=observation(1,300)['observation']['devices'][1]
        for row in (missing,observation(1,300),wrong_raw,wrong_device,one_wrong):
            with self.assertRaises(ValueError):power_gate.validate(observer,row)

    def test_card_work_must_be_bracketed_and_sequential(self):
        good=[dict(card=0,started_monotonic=2,released_monotonic=4),
              dict(card=1,started_monotonic=5,released_monotonic=9)]
        for where,key,value in ((0,'started_monotonic',0),(1,'released_monotonic',11),
                                (1,'started_monotonic',3),(0,'released_monotonic',float('nan')),
                                (0,'started_monotonic',True),(1,'card',0)):
            changed=copy.deepcopy(good);changed[where][key]=value
            with self.assertRaises(ValueError):
                power_gate.validate_pair(observer,observation(1),observation(10),changed)
        for changed in ([],list(reversed(good))):
            with self.assertRaises(ValueError):
                power_gate.validate_pair(observer,observation(1),observation(10),changed)

    def test_compiler_refusal_after_card0_still_requires_both_power_readbacks(self):
        cards=[dict(card=0,started_monotonic=2,released_monotonic=4)]
        power_gate.validate_pair(observer,observation(1),observation(5),cards)
        with self.assertRaises(ValueError):
            power_gate.validate_pair(observer,observation(1),observation(5,300),cards)


if __name__=='__main__':unittest.main(verbosity=2)
