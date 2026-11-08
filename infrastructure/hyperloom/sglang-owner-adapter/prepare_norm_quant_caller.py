"""Prepare the fixed one-use caller and unregistered proposal, without admission."""
import argparse
import difflib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_norm_quant_component as builder
import component_operation
import owner_operation as op
from hyperloom_exchange import digest
from hyperloom_sglang import checked_bytes, require


def prepare(root):
    root = op._canonical_dir(root, 'verification owner packet')
    material = builder.read(root / 'COMPONENT-PACKET.json')
    require(material['mode'] == builder.MODE and material['gpu_work'] is material['registration'] is False,
            'fixed inert component packet')
    context = json.loads(checked_bytes(Path(material['context']['path']), material['context']['sha256']))
    checked_bytes(Path(material['package']['path']), material['package']['sha256'])
    require(context['package_sha256'] == material['package']['sha256'], 'context/package identity')
    nonce = context['package']['remote_root'].rsplit('.', 1)[1]
    campaign = builder.N / ('norm-quant-campaign-' + nonce)
    native = op.R2.parent / ('norm-quant-run-' + nonce)
    require(not campaign.exists() and not native.exists(), 'fresh campaign and future run roots')
    seals = {name: op.sealer().seal(root / name) for name in ('source', 'native-component', 'owner')}
    readiness = dict(schema=1, status='COORDINATOR_REAL_AB_COMPONENT_READY',
        context_sha256=material['context']['sha256'], package_sha256=material['package']['sha256'],
        review_deferred_by_owner=True, component_only=True, capture_provenance=builder.PROVENANCE,
        native_source_checks_passed=True, model_starts=0, seals=seals, schedule=context['schedule'],
        component=context['package']['component'], gpu_execution=False, registration=False,
        independent_review_complete=False, qualification=False, selected=False, profiling=False)
    builder.write(root / 'READINESS.json', readiness)
    review = op._pin(root / 'READINESS.json')
    campaign.mkdir(mode=0o700)
    proposal = dict(schema=1, status='OWNER_AUTHORISED_REAL_AB_COMPONENT_EXTERNAL_ENVELOPE_AND_ORDINARY_ENTRY',
        proposal_only=True, registered=False, global_attempt_index=None,
        registration_policy='NEXT_FREE_INDEX_ONLY_AFTER_INDEPENDENT_RELEASE',
        maximum_invocations=1, retry=False, model_starts=0, http_requests=0, component_only=True,
        review_deferred_by_owner=True, promotion_authorized=False, qualification=False,
        capture_provenance=builder.PROVENANCE, component=context['package']['component'],
        context_sha256=material['context']['sha256'], readiness_sha256=review['sha256'],
        package_sha256=material['package']['sha256'], owner_start_contract='PRIOR_TARGET_PROBE_ABSOLUTE_BOUND_BEFORE_ADMISSION',
        schedule=context['schedule'], owner_packet_seal=seals['owner'], seals=seals,
        remote_root=context['package']['remote_root'], protected_root=context['package']['protected_root'],
        local_receipt_root=str(native), owner_seconds=1500, caller_seconds=2190, worker_seconds=600,
        card_seconds=280, prior_model_scope_owner_seconds_charged=None, prior_model_scope_caller_seconds_charged=None,
        candidate_sha256=material['candidate_sha256'], baseline_sha256=material['baseline_sha256'], adoption_authorized=False)
    builder.write(campaign / 'REGISTRATION-PROPOSED.json', proposal)
    donor = op.R2 / 'p1-gdn-layout-campaign-d18eb1b7274ad61b/execute_once.py'
    original = checked_bytes(donor, component_operation.CALLER_DONOR_SHA).decode()
    lines = original.splitlines(keepends=True)

    def swap(prefix, text):
        indexes = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        require(len(indexes) == 1, 'caller derivation anchor: ' + prefix)
        lines[indexes[0]] = text + '\n'

    swap('"""Inert one-use caller', '"""Inert one-use norm-quantization caller; exact owner approval is required.')
    swap('g=json.loads(', "assert sys.argv[1:]==['--registration-sha256',hashlib.sha256(grant.read_bytes()).hexdigest()]\n"
         "g=json.loads(grant.read_bytes());assert g.get('registered') is True and g.get('proposal_only') is not True,'registered owner grant required'\n"
         "assert type(g.get('global_attempt_index')) is int and g['global_attempt_index']>0,'registered global index required'")
    swap('expected_seals=', 'expected_seals=' + repr(seals))
    swap('sys.path.insert(0,', 'sys.path.insert(0,' + repr(str(root / 'owner/driver')) + ')')
    swap('result=operator_driver.execute_once(', 'result=operator_driver.execute_once(' +
         ','.join(map(repr, (material['context']['path'], material['context']['sha256'], review['path'], review['sha256']))) +
         ',str(grant),hashlib.sha256(grant.read_bytes()).hexdigest(),' + repr(str(native)) + ')')
    caller = ''.join(lines)
    compile(caller, 'norm-quant-one-use-caller', 'exec', dont_inherit=True)
    builder.put(campaign / 'execute_once.py', caller.encode())
    builder.put(campaign / 'CALLER-DERIVATION.diff', ''.join(difflib.unified_diff(original.splitlines(True), caller.splitlines(True), str(donor), 'execute_once.py')).encode())
    value = dict(schema=1, status='PREPARED_UNREGISTERED_REVIEW_PENDING', campaign_root=str(campaign),
        native_root=str(native), nonce=nonce, context=material['context'], package=material['package'], readiness=review,
        caller=op._pin(campaign / 'execute_once.py'), proposal=op._pin(campaign / 'REGISTRATION-PROPOSED.json'),
        seals=seals, registration=False, gpu_work=False, independent_review_complete=False,
        next='Independent review, exact owner decision, then existing global registration after independent release; no automatic execution')
    builder.write(root / 'CALLER-MATERIALIZATION.json', value)
    for path in campaign.iterdir(): builder.put(root / 'campaign-snapshot' / path.name, path.read_bytes())
    return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    print(json.dumps(prepare(parser.parse_args().root), sort_keys=True))
