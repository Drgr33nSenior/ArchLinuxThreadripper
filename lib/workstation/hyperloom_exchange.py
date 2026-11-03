"""Bounded job exchange between Hyperloom and the existing owner controller.

The optimizer side has no GPU access. The controller consumes one request,
invokes the separately authenticated owner, and publishes a result after its
independent release. This module never launches an owner or grants authority.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from hyperloom_sglang import decoded_json, require


MAX_JSON = 2 << 20
MAX_RESULT = 16 << 20
MAX_SOURCE = 4 << 20
MAX_SOURCE_TOTAL = 32 << 20
ADMISSION_SECONDS = 60
COMPONENT_ADMISSION_SECONDS = 600
KINDS = frozenset({"serving", "profile", "component"})
# Serving answers are decoded and graded only after independent GPU release.
# Reserve that established finite CPU envelope before admitting the GPU owner.
GRADING_SECONDS = 320
REPLAY_AND_REPORT_SECONDS = 120
POST_RELEASE_SECONDS = {"serving": GRADING_SECONDS + REPLAY_AND_REPORT_SECONDS,
                        "profile": GRADING_SECONDS + REPLAY_AND_REPORT_SECONDS, "component": 120}
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
JOB_ID = re.compile(r"[0-9a-f]{32}\Z")


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def envelope_seconds(kind, caller_seconds):
    require(kind in KINDS and type(caller_seconds) is int and 0 < caller_seconds <= 7160,
            "finite recognized owner envelope required")
    admission = COMPONENT_ADMISSION_SECONDS if kind == "component" else ADMISSION_SECONDS
    return admission + caller_seconds + POST_RELEASE_SECONDS[kind]


def _directory(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and path.is_dir()
            and not path.is_symlink(), "canonical exchange directory required")
    return path


def _read(path, cap=MAX_JSON):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, "canonical exchange file required")
    # Walk directory descriptors without following links. Candidate files are
    # mutable; a resolve-then-open check alone has a link-swap race.
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= cap, "bounded regular exchange file required")
            raw = stream.read(cap + 1)
    except OSError as error:
        raise ValueError("unreadable or linked exchange file") from error
    finally:
        os.close(directory)
    require(len(raw) <= cap, "exchange file exceeds cap")
    return raw


def _publish(path, raw):
    """Publish complete bytes once, without overwriting a response or claim."""
    require(len(raw) <= MAX_RESULT and not path.exists() and not path.is_symlink(),
            "fresh bounded exchange member required")
    temporary = path.parent / (".pending-" + secrets.token_hex(16))
    with temporary.open("xb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def _identity(value, name):
    require(isinstance(value, str) and SHA.fullmatch(value), "invalid " + name)


def _request(value):
    require(isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] == 1,
            "job schema")
    require(value.get("kind") in KINDS and isinstance(value.get("id"), str)
            and JOB_ID.fullmatch(value["id"]), "job kind/identity")
    for name in ("campaign_sha256", "selection_sha256", "config_sha256", "source_set_sha256"):
        _identity(value.get(name), name)
    for name in ("created_unix_ms", "expires_unix_ms", "caller_seconds"):
        require(type(value.get(name)) is int and value[name] > 0, "finite job clock required")
    require(type(value.get("post_release_seconds")) is int
            and value["post_release_seconds"] == POST_RELEASE_SECONDS[value["kind"]],
            "post-release grading reserve changed")
    require(value["caller_seconds"] <= 7160 and value["created_unix_ms"] < value["expires_unix_ms"]
            and value["expires_unix_ms"] - value["created_unix_ms"]
            == envelope_seconds(value["kind"], value["caller_seconds"]) * 1000,
            "job exceeds caller envelope")
    sources = value.get("sources")
    require(isinstance(sources, list) and 0 < len(sources) <= 64, "bounded source set required")
    names, total = set(), 0
    for source in sources:
        require(isinstance(source, dict) and isinstance(source.get("name"), str)
                and NAME.fullmatch(source["name"]) and source["name"] not in names,
                "unique source basename required")
        _identity(source.get("sha256"), "source digest")
        require(type(source.get("bytes")) is int and 0 < source["bytes"] <= MAX_SOURCE,
                "source size")
        names.add(source["name"])
        total += source["bytes"]
    require(total <= MAX_SOURCE_TOTAL and digest(canonical(sources)) == value["source_set_sha256"],
            "source set identity/size")
    require(value.get("automatic_retry") is False and value.get("adoption_authorized") is False,
            "job cannot retry or adopt")
    return value


def submit(exchange, *, campaign_sha256, selection_sha256, kind, config, sources,
           caller_seconds, campaign_deadline_unix_ms, now=None, expected_source_set=None):
    """Publish a complete request; caller supplies the declared job envelope."""
    exchange = _directory(exchange)
    for name, value in (("campaign", campaign_sha256), ("selection", selection_sha256)):
        _identity(value, name)
    require(type(caller_seconds) is int and 0 < caller_seconds <= 7160
            and type(campaign_deadline_unix_ms) is int, "finite campaign/job deadline required")
    created = int((time.time() if now is None else now) * 1000)
    total_seconds = envelope_seconds(kind, caller_seconds)
    require(created + total_seconds * 1000 <= campaign_deadline_unix_ms,
            "full caller envelope does not fit campaign")
    require(isinstance(config, dict), "job configuration must be an object")
    config_raw = canonical(config)
    require(len(config_raw) <= MAX_JSON and isinstance(sources, dict) and sources,
            "bounded configuration/sources required")
    source_raw, inventory = {}, []
    for name, path in sorted(sources.items()):
        require(isinstance(name, str) and NAME.fullmatch(name), "source must have a simple name")
        raw = _read(path, MAX_SOURCE)
        require(raw, "empty candidate source")
        source_raw[name] = raw
        inventory.append({"name": name, "sha256": digest(raw), "bytes": len(raw)})
    if expected_source_set is not None:
        require(digest(canonical(inventory)) == expected_source_set,
                "candidate changed while copying the screened source snapshot")
    request = _request({"schema": 1, "id": secrets.token_hex(16), "kind": kind,
        "campaign_sha256": campaign_sha256, "selection_sha256": selection_sha256,
        "config_sha256": digest(config_raw), "source_set_sha256": digest(canonical(inventory)),
        "created_unix_ms": created, "expires_unix_ms": created + total_seconds * 1000,
        "caller_seconds": caller_seconds, "post_release_seconds": POST_RELEASE_SECONDS[kind], "sources": inventory,
        "automatic_retry": False, "adoption_authorized": False})
    job = exchange / request["id"]
    job.mkdir(mode=0o700)
    (job / "sources").mkdir(mode=0o700)
    _publish(job / "CONFIG.json", config_raw)
    for name, raw in source_raw.items():
        _publish(job / "sources" / name, raw)
    # The controller ignores directories until this last atomic publication.
    _publish(job / "REQUEST.json", canonical(request))
    return job, request


def inspect(job, *, campaign_sha256, selection_sha256):
    """Authenticate the complete transmitted request without importing it."""
    job = _directory(job)
    raw = _read(job / "REQUEST.json")
    request = _request(decoded_json(raw))
    require(job.name == request["id"] and request["campaign_sha256"] == campaign_sha256
            and request["selection_sha256"] == selection_sha256, "wrong campaign, selection or job")
    config_raw = _read(job / "CONFIG.json")
    require(digest(config_raw) == request["config_sha256"], "job config changed")
    config = decoded_json(config_raw)
    require(isinstance(config, dict), "job config must be an object")
    source_dir = _directory(job / "sources")
    require({p.name for p in source_dir.iterdir()} == {r["name"] for r in request["sources"]},
            "unexpected source member")
    for row in request["sources"]:
        source = _read(source_dir / row["name"], MAX_SOURCE)
        require(len(source) == row["bytes"] and digest(source) == row["sha256"], "job source changed")
    return request, config


def claim(job, replies, *, campaign_sha256, selection_sha256, controller_sha256, now=None):
    """Reserve once for the existing controller; this is not GPU admission."""
    job = _directory(job)
    replies = _directory(replies)
    require(replies != job.parent and not replies.is_relative_to(job.parent)
            and not job.parent.is_relative_to(replies), "separate request/reply trees required")
    request, config = inspect(job, campaign_sha256=campaign_sha256, selection_sha256=selection_sha256)
    _identity(controller_sha256, "controller digest")
    current = int((time.time() if now is None else now) * 1000)
    require(current >= request["created_unix_ms"]
            and current + (request["caller_seconds"] + request["post_release_seconds"]) * 1000 <= request["expires_unix_ms"]
            and not (job / "CANCELLED.json").exists(), "job expired, clock moved or cancelled")
    reply = replies / request["id"]
    reply.mkdir(mode=0o700)
    _publish(reply / "CLAIM.json", canonical({"schema": 1, "id": request["id"],
        "request_sha256": digest(_read(job / "REQUEST.json")), "controller_sha256": controller_sha256,
        "claimed_unix_ms": current, "gpu_admission": False}))
    # Cancellation can race the claim. The controller also checks cancellation
    # immediately before its separately authenticated owner's invocation.
    require(not (job / "CANCELLED.json").exists(), "job cancelled while being claimed")
    return request, config, reply


def publish_result(reply, request, *, status, payload, owner_seal_sha256=None,
                   independent_release=False):
    """Publish a request-bound outcome after the controller's own validation."""
    reply = _directory(reply)
    require(status in {"SUCCEEDED", "FAILED", "UNSUPPORTED", "CANCELLED"}
            and isinstance(payload, dict), "job result status/payload")
    require(reply.name == request["id"] and (reply / "CLAIM.json").is_file(), "controller claim required")
    claim_record = decoded_json(_read(reply / "CLAIM.json"))
    require(claim_record.get("id") == request["id"]
            and claim_record.get("request_sha256") == digest(canonical(request)), "claim/request binding")
    if status == "SUCCEEDED":
        _identity(owner_seal_sha256, "owner result seal")
        require(independent_release is True, "successful job requires independent release")
    result = {"schema": 1, "id": request["id"], "status": status,
              "request_sha256": digest(canonical(request)), "campaign_sha256": request["campaign_sha256"],
              "config_sha256": request["config_sha256"], "source_set_sha256": request["source_set_sha256"],
              "controller_sha256": claim_record["controller_sha256"],
              "owner_seal_sha256": owner_seal_sha256, "independent_release": independent_release,
              "adoption_authorized": False, "automatic_retry": False, "payload": payload}
    _publish(reply / "RESULT.json", canonical(result))
    return result


def read_result(reply, request, *, controller_sha256):
    """Refuse another invocation's result, even when its timings look valid."""
    reply = _directory(reply)
    require(reply.name == request["id"], "wrong reply directory")
    result = decoded_json(_read(reply / "RESULT.json", MAX_RESULT))
    return validate_result(result, request, controller_sha256=controller_sha256)


def validate_result(result, request, *, controller_sha256):
    """Authenticate a retained result with the same predicate as a live reply."""
    require(isinstance(result, dict) and type(result.get("schema")) is int and result["schema"] == 1,
            "job result schema")
    require(all(result.get(k) == request[k] for k in
                ("id", "campaign_sha256", "config_sha256", "source_set_sha256"))
            and result.get("request_sha256") == digest(canonical(request))
            and result.get("controller_sha256") == controller_sha256,
            "result identity changed or belongs to another request")
    require(result.get("status") in {"SUCCEEDED", "FAILED", "UNSUPPORTED", "CANCELLED"}
            and result.get("automatic_retry") is False and result.get("adoption_authorized") is False
            and isinstance(result.get("payload"), dict), "job result contract")
    if result["status"] == "SUCCEEDED":
        _identity(result.get("owner_seal_sha256"), "result seal")
        require(result.get("independent_release") is True, "result missing independent release")
    return result


def _wait_state(job, replies, request):
    job = _directory(job)
    replies = _directory(replies)
    require(replies != job.parent and not replies.is_relative_to(job.parent)
            and not job.parent.is_relative_to(replies), "separate request/reply trees required")
    reply = replies / request["id"]
    remaining = (request["expires_unix_ms"] - time.time() * 1000) / 1000
    end = time.monotonic() + min(envelope_seconds(request["kind"], request["caller_seconds"]), max(0, remaining))
    return job, reply, end


def require_campaign_open(replies, *, campaign_sha256, controller_sha256):
    """Fail promptly on the controller's authenticated terminal disposition."""
    path = _directory(replies) / "CAMPAIGN-PARKED.json"
    if not path.exists():
        return
    value = decoded_json(_read(path))
    require(isinstance(value, dict) and value.get("schema") == 1
            and value.get("campaign_sha256") == campaign_sha256
            and value.get("controller_sha256") == controller_sha256
            and value.get("park_campaign") is True and value.get("automatic_retry") is False
            and value.get("adoption_authorized") is False
            and isinstance(value.get("reason"), str), "foreign or invalid campaign terminal record")
    raise RuntimeError("owner campaign parked: " + value["reason"])


def _cancel_wait(job, request, error):
    if not (job / "CANCELLED.json").exists():
        _publish(job / "CANCELLED.json", canonical({"id": request["id"],
            "request_sha256": digest(canonical(request)), "reason": type(error).__name__,
            "owner_release_claimed": False}))


def wait_result(job, replies, request, *, controller_sha256):
    """Wait only for this invocation; timeout/cancellation creates no retry."""
    job, reply, end = _wait_state(job, replies, request)
    try:
        while time.monotonic() < end:
            if (reply / "RESULT.json").exists():
                return read_result(reply, request, controller_sha256=controller_sha256)
            require_campaign_open(reply.parent, campaign_sha256=request["campaign_sha256"],
                                  controller_sha256=controller_sha256)
            time.sleep(min(0.2, max(0, end - time.monotonic())))
        raise TimeoutError("owner exchange deadline reached; no retry")
    except BaseException as error:
        _cancel_wait(job, request, error)
        raise


async def wait_result_async(job, replies, request, *, controller_sha256):
    """Cancellable async wait; no detached thread can outlive the task."""
    job, reply, end = _wait_state(job, replies, request)
    try:
        while time.monotonic() < end:
            if (reply / "RESULT.json").exists():
                return read_result(reply, request, controller_sha256=controller_sha256)
            require_campaign_open(reply.parent, campaign_sha256=request["campaign_sha256"],
                                  controller_sha256=controller_sha256)
            await asyncio.sleep(min(0.2, max(0, end - time.monotonic())))
        raise TimeoutError("owner exchange deadline reached; no retry")
    except BaseException as error:
        _cancel_wait(job, request, error)
        raise
