"""Family composition and connector checks. No model, browser or GPU claims."""
import json
import ipaddress
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/base/shared-api"), str(ROOT / "lib/workstation")]
import service
from service_tools import load_image_profile


def named(objects, kind, name):
    return next(o for o in objects if o["kind"] == kind and o["metadata"]["name"] == name)


def environment(objects):
    pod = named(objects, "Deployment", "open-webui")["spec"]["template"]["spec"]
    app = pod["containers"][0]
    merged = {}
    for source in app["envFrom"]:
        merged.update(named(objects, "ConfigMap", source["configMapRef"]["name"])["data"])
    for entry in app["env"]:
        merged[entry["name"]] = entry.get("value", entry.get("valueFrom"))
    return app, merged


class AssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.large = service.kustomize(ROOT / "apps/overlays/assistant")
        cls.split = service.kustomize(ROOT / "apps/overlays/assistant-split")

    def test_effective_family_configuration_retains_rag_and_private_gateway(self):
        for objects, model, images in ((self.large, "Qwen3.8-27B-FP8", "false"),
                                       (self.split, "Qwen3.5-9B", "true")):
            app, env = environment(objects)
            self.assertEqual(2, len(app["envFrom"]))
            self.assertEqual("/rag-models/Qwen3-Embedding-0.6B", env["RAG_EMBEDDING_MODEL"])
            self.assertEqual("Family AI", env["WEBUI_NAME"])
            self.assertEqual(model, env["DEFAULT_MODELS"])
            self.assertEqual(images, env["ENABLE_IMAGE_GENERATION"])
            self.assertEqual("false", env["ENABLE_PERSISTENT_CONFIG"])
            self.assertEqual("false", env["ENABLE_SIGNUP"])
            self.assertEqual("false", env["ENABLE_DIRECT_CONNECTIONS"])
            self.assertEqual("true", env["ENABLE_FORWARD_USER_INFO_HEADERS"])
            self.assertEqual("http://litellm:4000/webui/v1", env["OPENAI_API_BASE_URL"])
            self.assertEqual("http://service-tools:8000/webui", env["IMAGES_OPENAI_API_BASE_URL"])
            self.assertEqual("workstation-sdxl", env["IMAGE_GENERATION_MODEL"])
            self.assertEqual("false", env["ENABLE_IMAGE_EDIT"])
            self.assertEqual("", env["AUDIO_TTS_ENGINE"])
            self.assertEqual("", env["AUDIO_TTS_OPENAI_API_BASE_URL"])
            self.assertEqual("", env["AUDIO_STT_OPENAI_API_BASE_URL"])
            self.assertEqual("false", env["USER_PERMISSIONS_CHAT_STT"])
            self.assertEqual(4, len(json.loads(env["DEFAULT_PROMPT_SUGGESTIONS"])))
            pin = service.yaml.safe_load((ROOT / "apps/components/assistant/kustomization.yaml").read_text())["images"][0]
            image = pin["name"] + ":" + pin["newTag"] + "@" + pin["digest"]
            self.assertEqual("v0.12.5", pin["newTag"])
            pod = named(objects, "Deployment", "open-webui")["spec"]["template"]["spec"]
            self.assertTrue(all(c["image"] == image for c in pod["initContainers"] + pod["containers"]))

    def test_assistant_does_not_change_gpu_ownership_images_policies_or_persistence(self):
        for overlay, objects in (("service", self.large), ("service-split", self.split)):
            previous = service.kustomize(ROOT / "apps/overlays" / overlay)
            for original in previous:
                name, kind = original["metadata"]["name"], original["kind"]
                if kind == "Deployment" and name in {"open-webui", "service-tools"}:
                    continue
                if kind == "ConfigMap" and name.startswith("webui-rag-profile-"):
                    continue  # The reranker component merges its profile; checked below.
                self.assertEqual(original, named(objects, kind, name), (kind, name))
            reranker = dict(line.split("=", 1) for line in
                            (ROOT / "apps/components/rag-reranker/reranker.env").read_text().splitlines()
                            if line and not line.startswith("#"))
            profiles = [next(o["data"] for o in rendered if o["kind"] == "ConfigMap"
                             and o["metadata"]["name"].startswith("webui-rag-profile-")) for rendered in (previous, objects)]
            self.assertEqual({**profiles[0], **reranker}, profiles[1])
            self.assertEqual(("none", "ms-marco-minilm-l6-v2"), tuple(p["RAG_RERANKER_PROFILE"] for p in profiles))
            self.assertEqual(len(previous) + 2, len(objects))
            for obj in objects:
                self.assertNotEqual("Secret", obj["kind"])
                if obj["kind"] == "Deployment":
                    self.assertEqual(0, obj["spec"]["replicas"])
            pod = named(objects, "Deployment", "open-webui")["spec"]["template"]["spec"]
            old_pod = named(previous, "Deployment", "open-webui")["spec"]["template"]["spec"]
            self.assertEqual(old_pod["securityContext"], pod["securityContext"])
            self.assertEqual(old_pod["volumes"], pod["volumes"])
            tools = named(objects, "Deployment", "service-tools")["spec"]["template"]["spec"]
            old_tools = named(previous, "Deployment", "service-tools")["spec"]["template"]["spec"]
            self.assertEqual(old_tools["securityContext"], tools["securityContext"])
            for field in ("image", "command", "resources", "securityContext"):
                self.assertEqual(old_tools["containers"][0][field], tools["containers"][0][field])
            self.assertEqual(old_tools["volumes"], [v for v in tools["volumes"] if v["name"] != "web-search"])

    def test_shared_search_credentials_and_external_loader_preserve_local_rag(self):
        for objects, count in ((self.large, "3"), (self.split, "1")):
            runtime = next(o for o in objects if o["kind"] == "ConfigMap" and
                           o["metadata"]["name"].startswith("gateway-runtime-"))
            self.assertEqual("exa", json.loads(runtime["data"]["web-search-policy.json"])["provider"])
            _, env = environment(objects)
            self.assertEqual("true", env["ENABLE_WEB_SEARCH"])
            self.assertEqual({"function_calling": "native"}, json.loads(env["DEFAULT_MODEL_PARAMS"]))
            self.assertEqual("true", env["ENABLE_WEB_SEARCH_CONFIRMATION"])
            self.assertEqual("external", env["WEB_SEARCH_ENGINE"])
            self.assertEqual("external", env["WEB_LOADER_ENGINE"])
            self.assertEqual("http://service-tools:8000/webui/search", env["EXTERNAL_WEB_SEARCH_URL"])
            self.assertEqual("http://service-tools:8000/webui/fetch", env["EXTERNAL_WEB_LOADER_URL"])
            self.assertEqual(env["OPENAI_API_KEY"], env["EXTERNAL_WEB_SEARCH_API_KEY"])
            self.assertEqual(env["OPENAI_API_KEY"], env["EXTERNAL_WEB_LOADER_API_KEY"])
            self.assertEqual(count, env["WEB_SEARCH_RESULT_COUNT"])
            self.assertEqual("true", env["BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL"])
            for obj in objects:
                if obj["kind"] != "Deployment": continue
                volumes = obj["spec"]["template"]["spec"]["volumes"]
                names = {v["secret"]["secretName"] for v in volumes if "secret" in v}
                if obj["metadata"]["name"] == "service-tools":
                    self.assertEqual({"gateway-image-permissions", "exa-search"}, names)
                    provider = next(v["secret"] for v in volumes if v.get("secret", {}).get("secretName") == "exa-search")
                    self.assertEqual([{"key": "api-key", "path": "exa-api-key"}], provider["items"])
                else:
                    self.assertNotIn("exa-search", names)
            policy = named(objects, "NetworkPolicy", "assistant-web-search-egress")["spec"]
            self.assertEqual({"app.kubernetes.io/name": "service-tools"}, policy["podSelector"]["matchLabels"])
            self.assertEqual([{"protocol": "TCP", "port": 443}], policy["egress"][0]["ports"])
            excluded = [ipaddress.ip_network(n) for n in policy["egress"][0]["to"][0]["ipBlock"]["except"]]
            for address in ("127.0.0.1", "169.254.169.254", "10.43.0.1", "192.168.2.15", "172.16.1.1"):
                self.assertTrue(any(ipaddress.ip_address(address) in n for n in excluded))

    def test_browser_tools_use_each_users_own_session_identity(self):
        for objects in (self.large, self.split):
            _, env = environment(objects)
            servers = json.loads(env["TOOL_SERVER_CONNECTIONS"])
            self.assertEqual(1, len(servers))
            server = servers[0]
            # Native MCP with the signed-in user's own token; no shared credential.
            self.assertEqual(("mcp", "http://service-tools:8000/mcp", "session", ""),
                             (server["type"], server["url"], server["auth_type"], server["key"]))
            self.assertNotIn("headers", server)
            self.assertFalse(server.get("forward_cookies", False))
            self.assertTrue(server["config"]["enable"])
            self.assertEqual([{"principal_type": "user", "principal_id": "*", "permission": "read"}],
                             server["config"]["access_grants"])
            self.assertEqual("workstation", server["info"]["id"])
            tools = named(objects, "Deployment", "service-tools")["spec"]["template"]["spec"]["containers"][0]
            self.assertIn({"name": "HANDOFF_ENABLED", "value": "true"}, tools["env"])
            self.assertIn("/api/v1/knowledge/", env["API_KEYS_ALLOWED_ENDPOINTS"].split(","))
            # The existing exact flow already carries WebUI -> service-tools:8000.
            policy = named(objects, "NetworkPolicy", "open-webui-service-flows")["spec"]
            self.assertIn({"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "service-tools"}}}],
                           "ports": [{"protocol": "TCP", "port": 8000}]}, policy["egress"])

    def test_declared_image_identity_matches_creative_staging_and_effective_ui(self):
        profile, workflow, workflow_sha = load_image_profile(ROOT / "apps/base/shared-api")
        model = json.loads((ROOT / "infrastructure/creative/runtime.json").read_text())["model"]
        self.assertEqual(model["repository"], profile["model_repository"])
        self.assertEqual(model["revision"], profile["model_revision"])
        self.assertEqual(model["sha256"], profile["model_sha256"])
        self.assertEqual(model["file"], Path(workflow["1"]["inputs"]["ckpt_name"]).name)
        self.assertEqual(64, len(workflow_sha))
        app, env = environment(self.split)
        self.assertEqual(profile["id"], env["IMAGE_GENERATION_MODEL"])
        cm = next(o for o in self.split if o["kind"] == "ConfigMap" and
                  o["metadata"]["name"].startswith("gateway-runtime-"))
        self.assertEqual(profile, json.loads(cm["data"]["image-profile.json"]))
        self.assertEqual(workflow, json.loads(cm["data"][profile["workflow"]]))

    def test_swarmui_mounts_the_catalogue_sdxl_revision_read_only(self):
        profile, _, _ = load_image_profile(ROOT / "apps/base/shared-api")
        entry = json.loads((ROOT / "infrastructure/ai-stack/models/sdxl-base-1.0.json").read_text())["model"]
        pod = named(self.split, "Deployment", "swarmui")["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertRegex(container["image"], r"^registry\.lan\.thespry\.family/local/creative-gfx1201@sha256:[a-f0-9]{64}$")
        self.assertEqual([987, 983], pod["securityContext"]["supplementalGroups"])
        folder, name = profile["swarm_model"].split("/")
        mount = next(m for m in container["volumeMounts"] if m["name"] == "models")
        self.assertEqual({"name": "models", "mountPath": f"/SwarmUI/Models/Stable-Diffusion/{folder}",
                          "subPath": f"{entry['repository']}/{entry['revision']}", "readOnly": True}, mount)
        self.assertIn(f"{name}.safetensors", [f["path"] for f in entry["files"]])
        volume = next(v for v in pod["volumes"] if v["name"] == "models")
        self.assertEqual({"claimName": "ai-models", "readOnly": True}, volume["persistentVolumeClaim"])
        env = {e["name"]: e["value"] for e in container["env"]}
        self.assertEqual("1", env["COMFYUI_ENABLE_MIOPEN"])
        for key in ("MIOPEN_USER_DB_PATH", "MIOPEN_CUSTOM_CACHE_DIR"):
            # Persistent (Data is on the state volume) and created by the seed container.
            self.assertTrue(env[key].startswith("/SwarmUI/Data/"), key)
            self.assertIn(env[key].replace("/SwarmUI/", "/state/", 1), pod["initContainers"][0]["args"][0])

    def test_comfyui_starts_with_the_measured_shared_card_flags(self):
        # fp8 UNet + text encoders: 6.6 GiB peak at 1024² (14.7 GiB at defaults). --reserve-vram keeps
        # ComfyUI out of the ~0.6-0.8 GiB SGLang grows into after startup on a shared card.
        seed = (ROOT / "apps/base/swarmui/Backends.fds").read_text()
        args = next(line for line in seed.splitlines() if "ExtraArgs:" in line).split("ExtraArgs:", 1)[1].split()
        for flag in ("--fp8_e4m3fn-unet", "--fp8_e4m3fn-text-enc"):
            self.assertIn(flag, args)
        self.assertEqual("1.5", args[args.index("--reserve-vram") + 1])
        self.assertNotIn("--cuda-device", args)  # DRA shows the pod only its claimed card, as ordinal zero
        self.assertEqual(seed, (ROOT / "infrastructure/ai-stack/creative/Backends.fds").read_text())

    def test_changed_workflow_or_unsupported_profile_refuses_startup(self):
        base = ROOT / "apps/base/shared-api"
        for mutation in ("node", "route", "batch", "profile", "path"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                profile = json.loads((base / "image-profile.json").read_text())
                workflow = json.loads((base / "sdxl-workflow.json").read_text())
                if mutation == "node": workflow["7"]["class_type"] = "UnreviewedNode"
                if mutation == "route": workflow["2"]["inputs"]["clip"] = ["1", 0]
                if mutation == "batch": workflow["4"]["inputs"]["batch_size"] = 2
                if mutation == "profile": profile["id"] = "unqualified-model"
                if mutation == "path": profile["workflow"] = "../untrusted.json"
                (directory / "image-profile.json").write_text(json.dumps(profile))
                (directory / "sdxl-workflow.json").write_text(json.dumps(workflow))
                with self.assertRaises(ValueError):
                    load_image_profile(directory)

    def test_site_renderer_records_experience_and_preserves_legacy_default(self):
        site = {"context": "test", "node": "threadripper-ai",
                "node_address": "192.168.2.15", "management_cidr": "192.168.2.0/24",
                "client_cidrs": ["192.168.2.0/24"], "webui_host": "chat.test.local",
                "api_host": "api.test.local", "tls_secret": "owner-tls"}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "site.json"
            path.write_text(json.dumps(site))
            for experience in (None, "assistant", "assistant-memory"):
                for mode in ("large", "split"):
                    output = root / f"{experience}-{mode}"
                    args = SimpleNamespace(site=path, output=output, mode=mode)
                    if experience:
                        args.experience = experience
                    service.render(args)
                    record = json.loads((output / "evidence.json").read_text())
                    self.assertEqual(experience or "service", record["experience"])
                    self.assertEqual(mode, record["mode"])
                    self.assertEqual("source-rendered-not-deployed", record["status"])
                    objects = service.load_objects((output / "workloads.yaml").read_text())
                    if experience:
                        _, env = environment(objects)
                        self.assertEqual("Family AI", env["WEBUI_NAME"])
                        self.assertIn("AGENT_WEB_SEARCH_ENABLED=true", (output / "client.conf").read_text())
                        memory = experience == "assistant-memory"
                        self.assertIn(f"AGENT_MEMORY_ENABLED={'true' if memory else 'false'}", (output / "client.conf").read_text())
                        claims = {o["metadata"]["name"]: o for o in objects if o["kind"] == "PersistentVolumeClaim"}
                        self.assertEqual(memory, "graphiti-neo4j" in claims)
                        if memory:
                            self.assertEqual("irreplaceable", claims["graphiti-neo4j"]["metadata"]["labels"]["workstation.ai/backup"])
                            deployments = {o["metadata"]["name"] for o in objects if o["kind"] == "Deployment"}
                            self.assertLessEqual({"neo4j", "memory-embedding", "graphiti-memory"}, deployments)
                    self.assertTrue(all(service.sha(output / name) == digest
                                        for name, digest in record["files"].items()))


if __name__ == "__main__":
    unittest.main()
