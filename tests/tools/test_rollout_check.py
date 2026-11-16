"""tools/rollout/rollout_check.py: sizing and SGLang checks of a render against live Deployments."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/rollout/rollout_check.py"
sys.path.insert(0, str(TOOL.parent))
import rollout_check

IMAGE = "registry.example.invalid/sglang@sha256:" + "a" * 64


def deployment(name, memory, image="registry.example.invalid/app:1", env=None):
    resources = {"requests": {"cpu": "1", "memory": memory}, "limits": {"cpu": "2", "memory": memory}}
    container = {"name": name, "image": image, "resources": resources, "env": env or []}
    return {"kind": "Deployment", "metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [container]}}}}


SGLANG_ENV = [{"name": "SGLANG_GFX1201_ELASTIC_KV_BASE_TOKENS", "value": "201600"},
              {"name": "HIP_VISIBLE_DEVICES", "value": "0,1"}]
LIVE = [deployment("sglang", "60Gi", IMAGE, SGLANG_ENV), deployment("comfyui-gpu0", "8Gi"),
        deployment("open-webui", "4Gi")]


def render(**changes):
    docs = copy.deepcopy(LIVE)
    for doc in docs:
        container = doc["spec"]["template"]["spec"]["containers"][0]
        for key, value in changes.get(doc["metadata"]["name"], {}).items():
            if key == "memory":
                for section in ("requests", "limits"):
                    container["resources"][section]["memory"] = value
            else:
                container[key] = value
    return docs


class CheckTests(unittest.TestCase):
    def check(self, rendered, expected="", expected_env=()):
        return rollout_check.check(rendered, LIVE, IMAGE, rollout_check.parse_expected(expected), list(expected_env))

    def test_an_identical_render_passes(self):
        lines, ok = self.check(render())
        self.assertTrue(ok, lines)
        self.assertIn("resource check: unchanged", "\n".join(lines))

    def test_an_unreviewed_sizing_change_stops(self):
        lines, ok = self.check(render(**{"comfyui-gpu0": {"memory": "9Gi"}}))
        self.assertFalse(ok)
        self.assertIn("unexpected: ['comfyui-gpu0']", "\n".join(lines))

    def test_a_reviewed_sizing_change_passes_only_at_the_reviewed_value(self):
        rendered = render(**{"comfyui-gpu0": {"memory": "9Gi"}})
        self.assertTrue(self.check(rendered, "comfyui-gpu0=comfyui-gpu0:9Gi")[1])
        self.assertFalse(self.check(rendered, "comfyui-gpu0=comfyui-gpu0:10Gi")[1])
        self.assertFalse(self.check(rendered, "comfyui-gpu0=other:9Gi")[1])

    def test_a_reviewed_memory_change_does_not_cover_other_sizing(self):
        allow = "comfyui-gpu0=comfyui-gpu0:9Gi"
        # CPU changed together with the reviewed memory.
        rendered = render(**{"comfyui-gpu0": {"memory": "9Gi"}})
        container = rendered[1]["spec"]["template"]["spec"]["containers"][0]
        container["resources"]["limits"]["cpu"] = "24"
        self.assertFalse(self.check(rendered, allow)[1])
        # Another resource of the same container.
        rendered = render(**{"comfyui-gpu0": {"memory": "9Gi"}})
        rendered[1]["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["ephemeral-storage"] = "1Gi"
        self.assertFalse(self.check(rendered, allow)[1])

    def test_a_reviewed_memory_change_needs_request_and_limit(self):
        allow = "comfyui-gpu0=comfyui-gpu0:9Gi"
        for changed in ("requests", "limits"):
            rendered = render()
            rendered[1]["spec"]["template"]["spec"]["containers"][0]["resources"][changed]["memory"] = "9Gi"
            with self.subTest(changed=changed):
                self.assertFalse(self.check(rendered, allow)[1])

    def test_a_reviewed_change_does_not_cover_other_containers(self):
        sidecar = {"name": "sidecar", "image": "x", "resources": {"requests": {"cpu": "1", "memory": "1Gi"},
                                                                  "limits": {"cpu": "1", "memory": "1Gi"}}}
        init = {"name": "prepare", "image": "x", "resources": {"limits": {"cpu": "1", "memory": "1Gi"}}}
        live = copy.deepcopy(LIVE)
        live[1]["spec"]["template"]["spec"]["containers"].append(copy.deepcopy(sidecar))
        live[1]["spec"]["template"]["spec"]["initContainers"] = [copy.deepcopy(init)]
        allow = rollout_check.parse_expected("comfyui-gpu0=comfyui-gpu0:9Gi")

        def rendered_with(change):
            docs = copy.deepcopy(live)
            for section in ("requests", "limits"):
                docs[1]["spec"]["template"]["spec"]["containers"][0]["resources"][section]["memory"] = "9Gi"
            change(docs[1]["spec"]["template"]["spec"])
            return docs

        def check(docs):
            return rollout_check.check(docs, live, IMAGE, allow, [])[1]

        self.assertTrue(check(rendered_with(lambda spec: None)))
        self.assertFalse(check(rendered_with(
            lambda spec: spec["containers"][1]["resources"]["limits"].update(cpu="8"))))
        self.assertFalse(check(rendered_with(
            lambda spec: spec["initContainers"][0]["resources"]["limits"].update(memory="4Gi"))))
        # An init container change alone is a sizing change too.
        docs = copy.deepcopy(live)
        docs[1]["spec"]["template"]["spec"]["initContainers"][0]["resources"]["limits"]["cpu"] = "4"
        self.assertFalse(rollout_check.check(docs, live, IMAGE, {}, [])[1])

    def test_a_reviewed_change_the_render_does_not_make_stops(self):
        lines, ok = self.check(render(), "open-webui=open-webui:6Gi")
        self.assertFalse(ok)
        self.assertIn("expected but not changed: ['open-webui']", "\n".join(lines))

    def test_a_new_sglang_image_stops(self):
        self.assertFalse(self.check(render(sglang={"image": IMAGE.replace("a" * 64, "b" * 64)}))[1])

    def test_an_sglang_environment_change_needs_review(self):
        env = [{"name": "SGLANG_GFX1201_ELASTIC_KV_BASE_TOKENS", "value": "180000"}, SGLANG_ENV[1]]
        rendered = render(sglang={"env": env})
        lines, ok = self.check(rendered)
        self.assertFalse(ok)
        self.assertIn("  sglang env SGLANG_GFX1201_ELASTIC_KV_BASE_TOKENS: '201600' -> '180000'", lines)
        self.assertTrue(self.check(rendered, expected_env=["SGLANG_GFX1201_ELASTIC_KV_BASE_TOKENS"])[1])
        self.assertFalse(self.check(render(), expected_env=["SGLANG_GFX1201_ELASTIC_KV_BASE_TOKENS"])[1])
        added = render(sglang={"env": SGLANG_ENV + [{"name": "NEW", "valueFrom": {"secretKeyRef": {"name": "s"}}}]})
        self.assertFalse(self.check(added)[1])
        self.assertTrue(self.check(added, expected_env=["NEW"])[1])

    def test_api_server_env_defaults_are_not_changes(self):
        live = copy.deepcopy(LIVE)
        env = live[0]["spec"]["template"]["spec"]["containers"][0]["env"]
        env.append({"name": "POD_IP", "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "status.podIP"}}})
        env.append({"name": "CPUS", "valueFrom": {"resourceFieldRef": {"resource": "limits.cpu", "divisor": "0"}}})
        env.append({"name": "EMPTY"})
        rendered = copy.deepcopy(LIVE)
        env = rendered[0]["spec"]["template"]["spec"]["containers"][0]["env"]
        env.append({"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}})
        env.append({"name": "CPUS", "valueFrom": {"resourceFieldRef": {"resource": "limits.cpu"}}})
        env.append({"name": "EMPTY", "value": ""})
        lines, ok = rollout_check.check(rendered, live, IMAGE, {}, [])
        self.assertTrue(ok, lines)
        env[-3]["valueFrom"]["fieldRef"]["fieldPath"] = "status.hostIP"
        self.assertFalse(rollout_check.check(rendered, live, IMAGE, {}, [])[1])

    def test_a_render_without_sglang_stops(self):
        self.assertFalse(self.check([d for d in render() if d["metadata"]["name"] != "sglang"])[1])

    def test_malformed_allowlist_items_are_refused(self):
        for text in ("comfyui-gpu0", "comfyui-gpu0=comfyui", "=c:9Gi", "d=:9Gi", "d=c:"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                rollout_check.parse_expected(text)


class CommandTests(unittest.TestCase):
    def run_tool(self, rendered, env=None):
        with tempfile.TemporaryDirectory() as directory:
            workloads, live = Path(directory) / "workloads.yaml", Path(directory) / "live.json"
            workloads.write_text(yaml.safe_dump_all(rendered + [{"kind": "Service", "metadata": {"name": "x"}}]))
            live.write_text(json.dumps({"items": LIVE}))
            environment = {**os.environ, "EXPECT_CHANGED": "", "EXPECT_SGLANG_ENV": "", **(env or {})}
            return subprocess.run([sys.executable, str(TOOL), str(workloads), str(live), IMAGE],
                                  check=False, capture_output=True, text=True, env=environment)

    def test_exit_codes(self):
        self.assertEqual(0, self.run_tool(render()).returncode)
        self.assertEqual(1, self.run_tool(render(**{"open-webui": {"memory": "6Gi"}})).returncode)
        self.assertEqual(0, self.run_tool(render(**{"open-webui": {"memory": "6Gi"}}),
                                          {"EXPECT_CHANGED": "open-webui=open-webui:6Gi"}).returncode)
        bad = self.run_tool(render(), {"EXPECT_CHANGED": "nonsense"})
        self.assertEqual(1, bad.returncode)
        self.assertIn("STOP:", bad.stderr)

    def test_usage(self):
        result = subprocess.run([sys.executable, str(TOOL), "--help"], check=False, capture_output=True, text=True)
        self.assertEqual(0, result.returncode)
        self.assertIn("Usage: rollout_check.py", result.stdout)
        self.assertEqual(2, subprocess.run([sys.executable, str(TOOL)], check=False, capture_output=True).returncode)


if __name__ == "__main__":
    unittest.main()
