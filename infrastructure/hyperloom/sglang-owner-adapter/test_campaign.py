"""Actual owned CPU-child lifecycle and inert qualification measurement tests."""
import copy
import shutil
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE),str(HERE.parents[2]/'tests/workstation')]
import campaign
import qualification
import hyperloom_exchange as exchange
import hyperloom_owner_backend as backend
import test_hyperloom_owner_backend as fixtures


class LifecycleTests(unittest.TestCase):
    def test_prestart_cpu_repair_preserves_original_deadline_and_refuses_started_controller(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();(root/'native-qualification').mkdir();(root/'transport').mkdir();(root/'controller').mkdir()
            bound={key:'a'*64 for key in ('selection_sha256','runtime_sha256','workload_sha256','selected_source_set_sha256')}
            grant=dict(bound,started_unix_ms=1000,deadline_unix_ms=14401_000)
            result=dict(status='FAILED',controller_reaped=True,campaign_started_unix_ms=1000,deadline_unix_ms=14401_000)
            write=campaign.controller.write
            repair=dict(grant=write(root/'CAMPAIGN-GRANT.json',grant),result=write(root/'CAMPAIGN-RESULT.json',result),
                cancellation=write(root/'transport/cancel.stdout',dict(status='NOT_STARTED')),
                client_receipt=write(root/'native-qualification/RECEIPT.json',dict(child_reaped=True,owned_group_absent=True)))
            self.assertEqual(campaign.campaign_window({'cpu_startup_repair':repair},bound,300000),(1000,14401_000))
            with self.assertRaisesRegex(ValueError,'unexpired'):
                campaign.campaign_window({'cpu_startup_repair':repair},bound,14401_000)
            (root/'controller/CONTROLLER-STARTED.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'initialized controller'):
                campaign.campaign_window({'cpu_startup_repair':repair},bound,300000)

    def test_prestart_cpu_repair_refuses_submitted_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();(root/'controller/jobs/claimed').mkdir(parents=True)
            repair={key:{'path':str(root/name),'sha256':'a'*64} for key,name in (
                ('grant','CAMPAIGN-GRANT.json'),('result','CAMPAIGN-RESULT.json'),
                ('cancellation','transport/cancel.stdout'),('client_receipt','native-qualification/RECEIPT.json'))}
            with patch.object(campaign.controller,'read',side_effect=[{},
                {'status':'FAILED','controller_reaped':True},{'status':'NOT_STARTED'},
                {'child_reaped':True,'owned_group_absent':True}]):
                with self.assertRaisesRegex(ValueError,'submitted owner job'):
                    campaign.campaign_window({'cpu_startup_repair':repair},{},300000)

    def run_child(self,script,*,controller=None,seconds=3,cancel=None):
        with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
            return campaign.run_client([sys.executable,'-I','-B','-c',script],
                controller_process=controller or SimpleNamespace(poll=lambda:None),
                deadline=time.time()+seconds,cancel=cancel or Mock(),output=out,error=err)

    def test_natural_client_exit_is_reaped(self):
        cancel=Mock()
        result=self.run_child('print("CPU fixture")',cancel=cancel)
        self.assertEqual(result['exit_code'],0);self.assertIsNone(result['failure'])
        self.assertTrue(result['child_reaped'] and result['owned_group_absent']);cancel.assert_not_called()

    def test_dead_controller_cancels_once_and_reaps_only_owned_client(self):
        cancel=Mock()
        result=self.run_child('import time;time.sleep(30)',controller=SimpleNamespace(poll=lambda:1),cancel=cancel)
        self.assertIn('controller exited',result['failure'])
        cancel.assert_called_once();self.assertTrue(result['child_reaped'] and result['owned_group_absent'])
        self.assertLess(result['elapsed_seconds'],6)

    def test_deadline_and_transport_cancellation_failure_are_retained(self):
        cancel=Mock(side_effect=TimeoutError('CPU transport fixture failed'))
        result=self.run_child('import time;time.sleep(30)',seconds=.1,cancel=cancel)
        self.assertIn('TimeoutError',result['failure']);self.assertIn('transport fixture',result['cancellation_error'])
        cancel.assert_called_once();self.assertTrue(result['child_reaped'] and result['owned_group_absent'])

    def test_activation_timeout_is_retained_and_cannot_restart_the_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            instance=campaign.Campaign.__new__(campaign.Campaign)
            instance.root=root;instance.child=None;instance.started=None;instance.deadline=None
            instance.plan={'checkpoint_root':str(root)}
            instance.activate=Mock(side_effect=TimeoutError('CPU fixture staging timeout'))
            with patch.object(campaign.op,'_checkpoint',return_value={'state':{}}),patch.object(campaign.op,'_idle'):
                result=instance.run()
                self.assertEqual(result['status'],'FAILED');self.assertIn('staging timeout',result['reason'])
                self.assertIsNone(result['campaign_started_unix_ms'])
                self.assertTrue((root/'CAMPAIGN-RESULT.json').is_file())
                with self.assertRaises(ValueError):instance.run()
            instance.activate.assert_called_once()

    def test_qualification_failure_retains_diagnostics_without_retry_or_promotion(self):
        for read_fails, remaining in ((False, 120), (True, 120), (False, 10)):
            with self.subTest(read_fails=read_fails, remaining=remaining), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                instance = campaign.Campaign.__new__(campaign.Campaign)
                instance.root = root
                instance.native = Path('/home/developer/hyperloom-owner-campaign-0123456789abcdef')
                instance.child = None
                instance.started = 1000000
                instance.deadline = instance.started + remaining * 1000
                instance.plan = {'checkpoint_root': str(root)}
                instance.activate = Mock(return_value=({}, {}))
                instance.runtime_settings = Mock(return_value={})
                instance.launch = Mock(side_effect=RuntimeError('warm C2 cache contract failed'))
                observed = {'files': {'qualification-runtime-result.json': {
                    'exit_code': 1, 'child_reaped': True, 'owned_group_absent': True}}}
                instance.call = Mock(side_effect=TimeoutError('diagnostic read failed')) if read_fails else Mock(return_value=observed)
                instance.transfer = Mock()
                with patch.object(campaign.time, 'time', return_value=1000), \
                     patch.object(campaign.op, '_checkpoint', return_value={'state': {}}), \
                     patch.object(campaign.op, '_idle'), patch.object(campaign.qualification, 'prepare') as prepare:
                    result = instance.run()
                self.assertEqual(result['status'], 'FAILED')
                self.assertEqual(result['reason'], 'RuntimeError: warm C2 cache contract failed')
                self.assertTrue(result['qualification_attempted'])
                self.assertNotIn('optimizer_attempted', result)
                self.assertFalse(result['automatic_retry'])
                self.assertEqual(result['deadline_unix_ms'], instance.deadline)
                instance.launch.assert_called_once_with('qualification', {})
                prepare.assert_not_called()
                instance.transfer.assert_not_called()
                if remaining <= 15:
                    instance.call.assert_not_called()
                    self.assertIn('no time', result['qualification_retention_error'])
                else:
                    self.assertEqual(instance.call.call_count, 1)
                    self.assertEqual(instance.call.call_args.args[0], 'read-qualification-failure')
                    self.assertEqual(instance.call.call_args.kwargs['seconds'], 20)
                    if read_fails:
                        self.assertIn('diagnostic read failed', result['qualification_retention_error'])
                    else:
                        self.assertEqual(campaign.controller.read(result['qualification_failure']), observed)
                        self.assertNotIn('qualification_retention_error', result)
                self.assertTrue((root / 'CAMPAIGN-RESULT.json').is_file())

    def test_optimizer_failure_still_collects_once_and_retains_transport_errors(self):
        for read_fails,collect_fails in ((False,False),(True,False),(False,True)):
            with self.subTest(read_fails=read_fails,collect_fails=collect_fails),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary).resolve()
                instance=campaign.Campaign.__new__(campaign.Campaign)
                instance.root=root;instance.native=Path('/home/developer/hyperloom-owner-campaign-0123456789abcdef')
                instance.child=None;instance.started=int(time.time()*1000);instance.deadline=instance.started+120000
                instance.plan={'checkpoint_root':str(root),'cpu_proof':{},'evidence_bundle':{}}
                instance.activate=Mock(return_value=({},{}));instance.runtime_settings=Mock(return_value={})
                instance.launch=Mock(side_effect=[{},RuntimeError('controller parked fixture')])
                observed={'qualification-inside.json':{},'qualification-runtime-result.json':{}}
                instance.call=Mock(side_effect=[{'files':observed},
                    TimeoutError('read transport fixture') if read_fails else {'files':{'runtime':'retained'}}])
                instance.transfer=Mock()
                instance.collect=Mock(side_effect=TimeoutError('collect transport fixture')) if collect_fails else Mock(return_value={'members':2})
                def prepare(_controller,_bound,destination,**_kwargs):
                    destination.mkdir();(destination/'QUALIFICATION.json').write_text('{}\n')
                    return {}
                with patch.object(campaign.qualification,'prepare',side_effect=prepare),\
                     patch.object(campaign.op,'_checkpoint',return_value={'state':{}}),patch.object(campaign.op,'_idle'):
                    result=instance.run()
                self.assertEqual(result['status'],'FAILED');self.assertIn('controller parked fixture',result['reason'])
                instance.collect.assert_called_once_with(seconds=20)
                self.assertEqual(instance.call.call_count,2);self.assertTrue(result['optimizer_attempted'])
                self.assertEqual('runtime_result' in result['retention_errors'],read_fails)
                self.assertEqual('artifacts' in result['retention_errors'],collect_fails)
                self.assertTrue((root/'CAMPAIGN-RESULT.json').is_file())


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.BackendTests();self.fixture.setUp();self.addCleanup(self.fixture.temporary.cleanup)
        f=self.fixture
        request,result,_,_=f.qualification()
        self.jobs=[(request,result)]
        for kind in ('profile','component'):
            out=f.root/kind;out.mkdir()
            config=copy.deepcopy(f.config)
            if kind=='profile':config['benchmark']['profiler']={'torch_profiler':{'enabled':True}}
            else:config['benchmark']['workstation_component']={'contract':'c4-fixed-geometry-v1'}
            job,req=backend.start(config,out,f.binding)
            if kind=='component':
                req['sources'][0]['sha256']='e945ad72296777556ed93859263442c077e0f1d1d7082268c1f669e157a324e9'
                req['source_set_sha256']=exchange.digest(exchange.canonical(req['sources']))
            report=f.report(req)
            if kind=='profile':report.update(diagnostic_only=True,workstation_latency=None,
                workstation_latency_control=None,profile_coverage={'complete':True,'owner_seal_sha256':'f'*64})
            else:report=dict(schema=1,diagnostic_only=True,serving_tested=False,adoption_authorized=False,
                correct_both_cards=True,useful_both_cards=False,zero_prefix_no_regression=False,eligible_for_serving=False,
                owner_binding=dict(report['owner_binding'],controller_sha256=f.binding['controller_sha256']))
            res=dict(result,**{key:req[key] for key in ('id','campaign_sha256','config_sha256','source_set_sha256')},
                request_sha256=exchange.digest(exchange.canonical(req)),
                payload={('component_report' if kind=='component' else 'benchmark_report'):report})
            self.jobs.append((req,res))

    def test_correct_negative_component_can_qualify_harness_but_cannot_serve(self):
        result,comparison=qualification.measurements(self.jobs,self.fixture.binding)
        self.assertFalse(result['component'][2]['eligible_for_serving'])
        self.assertEqual(comparison['reasons'],['balanced_gain_not_resolved'])

    def test_foreign_campaign_missing_release_and_failed_oracle_refuse(self):
        for mutate in (lambda j:j[0][0].update(campaign_sha256='0'*64),
                       lambda j:j[0][1].update(independent_release=False),
                       lambda j:j[2][1]['payload']['component_report'].update(correct_both_cards=False)):
            jobs=copy.deepcopy(self.jobs);mutate(jobs)
            with self.assertRaises(ValueError):qualification.measurements(jobs,self.fixture.binding)

    def test_aa_improvement_or_regression_beyond_noise_is_not_qualification(self):
        for scale in (.8,1.2):
            jobs=copy.deepcopy(self.jobs)
            for case in jobs[0][1]['payload']['benchmark_report']['workstation_latency']['cases']:
                for sample in case['samples']:
                    sample['request_e2e_ms']=[v*scale for v in sample['request_e2e_ms']]
                    sample['group_wall_ms']*=scale
            with self.subTest(scale=scale),self.assertRaises(ValueError):
                qualification.measurements(jobs,self.fixture.binding)

    def test_proof_writer_checks_real_seals_and_retains_readonly_reuse_contract(self):
        from test_hyperloom_owner_profile import fixture as profile_fixture
        op=campaign.op;root=self.fixture.root;bound=self.fixture.binding
        jobs=root/'controller/jobs';jobs.mkdir(parents=True)
        for index,(request,result) in enumerate(self.jobs,191):
            kind=request['kind'];job=jobs/request['id'];(job/'snapshot').mkdir(parents=True)
            native=root/('sealed-'+kind);native.mkdir();(native/'WITNESS.txt').write_text('CPU fixture, no GPU operation\n')
            seal=op.sealer().seal(native)
            pin=seal['manifest_sha256'];result['owner_seal_sha256']=pin
            report=result['payload']['component_report' if kind=='component' else 'benchmark_report']
            report['owner_binding']['owner_seal_sha256']=pin
            if kind=='serving':
                for key in ('workstation_latency','workstation_latency_control'):report[key]['owner_seal_sha256']=pin
            if kind=='profile':
                report['profile_coverage']['owner_seal_sha256']=pin
                report['profile_capture']=profile_fixture()[1]['profile_capture']
                report['profile_capture'].update(report['owner_binding'])
            checkpoint=op._write(job/'CLOSED.json',dict(schema=1,independent_release_verified=True,
                last_completed_global_attempt_index=index,active_owner=None,active_caller=None))
            result.update(index=index,native_root=str(native),checkpoints={'closed':dict(checkpoint,number=index)})
            op._write(job/'snapshot/REQUEST.json',request)
            outcome=op._write(job/'OUTCOME.json',result)
            op._write(job/'PUBLICATION.json',dict(published_sha256=outcome['sha256']))
        cpu=op._write(root/'cpu.json',dict(status='PASS',controller_sha256=bound['controller_sha256'],
            failure_retention=True,actual_baseline_writeback=True,actual_profile_writeback=True,upstream_balanced_keep_paths=True))
        bundle=op._write(root/'bundle.json',dict(artifacts=[{'id':name} for name in
            ('serving_profile','trace_census','research_reconciliation')]))
        observation=dict(gpu_devices=False,replies_read_only=True,inputs_read_only=True,
                         deadline_unix_ms=bound['campaign_deadline_unix_ms'])
        result=dict(mode='qualification',exit_code=0,failure=None,child_reaped=True,owned_group_absent=True,
                    deadline_unix_ms=bound['campaign_deadline_unix_ms'])
        kwargs=dict(native_root=Path('/home/developer/hyperloom-owner-campaign-'+'a'*16),
                    runtime_observation=observation,runtime_result=result,cpu_proof=cpu,evidence_bundle=bundle)
        proof=qualification.prepare(root/'controller',bound,root/'proof',**kwargs)
        self.assertEqual(set(proof['measurements']),{'serving_aa','c1_c2_profile'})
        self.assertEqual(len(proof['owner_seals']),3)
        self.assertTrue(all(proof['checks'].values()))
        self.assertEqual((root/'proof/serving/OWNER-JOB.json').read_bytes(),
                         exchange.canonical(self.jobs[0][0]))
        remaining=root/'continuation-controller/jobs';remaining.mkdir(parents=True)
        for request,_ in self.jobs:
            if request['kind']!='component':shutil.copytree(jobs/request['id'],remaining/request['id'])
        component_request,component_result=self.jobs[2]
        retained=dict(component_root=str(jobs/component_request['id']),result=component_result)
        reference={'fixture':'authenticated separately by the continuation tests'}
        with patch.object(qualification.continuation,'validate',return_value=retained) as validate:
            continued=qualification.prepare(root/'continuation-controller',bound,root/'continued-proof',
                                            continued=reference,**kwargs)
        validate.assert_called_once()
        self.assertEqual(len(continued['owner_seals']),3)
        self.assertFalse(continued['continued_component']['new_measurement'])
        self.assertEqual(continued['continued_component']['request_sha256'],component_result['request_sha256'])
        self.assertEqual((root/'continued-proof/component/component_report.json').read_bytes(),
                         exchange.canonical(component_result['payload']['component_report']))
        (root/'sealed-serving/WITNESS.txt').write_text('tampered fixture\n')
        with self.assertRaises(AssertionError):qualification.prepare(root/'controller',bound,root/'refused',**kwargs)
        self.assertFalse((root/'refused').exists())


if __name__=='__main__':unittest.main(verbosity=2)
