"""Stage and supervise one finite owner-backed Hyperloom campaign.

Staging performs file-only native work. Execution consumes an explicit retained
user instruction once and starts the shared four-hour clock before qualification.
The same controller stays alive through qualification and optimization.
"""

import argparse
import base64
import json
import os
import re
from pathlib import Path, PurePosixPath
import subprocess
import signal
import sys
import time
import zlib

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[2] / 'lib/workstation')]
import controller
import exchange_ssh
import owner_operation as op
import optimizer_runtime as runtime
import qualification
import continuation
import hyperloom_exchange as exchange
from hyperloom_sglang import checked_bytes, checked_json, require


REMOTE = r'''
import base64,hashlib,json,os,re,resource,signal,socket,sys,time,zlib
from pathlib import Path,PurePosixPath
assert os.getuid()==os.geteuid()==1000 and socket.gethostname()=='threadripper-ai'
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==BOOT
resource.setrlimit(resource.RLIMIT_CPU,(30,30))
resource.setrlimit(resource.RLIMIT_AS,(512<<20,512<<20))
resource.setrlimit(resource.RLIMIT_FSIZE,(32<<20,32<<20))
def audit(event,args):
 if event=='import' and args[0].split('.')[0] in {'torch','triton','sglang','numpy'}:raise RuntimeError('framework import refused')
 if event in {'subprocess.Popen','os.system','os.fork','os.posix_spawn','socket.__new__','socket.connect'} or event.startswith('os.exec'):raise RuntimeError('child/network action refused')
 if event=='open' and isinstance(args[0],(str,bytes)) and os.fsdecode(args[0]).startswith(('/dev/kfd','/dev/dri/')):raise RuntimeError('GPU access refused')
sys.addaudithook(audit)
raw=zlib.decompress(base64.b64decode(PAYLOAD));assert len(raw)<=180<<20
assert hashlib.sha256(raw).hexdigest()==PAYLOAD_SHA
data=json.loads(raw);root=Path(data['root'])
assert root.parent==Path('/home/developer') and re.fullmatch('hyperloom-owner-campaign-[0-9a-f]{16}',root.name)
assert root.resolve()==root
action=data['action']
if action=='write':
 if data['fresh']:
  root.mkdir(mode=0o700)
  for name in ('inputs','sdk','evidence','candidate','requests','replies','work','results'):(root/name).mkdir(mode=0o700)
 assert root.is_dir() and root.stat().st_uid==1000 and root.stat().st_mode&0o777==0o700
 assert 0<len(data['files'])<=4096
 total=0
 for name,item in data['files'].items():
  part=PurePosixPath(name);assert not part.is_absolute() and '..' not in part.parts and part.as_posix()==name
  assert part.parts[0] in {'inputs','sdk','evidence','candidate','optimizer_runtime.py'}
  p=root/part;assert p.resolve()==p and not p.exists()
  content=base64.b64decode(item['data'],validate=True);assert len(content)<=32<<20
  assert hashlib.sha256(content).hexdigest()==item['sha256'];total+=len(content);assert total<=128<<20
  p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  with p.open('xb') as f:f.write(content)
  p.chmod(0o600)
 print(json.dumps({'schema':1,'status':'STAGED','files':len(data['files']),'bytes':total,'gpu_work':False}))
elif action=='read':
 output={}
 for name in data['names']:
  assert name in {'qualification-inside.json','qualification-runtime-result.json','optimizer-runtime-result.json','optimizer-effective-budget.json'}
  p=root/'results'/name;assert p.resolve()==p and p.is_file() and p.stat().st_size<=2<<20
  output[name]=json.loads(p.read_bytes())
 print(json.dumps({'schema':1,'files':output}))
elif action=='cancel':
 mode=data['mode'];assert mode in {'qualification','optimizer'}
 cancelled=root/'results'/(mode+'-cancelled.json')
 with cancelled.open('x') as f:json.dump({'reason':'trusted campaign supervisor stopped the CPU client'},f)
 p=root/'results'/(mode+'-runtime-started.json')
 if not p.exists():
  print(json.dumps({'schema':1,'status':'NOT_STARTED','independent_release_claimed':False}))
 else:
  r=json.loads(p.read_bytes());pid=r['pid'];assert type(pid)is int and pid>1 and r['boot_id']==BOOT
  proc=Path('/proc')/str(pid)
  if not proc.exists():status='ALREADY_EXITED'
  else:
   assert proc.stat().st_uid==1000
   assert (proc/'stat').read_text().rsplit(')',1)[1].split()[19]==r['process_start_ticks']
   argv=(proc/'cmdline').read_bytes().split(b'\0')
   assert str(root/'optimizer_runtime.py').encode() in argv and b'launch' in argv and r['config'].encode() in argv
   os.kill(pid,signal.SIGTERM);status='SIGTERM_SENT_TO_OWNED_CPU_RUNTIME'
  print(json.dumps({'schema':1,'status':status,'independent_release_claimed':False}))
elif action=='collect':
 files={};inventory=[];total=0
 base=root/'work/sessions'
 allowed={'reports','checkpoints','findings','patches','optimizer_runs'}
 for p in sorted(base.rglob('*')):
  if not p.is_file() or p.resolve()!=p:continue
  parts=p.relative_to(base).parts
  if p.name!='manifest.json' and not any(part in allowed for part in parts[:-1]):continue
  assert p.stat().st_size<=32<<20
  content=p.read_bytes();total+=len(content);assert total<=64<<20 and len(files)<4096
  name=p.relative_to(root).as_posix()
  pin=hashlib.sha256(content).hexdigest();files[name]={'sha256':pin,'data':base64.b64encode(content).decode()}
  inventory.append({'path':name,'sha256':pin,'bytes':len(content)})
 p=root/'candidate/extend_attention_fwd_candidate.py';content=p.read_bytes();assert len(content)<=4<<20
 files['candidate/extend_attention_fwd_candidate.py']={'sha256':hashlib.sha256(content).hexdigest(),'data':base64.b64encode(content).decode()}
 raw=json.dumps(files,sort_keys=True,separators=(',',':')).encode();packed=zlib.compress(raw,9)
 assert len(packed)<=12<<20
 print(json.dumps({'schema':1,'files_zlib_base64':base64.b64encode(packed).decode(),'raw_sha256':hashlib.sha256(raw).hexdigest(),
                   'raw_bytes':len(raw),'inventory':inventory,'gpu_work':False}))
else:raise ValueError('unknown campaign file operation')
'''


def encode_files(files):
    return {name: dict(sha256=exchange.digest(raw), data=base64.b64encode(raw).decode())
            for name, raw in files.items()}


def remote_recipe(native_root, action, **values):
    payload = exchange.canonical(dict(root=str(native_root), action=action, **values))
    require(len(payload) <= 180 << 20, 'campaign transfer exceeds its bound')
    source = ('BOOT=' + repr(runtime.BOOT) + '\nPAYLOAD_SHA=' + repr(exchange.digest(payload))
              + '\nPAYLOAD=' + repr(base64.b64encode(zlib.compress(payload, 9)).decode()) + '\n' + REMOTE).encode()
    compile(source, 'campaign-file-operation', 'exec', dont_inherit=True)
    return source


def run_client(argv, *, controller_process, deadline, cancel, output, error):
    """Supervise only the owned CPU client; never terminate a GPU owner."""
    started=time.monotonic()
    try:
        child=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=output,stderr=error,start_new_session=True)
    except OSError as error:
        return dict(pid=None,signals=[],failure=type(error).__name__+': '+str(error),
                    cancellation_error=None,exit_code=None,child_reaped=True,owned_group_absent=True,
                    elapsed_seconds=time.monotonic()-started)
    result=dict(pid=child.pid,signals=[],failure=None,cancellation_error=None)
    try:
        while child.poll() is None:
            if controller_process.poll() is not None:
                raise RuntimeError('controller exited while the CPU client was active')
            if time.time()>=deadline:
                raise TimeoutError('shared campaign client deadline')
            time.sleep(.2)
    except BaseException as failure:
        result['failure']=type(failure).__name__+': '+str(failure)
        try:cancel()
        except Exception as error:result['cancellation_error']=type(error).__name__+': '+str(error)
    finally:
        for sig in (signal.SIGTERM,signal.SIGKILL):
            try:os.killpg(child.pid,sig)
            except ProcessLookupError:break
            result['signals'].append(int(sig))
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:pass
        if child.poll() is None:child.wait(timeout=5)
    try:os.killpg(child.pid,0)
    except ProcessLookupError:absent=True
    else:absent=False
    result.update(exit_code=child.poll(),child_reaped=child.poll() is not None,owned_group_absent=absent,
                  elapsed_seconds=time.monotonic()-started)
    return result


def campaign_window(plan, bound, now_ms):
    """A pre-start CPU repair carries the first deadline; it cannot renew it."""
    if 'qualification_continuation' in plan:
        require('cpu_startup_repair' not in plan,'one explicit continuation category required')
        observed=continuation.validate(plan['qualification_continuation'],bound,now_ms)
        return observed['started_unix_ms'],observed['deadline_unix_ms']
    repair=plan.get('cpu_startup_repair')
    if repair is None:
        return now_ms,now_ms+14400_000
    require(set(repair)=={'grant','result','cancellation','client_receipt'},'closed CPU startup repair evidence required')
    grant,result,cancel,client=(controller.read(repair[key]) for key in
                              ('grant','result','cancellation','client_receipt'))
    previous=Path(repair['result']['path']).parent
    require(Path(repair['grant']['path'])==previous/'CAMPAIGN-GRANT.json'
            and Path(repair['client_receipt']['path'])==previous/'native-qualification/RECEIPT.json'
            and Path(repair['cancellation']['path']).is_relative_to(previous/'transport'),
            'startup repair evidence belongs to different attempts')
    require(result.get('status')=='FAILED' and result.get('controller_reaped') is True
            and cancel.get('status')=='NOT_STARTED'
            and client.get('child_reaped') is True and client.get('owned_group_absent') is True,
            'only an unstarted native client with reaped CPU processes can be repaired')
    require(not (previous/'controller/CONTROLLER-STARTED.json').exists()
            and not list((previous/'controller/jobs').glob('*')),
            'an initialized controller or submitted owner job cannot use CPU startup repair')
    require(all(grant.get(key)==bound[key] for key in
                ('selection_sha256','runtime_sha256','workload_sha256','selected_source_set_sha256')),
            'startup repair changed the selected workload')
    start,end=grant['started_unix_ms'],grant['deadline_unix_ms']
    require(type(start)is int and type(end)is int and end-start==14400_000 and start<=now_ms<end
            and result.get('campaign_started_unix_ms')==start and result.get('deadline_unix_ms')==end,
            'startup repair must preserve the original unexpired four-hour window')
    return start,end


class Campaign:
    def __init__(self, plan_path, plan_sha):
        self.plan_ref = dict(path=str(plan_path), sha256=plan_sha)
        self.plan = controller.read(self.plan_ref)
        p = self.plan
        require(p.get('schema') == 1 and p.get('campaign_seconds') == 14400
                and p.get('automatic_retry') is False and p.get('adoption_authorized') is False,
                'one four-hour campaign without retry or adoption required')
        self.root = op._canonical_dir(p['root'], 'campaign retention')
        self.native = Path(p['native_root'])
        require(self.native.parent == Path('/home/developer')
                and re.fullmatch('hyperloom-owner-campaign-[0-9a-f]{16}',self.native.name), 'native private root')
        self.controller_sha = controller.source_closure(p['controller_sources'])
        require(p['binding']['controller_sha256'] == self.controller_sha, 'campaign controller identity changed')
        require(set(p['supervisor_sources']) == {'campaign.py','qualification.py','optimizer_runtime.py','continuation.py'},
                'closed campaign supervisor sources required')
        for name, pin in p['supervisor_sources'].items():
            checked_bytes(HERE / name, pin, 1 << 20)
        checked_bytes(Path(p['connection']['path']), exchange_ssh.CONNECTION_SHA, 1 << 20)
        self.connection = exchange_ssh.load(Path(p['connection']['path']), '_campaign_connection')
        self.connection.check()
        checked_bytes(Path(p['commands']['path']), exchange_ssh.COMMANDS_SHA, 1 << 20)
        self.sequence = len(list((self.root / 'transport').glob('*'))) if (self.root / 'transport').exists() else 0
        (self.root / 'transport').mkdir(mode=0o700, exist_ok=True)
        self.child = None
        self.started = self.deadline = None

    def call(self, name, source, seconds=45):
        self.sequence += 1
        target = self.root / 'transport' / ('%04d-%s' % (self.sequence, name))
        target.mkdir(mode=0o700)
        runner = op.commands(self.plan['commands']['path'], target)
        (target / 'RECIPE.py').write_bytes(source)
        receipt = runner.run(name, self.connection.SSH + ['/usr/bin/python3 -I -B -S -'], seconds,
                             stdin=source, cwd=target, output_limit=16 << 20)
        return json.loads(checked_bytes(target / (name + '.stdout'), receipt['stdout']['sha256'], 16 << 20))

    def transfer(self, name, files, *, fresh=False):
        return self.call(name, remote_recipe(self.native, 'write', fresh=fresh, files=encode_files(files)))

    def collect(self, *, seconds=45):
        value=self.call('collect-results',remote_recipe(self.native,'collect'),seconds=seconds)
        packed=base64.b64decode(value['files_zlib_base64'],validate=True)
        require(len(packed)<=12<<20, 'bounded campaign artifact archive required')
        decoder=zlib.decompressobj()
        raw=decoder.decompress(packed,90<<20)
        require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
                and len(raw)==value['raw_bytes'] and exchange.digest(raw)==value['raw_sha256'],
                'complete campaign artifact archive required')
        files=json.loads(raw)
        target=self.root/'optimizer-artifacts';target.mkdir(mode=0o700)
        for name,row in files.items():
            part=PurePosixPath(name)
            require(not part.is_absolute() and '..' not in part.parts and part.parts[0] in {'work','candidate'},
                    'artifact outside the private campaign')
            content=base64.b64decode(row['data'],validate=True)
            require(exchange.digest(content)==row['sha256'], 'artifact digest differs')
            path=target/part;path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            with path.open('xb') as stream:stream.write(content)
        controller.write(target/'INVENTORY.json',dict(inventory=value['inventory'],raw_sha256=value['raw_sha256']))
        return dict(root=str(target),members=len(files),archive_sha256=value['raw_sha256'])

    def stage(self):
        require(not (self.root / 'STAGED.json').exists() and not (self.root / 'CAMPAIGN-GRANT.json').exists(),
                'staging and execution are single-use')
        files = {}
        for name, reference in self.plan['stage_files'].items():
            files[name] = checked_bytes(Path(reference['path']), reference['sha256'], 32 << 20)
        result = self.transfer('stage', files, fresh=True)
        controller.write(self.root / 'STAGED.json', dict(result, plan=self.plan_ref,
                                                       source_count=len(files), campaign_clock_started=False))
        return result

    def runtime_settings(self, mode, binding_ref, *, extra_pins=None):
        pins = {name: row['sha256'] for name, row in self.plan['stage_files'].items()
                if not name.startswith('candidate/')}
        pins.update(extra_pins or {})
        settings=dict(schema=1, mode=mode, root=str(self.native), started_unix_ms=self.started,
            deadline_unix_ms=self.deadline, binding=binding_ref, source_pins=pins,
            environment=self.plan['environment'], model='gpt-6.1-sol', argv=self.plan['optimizer_argv'],
            automatic_retry=False, adoption_authorized=False)
        if mode=='qualification' and 'qualification_continuation' in self.plan:
            name='inputs/completed-component.json'
            settings['completed_component']=dict(path=str(self.native/name),sha256=pins[name])
        return settings

    def launch(self, mode, settings):
        local = self.root / ('runtime-' + mode + '.json')
        reference = controller.write(local, settings)
        name = 'inputs/runtime-' + mode + '.json'
        self.transfer('configure-' + mode, {name: local.read_bytes()})
        config = self.native / name
        seconds = int(self.deadline / 1000 - time.time() - 45)
        require(seconds > 0, 'campaign deadline exhausted before native launch')
        # Arguments are fixed absolute paths and lowercase SHA256; no user shell text.
        command = 'exec /usr/bin/python3 -I -B -S ' + str(self.native / 'optimizer_runtime.py')
        command += ' launch ' + str(config) + ' --sha256 ' + reference['sha256']
        target = self.root / ('native-' + mode)
        target.mkdir(mode=0o700)
        with (target/'stdout').open('xb') as output,(target/'stderr').open('xb') as error:
            receipt=run_client(self.connection.SSH+[command],controller_process=self.child,
                deadline=time.time()+seconds,
                cancel=lambda:self.call('cancel-'+mode,remote_recipe(self.native,'cancel',mode=mode),seconds=20),
                output=output,error=error)
        receipt.update(settings=reference,mode=mode)
        controller.write(target/'RECEIPT.json',receipt)
        require(receipt['exit_code']==0 and receipt['failure'] is None and receipt['child_reaped']
                and receipt['owned_group_absent'], 'native CPU client failed: '+str(receipt))
        return receipt

    def activate(self):
        staged = op._json(self.root / 'STAGED.json')
        require(staged['plan'] == self.plan_ref and staged['status'] == 'STAGED', 'matching completed stage required')
        tests = controller.read(self.plan['cpu_proof'])
        require(tests.get('status') == 'PASS' and tests.get('controller_sha256') == self.controller_sha,
                'current passing CPU evidence required before campaign activation')
        require(tests.get('supervisor_sources') == self.plan['supervisor_sources']
                and tests.get('sdk_build') == self.plan['sdk_build'], 'CPU proof belongs to another runtime build')
        require(op._pin(op.R2 / 'CANDIDATE-POINTER.json')['sha256'] == self.plan['binding']['selection_sha256'],
                'selected serving pointer changed')
        op._idle(op._checkpoint(self.plan['checkpoint_root'])['state'])
        self.started,self.deadline=campaign_window(self.plan,self.plan['binding'],int(time.time()*1000))
        bound = dict(self.plan['binding'], campaign_deadline_unix_ms=self.deadline)
        local_binding = controller.write(self.root / 'BINDING.json', bound)
        native_binding = dict(path=str(self.native / 'inputs/binding.json'), sha256=local_binding['sha256'])
        grant = dict(schema=1, kind='hyperloom-sglang-campaign', execution_authorized=True,
            campaign_seconds=14400, objective='balanced-c1-c2-no-regressions', automatic_retry=False,
            adoption_authorized=False, existing_observer_only=True, watts_per_card=235,
            **{key:bound[key] for key in controller.IDENTITIES}, started_unix_ms=self.started,
            deadline_unix_ms=self.deadline, owner='workstation owner',
            user_instruction=self.plan['user_instruction'], allowed_kinds=['serving','profile','component'],
            source_instruction='Retained user instruction and preferences; not a reuse of a prior one-shot approval.')
        grant_ref = controller.write(self.root / 'CAMPAIGN-GRANT.json', grant)
        if 'cpu_startup_repair' in self.plan:
            controller.write(self.root/'CPU-STARTUP-REPAIR.json',dict(previous=self.plan['cpu_startup_repair'],
                started_unix_ms=self.started,deadline_unix_ms=self.deadline,deadline_extended=False,
                prior_controller_started=False,prior_native_client_started=False,prior_owner_jobs=0))
        if 'qualification_continuation' in self.plan:
            controller.write(self.root/'QUALIFICATION-CONTINUATION.json',dict(
                evidence=self.plan['qualification_continuation'],started_unix_ms=self.started,
                deadline_unix_ms=self.deadline,deadline_extended=False,repeated_gpu_jobs=0))
        self.transfer('binding', {'inputs/binding.json': (self.root / 'BINDING.json').read_bytes()})
        rpc = dict(module_pins=self.plan['rpc_modules'], boot_id=runtime.BOOT, native_binding=native_binding,
                   connection=self.plan['connection']['path'], local_commands=self.plan['commands']['path'])
        controller_root = self.root / 'controller'
        controller_root.mkdir(mode=0o700)
        configuration = dict(schema=1, binding=local_binding, campaign_grant=grant_ref,
            source_pins=self.plan['controller_sources'], root=str(controller_root), rpc=rpc,
            inputs=self.plan['owner_inputs'], checkpoint_root=self.plan['checkpoint_root'], run_parent=str(op.MX))
        config_ref = controller.write(self.root / 'CONTROLLER.json', configuration)
        with (self.root / 'controller.stdout').open('xb') as output, (self.root / 'controller.stderr').open('xb') as error:
            self.child = subprocess.Popen([op.PYTHON, '-I', '-B', str(HERE / 'controller.py'),
                '--config', config_ref['path'], '--config-sha256', config_ref['sha256']],
                stdin=subprocess.DEVNULL, stdout=output, stderr=error, start_new_session=True)
        return bound, native_binding

    def run(self):
        require(not (self.root/'CAMPAIGN-RESULT.json').exists()
                and not (self.root/'CAMPAIGN-GRANT.json').exists(), 'campaign execution is single-use')
        result = dict(schema=1, status='FAILED', adoption_authorized=False, automatic_retry=False)
        try:
            bound, native_binding = self.activate()
            result['qualification_attempted'] = True
            try:
                self.launch('qualification', self.runtime_settings('qualification', native_binding))
            except Exception:
                # A failed owner can stop qualification before its normal read.
                # Preserve the original failure and collect once within the
                # remaining deadline; collection cannot qualify or retry it.
                try:
                    seconds = min(20, int(self.deadline / 1000 - time.time() - 15))
                    require(seconds > 0, 'no time for qualification diagnostics before closeout')
                    observed = self.call('read-qualification-failure', remote_recipe(self.native, 'read', names=[
                        'qualification-inside.json', 'qualification-runtime-result.json']), seconds=seconds)
                    result['qualification_failure'] = controller.write(
                        self.root / 'NATIVE-QUALIFICATION-FAILURE.json', observed)
                except Exception as error:
                    result['qualification_retention_error'] = op._error(error)
                raise
            observed = self.call('read-qualification', remote_recipe(self.native, 'read', names=[
                'qualification-inside.json','qualification-runtime-result.json']))['files']
            controller.write(self.root / 'NATIVE-QUALIFICATION.json', observed)
            destination = self.root / 'qualification'
            proof = qualification.prepare(self.root / 'controller', bound, destination,
                native_root=self.native, runtime_observation=observed['qualification-inside.json'],
                runtime_result=observed['qualification-runtime-result.json'], cpu_proof=self.plan['cpu_proof'],
                evidence_bundle=self.plan['evidence_bundle'],
                continued=self.plan.get('qualification_continuation'))
            files = {'inputs/qualification/' + p.relative_to(destination).as_posix(): p.read_bytes()
                     for p in destination.rglob('*') if p.is_file() and 'profile-validation' not in p.parts}
            proof_name = 'inputs/qualification/QUALIFICATION.json'
            qualified = dict(bound, qualification=dict(path=str(self.native / proof_name),
                                                       sha256=exchange.digest(files[proof_name])))
            files['inputs/qualified-binding.json'] = exchange.canonical(qualified)
            qualified_binding = dict(path=str(self.native / 'inputs/qualified-binding.json'),
                                     sha256=exchange.digest(files['inputs/qualified-binding.json']))
            self.transfer('qualified-inputs', files)
            pins = {name:exchange.digest(raw) for name,raw in files.items()}
            result.update(optimizer_attempted=True,native_artifacts_root=str(self.native))
            try:
                self.launch('optimizer', self.runtime_settings('optimizer', qualified_binding, extra_pins=pins))
            finally:
                # A controller park can cancel the client before normal CLOSE.
                # Retain each available output once without extending the deadline.
                result['retention_errors']={}
                try:
                    seconds=min(20,int(self.deadline/1000-time.time()-15))
                    require(seconds>0,'no time for runtime-result collection before closeout')
                    observed=self.call('read-optimizer',remote_recipe(self.native,'read',names=[
                        'optimizer-runtime-result.json','optimizer-effective-budget.json']),seconds=seconds)['files']
                    controller.write(self.root/'NATIVE-OPTIMIZER.json',observed)
                    result['optimizer']=observed
                except Exception as error:
                    result['retention_errors']['runtime_result']=op._error(error)
                try:
                    seconds=min(20,int(self.deadline/1000-time.time()-15))
                    require(seconds>0,'no time for artifact collection before closeout')
                    result['artifacts']=self.collect(seconds=seconds)
                except Exception as error:
                    result['retention_errors']['artifacts']=op._error(error)
            require(not result['retention_errors'] and result['artifacts']['members']>1,
                    'optimizer outputs were not completely retained')
            result.update(status='COMPLETE',
                          qualification=dict(path=str(destination / 'QUALIFICATION.json')))
        except Exception as error:
            result['reason'] = op._error(error)
        finally:
            if self.child is not None:
                stop = self.root / 'controller/STOP.json'
                if not stop.exists():controller.write(stop, dict(reason='campaign client finished; close after any admitted owner'))
                # An admitted GPU owner keeps its original finite lifetime.
                # Never kill the controller to force a release claim.
                while self.child.poll() is None and time.time() < self.deadline / 1000:
                    time.sleep(.5)
                result['controller_exit_code'] = self.child.poll()
                result['controller_reaped'] = self.child.poll() is not None
            result.update(campaign_started_unix_ms=self.started,deadline_unix_ms=self.deadline,
                          elapsed_seconds=None if self.started is None else (time.time()*1000-self.started)/1000)
            try:
                result['final_checkpoint'] = op._checkpoint(self.plan['checkpoint_root'])
                op._idle(result['final_checkpoint']['state'])
                if self.child is not None:
                    require(result['controller_reaped'] and result['controller_exit_code']==0,
                            'controller did not finish naturally')
            except Exception as error:
                result.update(status='FAILED',closeout_error=op._error(error))
            controller.write(self.root / 'CAMPAIGN-RESULT.json', result)
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('stage','run'))
    parser.add_argument('--plan',required=True,type=Path)
    parser.add_argument('--sha256',required=True)
    args=parser.parse_args()
    campaign=Campaign(args.plan,args.sha256)
    result=campaign.stage() if args.action=='stage' else campaign.run()
    print(json.dumps(result,sort_keys=True),flush=True)
    return 0 if result['status'] in {'STAGED','COMPLETE'} else 1


if __name__=='__main__':
    raise SystemExit(main())
