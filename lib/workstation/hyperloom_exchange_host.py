"""Trusted CPU-side exchange endpoint, called only by the external controller.

This file is excluded from the optimizer's writable tree. Requests are input;
replies are controller-owned and mounted read-only in the optimizer sandbox.
No action here launches GPU work or grants an owner allowance.
"""

import base64
from pathlib import Path
import time

import hyperloom_exchange as exchange
import hyperloom_owner_backend as backend
from hyperloom_sglang import decoded_json, require


MAX_QUEUED = 256


def _job(bound, job_id):
    require(isinstance(job_id, str) and exchange.JOB_ID.fullmatch(job_id), "invalid exchange job ID")
    return Path(bound["requests_root"]) / job_id


def dispatch(action, bound, *, job_id=None, result=None, now=None):
    """One bounded RPC; no persistent remote interpreter or executable input."""
    current = time.time() if now is None else now
    requests = exchange._directory(bound["requests_root"])
    replies = exchange._directory(bound["replies_root"])
    if action == "park":
        require(job_id is None and isinstance(result, dict) and set(result) == {"reason"}
                and isinstance(result["reason"], str) and 0 < len(result["reason"]) <= 2048,
                "bounded terminal campaign reason required")
        terminal = dict(schema=1, campaign_sha256=bound["campaign_sha256"],
            controller_sha256=bound["controller_sha256"], reason=result["reason"],
            park_campaign=True, automatic_retry=False, adoption_authorized=False,
            independent_release_claimed=False, unix_ms=int(current*1000))
        raw = exchange.canonical(terminal)
        exchange._publish(replies / "CAMPAIGN-PARKED.json", raw)
        return dict(schema=1, published_sha256=exchange.digest(raw), bytes=len(raw))
    if action == "list":
        require(job_id is None and result is None, "list takes no job payload")
        entries = []
        for path in requests.iterdir():
            require(len(entries) < MAX_QUEUED and exchange.JOB_ID.fullmatch(path.name)
                    and path.is_dir() and not path.is_symlink(), "bounded ordinary request directories required")
            entries.append(path)
        return {"schema": 1, "unix_ms": int(current * 1000), "jobs": sorted(
            p.name for p in entries if (p / "REQUEST.json").is_file() and not (replies / p.name).exists())}
    job = _job(bound, job_id)
    if action == "take":
        require(result is None, "take accepts no result")
        request, config = exchange.inspect(job, campaign_sha256=bound["campaign_sha256"],
                                           selection_sha256=bound["selection_sha256"])
        require(request["caller_seconds"] == bound["caller_seconds"][request["kind"]]
                and request["expires_unix_ms"] <= bound["campaign_deadline_unix_ms"],
                "request exceeds bound campaign or owner ceiling")
        require(backend.validate_config(config, bound) == request["kind"], "request/config kind mismatch")
        # Re-read and verify the exact bytes exported. A mutable source changing
        # after inspect must never enter the controller's immutable snapshot.
        sources = {}
        for row in request["sources"]:
            raw = exchange._read(job / "sources" / row["name"], exchange.MAX_SOURCE)
            require(len(raw) == row["bytes"] and exchange.digest(raw) == row["sha256"], "source changed during snapshot")
            sources[row["name"]] = base64.b64encode(raw).decode()
        claimed, config2, reply = exchange.claim(job, replies,
            campaign_sha256=bound["campaign_sha256"], selection_sha256=bound["selection_sha256"],
            controller_sha256=bound["controller_sha256"], now=current)
        require(claimed == request and config2 == config, "request changed during claim")
        return dict(schema=1, request=request, config=config, sources=sources,
                    claim=decoded_json(exchange._read(reply / "CLAIM.json")))
    if action == "cancelled":
        require(result is None, "cancellation read accepts no result")
        # Deleting or corrupting a submitted job is cancellation, never a reason
        # to launch from an old unobserved snapshot.
        try:
            request, _ = exchange.inspect(job, campaign_sha256=bound["campaign_sha256"],
                                          selection_sha256=bound["selection_sha256"])
            cancelled = ((job / "CANCELLED.json").exists() or current * 1000 >= request["expires_unix_ms"])
            request_sha = exchange.digest(exchange.canonical(request))
        except (OSError, ValueError):
            cancelled, request_sha = True, None
        return dict(schema=1, cancelled=cancelled, request_sha256=request_sha)
    if action == "publish":
        require(isinstance(result, dict), "controller result required")
        reply = exchange._directory(replies / job_id)
        claim = decoded_json(exchange._read(reply / "CLAIM.json"))
        # The request can have been cancelled or removed while its owner ran.
        # Bind to the controller's immutable claim rather than mutable inputs.
        require(result.get("id") == job_id and result.get("controller_sha256") == bound["controller_sha256"]
                and result.get("campaign_sha256") == bound["campaign_sha256"]
                and result.get("request_sha256") == claim["request_sha256"], "foreign result publication")
        require(result.get("status") in {"SUCCEEDED", "FAILED", "UNSUPPORTED", "CANCELLED"}
                and result.get("adoption_authorized") is False and result.get("automatic_retry") is False,
                "invalid controller disposition")
        if result["status"] == "SUCCEEDED":
            exchange._identity(result.get("owner_seal_sha256"), "completed owner seal")
            require(result.get("independent_release") is True, "unreleased owner cannot publish success")
        raw = exchange.canonical(result)
        exchange._publish(reply / "RESULT.json", raw)
        return dict(schema=1, published_sha256=exchange.digest(raw), bytes=len(raw))
    raise ValueError("unsupported exchange action")


def retain_snapshot(snapshot, destination, bound):
    """Verify a remote take response and create a private, closed local copy."""
    require(isinstance(snapshot, dict) and set(snapshot) == {"schema", "request", "config", "sources", "claim"}
            and snapshot["schema"] == 1, "snapshot schema")
    request = exchange._request(snapshot["request"])
    require(request["campaign_sha256"] == bound["campaign_sha256"]
            and request["selection_sha256"] == bound["selection_sha256"]
            and request["caller_seconds"] == bound["caller_seconds"][request["kind"]]
            and request["expires_unix_ms"] <= bound["campaign_deadline_unix_ms"], "snapshot campaign/ceiling mismatch")
    config = snapshot["config"]
    require(exchange.digest(exchange.canonical(config)) == request["config_sha256"]
            and backend.validate_config(config, bound) == request["kind"], "snapshot config mismatch")
    claim = snapshot["claim"]
    require(claim.get("id") == request["id"] and claim.get("request_sha256") == exchange.digest(exchange.canonical(request))
            and claim.get("controller_sha256") == bound["controller_sha256"] and claim.get("gpu_admission") is False,
            "snapshot claim mismatch")
    encoded = snapshot["sources"]
    require(isinstance(encoded, dict) and set(encoded) == {r["name"] for r in request["sources"]},
            "closed snapshot source set required")
    sources = {}
    for row in request["sources"]:
        value = encoded[row["name"]]
        require(isinstance(value, str) and len(value) <= 4 * ((exchange.MAX_SOURCE + 2) // 3), "snapshot source size")
        raw = base64.b64decode(value, validate=True)
        require(len(raw) == row["bytes"] and exchange.digest(raw) == row["sha256"], "snapshot source digest mismatch")
        sources[row["name"]] = raw
    destination = Path(destination)
    require(destination.is_absolute() and destination.parent.resolve() == destination.parent
            and destination.parent.is_dir() and not destination.exists() and not destination.is_symlink(),
            "fresh private snapshot destination required")
    destination.mkdir(mode=0o700)
    (destination / "sources").mkdir(mode=0o700)
    for name, raw in sources.items():
        exchange._publish(destination / "sources" / name, raw)
    for name, value in (("REQUEST.json", request), ("CONFIG.json", config), ("CLAIM.json", claim)):
        exchange._publish(destination / name, exchange.canonical(value))
    return destination
