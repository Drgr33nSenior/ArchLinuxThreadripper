"""CPU checks for the finite optimizer's identity, mounts and shutdown."""
import copy
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import optimizer_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "campaign"; self.root.mkdir(mode=0o700)
        for name in (*runtime.WRITABLE, "replies", "sdk", "evidence", "inputs"):
            (self.root / name).mkdir(mode=0o700)
        (self.root / "optimizer_runtime.py").write_text("# CPU fixture runtime\n")
        (self.root / "sdk/example.py").write_text("# CPU fixture SDK\n")
        (self.root / "inputs/model-config.json").write_text('{"fixture":true}\n')
        model_pin = runtime.digest(self.root / "inputs/model-config.json")
        self.model_pin = patch.object(runtime, "MODEL_CONFIG_SHA", model_pin)
        self.model_pin.start(); self.addCleanup(self.model_pin.stop)
        (self.root / "inputs/tokenizer-config.json").write_text('{"fixture":true}\n')
        tokenizer_pin = patch.object(runtime, "TOKENIZER_CONFIG_SHA", model_pin)
        tokenizer_pin.start(); self.addCleanup(tokenizer_pin.stop)
        (self.root / "inputs/qualification-candidate.py").write_text('# CPU qualification fixture\n')
        qualification_pin = patch.object(runtime,"QUALIFICATION_CANDIDATE_SHA",
                                         runtime.digest(self.root / "inputs/qualification-candidate.py"))
        qualification_pin.start(); self.addCleanup(qualification_pin.stop)
        now = int(time.time()*1000)
        self.binding = {"campaign_deadline_unix_ms": now+14400_000, "benchmark": {"model":runtime.MODEL},
            "requests_root": str(self.root/"requests"), "replies_root": str(self.root/"replies"),
            "sources": {"extend_attention_fwd_candidate.py": str(self.root/"candidate/extend_attention_fwd_candidate.py")}}
        self.binding_path = self.root/"inputs/binding.json"
        self.binding_path.write_bytes(runtime.canonical(self.binding))
        self.settings = dict(schema=1, mode="qualification", root=str(self.root),
            started_unix_ms=now, deadline_unix_ms=now+14400_000,
            binding={"path":str(self.binding_path),"sha256":runtime.digest(self.binding_path)},
            source_pins={name:runtime.digest(self.root/name) for name in
                         ("optimizer_runtime.py","sdk/example.py","inputs/model-config.json","inputs/tokenizer-config.json",
                          "inputs/qualification-candidate.py")},
            environment={}, automatic_retry=False, adoption_authorized=False)
        self.path = self.root/"inputs/runtime.json"

    def write(self):
        self.path.write_bytes(runtime.canonical(self.settings))
        return runtime.digest(self.path)

    def test_validated_runtime_is_single_deadline_and_closed_source(self):
        pin = self.write()
        root, settings, binding, seconds = runtime.validate(self.path, pin, native=False)
        self.assertEqual(root,self.root)
        self.assertEqual(binding,self.binding)
        self.assertLessEqual(seconds,14385)
        (self.root/"sdk/extra.py").write_text("# undeclared")
        with self.assertRaisesRegex(ValueError,"inventory"):
            runtime.validate(self.path,pin,native=False)

    def test_binding_source_and_deadline_cannot_drift(self):
        original=copy.deepcopy(self.binding)
        for key,value in (("requests_root",str(self.root/"work")),
                          ("campaign_deadline_unix_ms",original["campaign_deadline_unix_ms"]+1)):
            self.binding=dict(original,**{key:value})
            self.binding_path.write_bytes(runtime.canonical(self.binding))
            self.settings["binding"]["sha256"]=runtime.digest(self.binding_path)
            pin=self.write()
            with self.subTest(key=key),self.assertRaises(ValueError):
                runtime.validate(self.path,pin,native=False)

    def test_deadline_cannot_restart_extend_or_launch_without_cleanup_room(self):
        start=1000_000
        config=dict(started_unix_ms=start,deadline_unix_ms=start+14400_000)
        self.assertEqual(runtime.budget(config,now=1000),14385)
        self.assertEqual(runtime.budget(config,now=1100),14285)
        for candidate,now in ((dict(config,deadline_unix_ms=config["deadline_unix_ms"]+1),1000),
                              (config,config["deadline_unix_ms"]/1000-10),
                              (config,990)):
            with self.assertRaises(ValueError):runtime.budget(candidate,now=now)

    def test_qualification_mounts_no_login_or_network_and_replies_are_read_only(self):
        argv=runtime.command(self.root,self.path,self.write(),self.settings)
        self.assertNotIn("--share-net",argv)
        self.assertNotIn(str(runtime.AUTH),argv)
        binds=[argv[i+1:i+3] for i,value in enumerate(argv) if value=="--bind"]
        self.assertEqual(binds,[[str(self.root/name)]*2 for name in runtime.WRITABLE])
        ro=[argv[i+1:i+3] for i,value in enumerate(argv) if value=="--ro-bind"]
        self.assertIn([str(self.root/"replies")]*2,ro)
        self.assertIn([str(self.root/"inputs/model-config.json"),runtime.MODEL+"/config.json"],ro)
        self.assertNotIn("/dev/kfd",argv)
        self.assertNotIn("/dev/dri",argv)

    def test_optimizer_receives_remaining_hours_and_preserves_closeout(self):
        settings=dict(started_unix_ms=1000_000,deadline_unix_ms=15400_000,
                      argv=['optimize','--max-hours','4','--no-kernel'])
        args=runtime.optimizer_argv(settings,now=4600)
        self.assertLessEqual(float(args[2])*3600,10800-15-300)
        self.assertGreater(float(args[2]),2.9)
        self.assertEqual(settings['argv'][2],'4')
        settings['argv'][2]='1'
        self.assertEqual(runtime.optimizer_argv(settings,now=4600)[2],'1.0')
        for value in ('nan','inf','0','5'):
            settings['argv'][2]=value
            with self.subTest(value),self.assertRaises(ValueError):
                runtime.optimizer_argv(settings,now=4600)
        settings['argv']=['optimize','--max-hours','4','--max-hours','1']
        with self.assertRaises(ValueError):runtime.optimizer_argv(settings,now=4600)
        settings['argv']=['optimize','--max-hours','4']
        with self.assertRaises(ValueError):runtime.optimizer_argv(settings,now=15300)

    def test_optimizer_mounts_only_dedicated_auth_and_forbids_api_env(self):
        self.settings.update(mode="optimizer",model="gpt-6.1-sol",
            argv=["optimize","--no-kernel","--degraded-kb","--degraded-pr","--no-enable-roofline"])
        auth=self.parent/"private-login";auth.mkdir(mode=0o700)
        with patch.object(runtime,"AUTH",auth):
            argv=runtime.command(self.root,self.path,self.write(),self.settings)
            self.assertIn("--share-net",argv)
            self.assertIn(str(auth),argv)
        self.settings["environment"]["OPENAI_API_KEY"]="fixture-not-a-credential"
        with self.assertRaisesRegex(ValueError,"environment"):
            runtime.validate(self.path,self.write(),native=False)

    def test_real_subprocess_completes_or_is_reaped_at_deadline(self):
        with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
            good=runtime.run_group([sys.executable,"-I","-B","-c","print('finite fixture')"],
                                   seconds=2,output=out,error=err)
            self.assertEqual(good["exit_code"],0)
            self.assertTrue(good["child_reaped"] and good["owned_group_absent"])
            stopped=runtime.run_group([sys.executable,"-I","-B","-c","import time;time.sleep(30)"],
                                      seconds=1,output=out,error=err)
            self.assertEqual(stopped["failure"],"TimeoutExpired")
            self.assertTrue(stopped["child_reaped"] and stopped["owned_group_absent"])
            self.assertNotEqual(stopped["exit_code"],0)
            self.assertLess(stopped["elapsed_seconds"],6)

    def test_qualification_orders_component_then_profile_and_aa_with_selected_source(self):
        candidate=self.root/'candidate/extend_attention_fwd_candidate.py';candidate.write_text('# selected\n')
        self.binding['selected_source_set_sha256']=runtime.digest(candidate)
        calls=[]
        def benchmark(config, output, bound):
            bench=config['benchmark']
            kind='component' if 'workstation_component' in bench else 'profile' if 'profiler' in bench else 'serving'
            calls.append(kind)
            self.assertEqual(candidate.read_bytes(),(self.root/'inputs/qualification-candidate.py').read_bytes()
                             if kind=='component' else b'# selected\n')
            return {'correct_both_cards':True,'eligible_for_serving':False,'fixture':True}
        self.assertEqual(runtime.qualify(self.root,self.binding,benchmark=benchmark,
                                         source_set=lambda bound:runtime.digest(candidate)),0)
        self.assertEqual(calls,['component','profile','serving'])
        self.assertEqual(candidate.read_bytes(),b'# selected\n')

    def test_profile_failure_stops_before_serving_and_preserves_selected_source(self):
        candidate=self.root/'candidate/extend_attention_fwd_candidate.py';candidate.write_text('# selected\n')
        self.binding['selected_source_set_sha256']=runtime.digest(candidate)
        calls=[]
        def benchmark(config, output, bound):
            bench=config['benchmark']
            kind='component' if 'workstation_component' in bench else 'profile' if 'profiler' in bench else 'serving'
            calls.append(kind)
            if kind=='profile':raise ValueError('fixture profile failed')
            return {'correct_both_cards':True,'eligible_for_serving':False,'fixture':True}
        with self.assertRaisesRegex(ValueError,'profile failed'):
            runtime.qualify(self.root,self.binding,benchmark=benchmark,
                            source_set=lambda bound:runtime.digest(candidate))
        self.assertEqual(calls,['component','profile'])
        self.assertEqual(candidate.read_bytes(),b'# selected\n')
        self.assertFalse((self.root/'results/qualification-serving').exists())

    def test_qualification_failure_restores_source_and_starts_no_model(self):
        candidate=self.root/'candidate/extend_attention_fwd_candidate.py';candidate.write_text('# selected\n')
        self.binding['selected_source_set_sha256']=runtime.digest(candidate)
        calls=[]
        def benchmark(config, output, bound):
            calls.append(config);raise ValueError('fixture component failed')
        with self.assertRaisesRegex(ValueError,'component failed'):
            runtime.qualify(self.root,self.binding,benchmark=benchmark,
                            source_set=lambda bound:runtime.digest(candidate))
        self.assertEqual(len(calls),1)
        self.assertEqual(candidate.read_bytes(),b'# selected\n')
        self.assertFalse((self.root/'results/qualification-serving').exists())


if __name__=="__main__":
    unittest.main()
