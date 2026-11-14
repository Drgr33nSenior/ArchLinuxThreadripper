"""Image probe for the pinned upstream pod builder/reaper, with a fake Kubernetes API.

Run this module inside the built image; call probe(rendered_policies), the policies.json
of a render whose homes mapping has a user named "alice". No cluster calls.
"""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


async def probe(policies):
    home, shared = "home-alice", next(name for name in sorted(policies) if name.startswith("sandbox-"))
    policy = policies[home]
    from kubernetes_asyncio import client
    from terminals.backends.kubernetes import KubernetesBackend
    from terminals.backends import base
    api = client.ApiClient()
    core = SimpleNamespace(create_namespaced_secret=AsyncMock(), create_namespaced_service=AsyncMock(),
        create_namespaced_pod=AsyncMock(return_value=SimpleNamespace(metadata=SimpleNamespace(uid="fixture-pod"))),
        delete_namespaced_pod=AsyncMock(), delete_namespaced_service=AsyncMock(), delete_namespaced_secret=AsyncMock())
    backend = KubernetesBackend()
    backend._ensure_client = AsyncMock(return_value=api)
    backend._wait_until_pod_ready = AsyncMock()
    env_lists = {name.removeprefix("sandbox-") for name in policies if name.startswith("sandbox-")}
    try:
        with patch("terminals.backends.kubernetes.client.CoreV1Api", return_value=core):
            await backend.provision("alice", home, spec=copy.deepcopy(policy))
            await backend.provision("bob", shared, spec=copy.deepcopy(policies[shared]))
            pods = [c.args[1] for c in core.create_namespaced_pod.call_args_list]
            assert pods[0]["metadata"]["name"] != pods[1]["metadata"]["name"]
            for pod in pods:
                spec = pod["spec"]
                assert spec["automountServiceAccountToken"] is False
                assert spec["enableServiceLinks"] is False
                assert pod["metadata"]["labels"]["app.kubernetes.io/name"] == "assistant-workspace"
                container = spec["containers"][0]
                assert container["command"] == ["python", "/opt/sandbox/terminal.py"]
                assert container["resources"]["requests"] == container["resources"]["limits"]
                assert container["resources"]["limits"]["ephemeral-storage"] == "2Gi"
                assert container["securityContext"]["readOnlyRootFilesystem"]
                assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
                assert "resourceClaims" not in spec
                mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}
                assert mounts == {"/home/user": "home", "/tmp": "scratch"}, mounts
                env = {e["name"]: e.get("value") for e in container["env"]}
                assert env["HTTPS_PROXY"] == env["http_proxy"] and env["HTTPS_PROXY"].startswith("http://")
                assert pod["metadata"]["labels"]["workstation.ai/sandbox-egress"] in env_lists
            volumes = [{v["name"]: v for v in pod["spec"]["volumes"]} for pod in pods]
            # The mapped account's home is its own claim; the other account's is disposable.
            assert volumes[0]["home"]["persistentVolumeClaim"] == {"claimName": "sandbox-home-alice"}
            assert "emptyDir" in volumes[1]["home"] and all("emptyDir" in v["scratch"] for v in volumes)
            key = backend._key("alice", home, "default")
            backend._instances[key] = {"instance_id": "fixture-pod", "started_at": time.time()}
            backend._specs[key] = policy
            backend._activity[key] = time.monotonic() - 901
            backend._apply_due_reset = AsyncMock()
            with patch.object(base, "reset_due_for", AsyncMock(return_value=False)), \
                    patch.object(base, "terminal_last_active_at", AsyncMock(return_value=None)):
                await backend._reap_idle()
            assert key not in backend._instances
            core.delete_namespaced_pod.assert_awaited_once()
            core.delete_namespaced_service.assert_awaited_once()
            core.delete_namespaced_secret.assert_awaited_once()
        print(json.dumps({"native_pod_builder": "passed", "per_user_names": "distinct",
                          "per_user_home_claim": "mounted", "scratch": "separate emptyDir",
                          "security_and_resources": "preserved", "native_idle_reaper": "passed"}))
    finally:
        await api.close()


def probe_control(config, policies):
    """Exercise real DB migrations/policy seeding while replacing only cluster I/O."""
    import importlib.util
    from pathlib import Path
    from unittest.mock import Mock
    spec = importlib.util.spec_from_file_location("control", "/opt/sandbox/control.py")
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        if str(path) == "/config/config.json":
            return json.dumps(config)
        if str(path) == "/config/policies.json":
            return json.dumps(policies)
        return original_read(path, *args, **kwargs)
    backend = SimpleNamespace(reconcile=AsyncMock(), start_reaper=Mock(),
                              stop_reaper=AsyncMock(), close=AsyncMock())
    async def startup(gate):
        with patch("terminals.main.create_backend", return_value=backend):
            async with gate.app.router.lifespan_context(gate.app):
                from terminals.db.session import async_session
                from terminals.models.policy import Policy
                async with async_session() as session:
                    for name, policy in policies.items():
                        assert (await session.get(Policy, name)).data == policy
                    assert await session.get(Policy, "sandbox") is None
                backend.start_reaper.assert_called_once()
        backend.stop_reaper.assert_awaited_once()
        print(json.dumps({"controller_migrations_and_policy_startup": "passed"}))
    with patch.object(Path, "read_text", read), patch("uvicorn.run", lambda gate, **kwargs: asyncio.run(startup(gate))):
        control.main()
