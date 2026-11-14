"""Family composition and connector checks. No model, browser or GPU claims."""
import json
import ipaddress
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import unittest.mock

import yaml

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


def load_pin_config():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pin_config", ROOT / "apps/components/assistant/pin-config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PinConfigTests(unittest.TestCase):
    """pin-config.py maps settings to the variables they are computed from (Open WebUI config.py shapes)."""

    SOURCE = """
import os
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_API_KEYS = os.getenv('OPENAI_API_KEYS', '')
OPENAI_API_KEYS = OPENAI_API_KEYS if OPENAI_API_KEYS != '' else OPENAI_API_KEY
OPENAI_API_KEYS = [k.strip() for k in OPENAI_API_KEYS.split(';')]
USER_PERMISSIONS_CHAT_CONTROLS = os.getenv('USER_PERMISSIONS_CHAT_CONTROLS', 'True').lower() == 'true'
DEFAULT_USER_PERMISSIONS = {'chat': {'controls': USER_PERMISSIONS_CHAT_CONTROLS}}
try:
    default_model_params = loads(os.environ.get('DEFAULT_MODEL_PARAMS', '{}'))
except Exception:
    default_model_params = {}
DEFAULT_MODEL_PARAMS = default_model_params
BANNERS = os.environ['WEBUI_BANNERS']
UNRELATED = os.getenv('UNRELATED', '')
DEFAULT_CONFIG = {
    'openai.api_keys': OPENAI_API_KEYS,
    'user.permissions': DEFAULT_USER_PERMISSIONS,
    'models.default_params': DEFAULT_MODEL_PARAMS,
    'ui.banners': BANNERS,
    'ui.unrelated': UNRELATED,
    'ui.constant': 3,
}
"""

    def test_settings_follow_reassignment_chains_and_nested_blocks(self):
        sources = load_pin_config().setting_sources(self.SOURCE)
        self.assertEqual({"OPENAI_API_KEY", "OPENAI_API_KEYS"}, sources["openai.api_keys"])
        self.assertEqual({"USER_PERMISSIONS_CHAT_CONTROLS"}, sources["user.permissions"])
        self.assertEqual({"DEFAULT_MODEL_PARAMS"}, sources["models.default_params"])
        self.assertEqual({"WEBUI_BANNERS"}, sources["ui.banners"])
        self.assertEqual(set(), sources["ui.constant"])

    def test_missing_list_fails_closed_and_env_only_mode_pins_nothing(self):
        import contextlib, io, os
        pin = load_pin_config()
        for environ, code in (({"ENABLE_PERSISTENT_CONFIG": "true"}, 1), ({"ENABLE_PERSISTENT_CONFIG": "false"}, 0)):
            with unittest.mock.patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(code, pin.main())

    def test_system_prompt_changes_one_field_and_is_idempotent(self):
        pin = load_pin_config()
        stored = {"reasoning_tags": ["<think>", "</think>"], "custom_params": {"a": 1}}
        changed = pin.system_prompt_change(stored, "Use tools.")
        self.assertEqual({**stored, "system": "Use tools."}, changed)
        self.assertNotIn("system", stored)  # the stored value is copied, not edited in place
        self.assertIsNone(pin.system_prompt_change(changed, "Use tools."))
        self.assertEqual({"system": "Use tools."}, pin.system_prompt_change(None, "Use tools."))
        # An Admin Panel edit to the prompt lasts until the next start, like the other pins.
        self.assertEqual("Use tools.", pin.system_prompt_change({"system": "edited"}, "Use tools.")["system"])

    def test_model_record_failures_are_reported_and_never_stop_the_start(self):
        import os
        pin = load_pin_config()
        skipped = {"skipped": "DEFAULT_MODELS names no model"}
        with unittest.mock.patch.dict(os.environ, {"DEFAULT_MODELS": ""}, clear=True):
            self.assertEqual((skipped, skipped), pin.model_record_report())
        environ = {"DEFAULT_MODELS": "a, b", "MODEL_BUILTIN_TOOLS_OFF": "notes, calendar,notes"}
        with unittest.mock.patch.dict(os.environ, environ, clear=True):
            # Outside the image Open WebUI cannot be imported: reported by type, not raised.
            error = {"error": "ModuleNotFoundError"}
            self.assertEqual((error, error), pin.model_record_report())
            seen = []

            async def fake(model_ids, prompt, builtin_off):
                seen.append((model_ids, prompt, builtin_off))
                return {"a": ("updated", "unchanged"), "b": ("no model record", "no model record")}
            with unittest.mock.patch.object(pin, "pin_model_records", fake):
                report = pin.model_record_report()
                # A missing prompt file is reported; the tool categories are still written.
                with unittest.mock.patch.object(pin, "SYSTEM_PROMPT", Path("/nonexistent/system-prompt.md")):
                    missing = pin.model_record_report()
        prompt = (ROOT / "apps/components/assistant/system-prompt.md").read_text().strip()
        self.assertEqual([(["a", "b"], prompt, ["notes", "calendar"]), (["a", "b"], None, ["notes", "calendar"])], seen)
        self.assertEqual({"characters": len(prompt), "models": {"a": "updated", "b": "no model record"}}, report[0])
        self.assertEqual({"off": ["notes", "calendar"], "models": {"a": "unchanged", "b": "no model record"}}, report[1])
        self.assertEqual({"error": "system-prompt.md missing"}, missing[0])
        self.assertEqual(report[1], missing[1])

    def test_builtin_tool_categories_change_one_field_and_are_idempotent(self):
        pin = load_pin_config()
        stored = {"capabilities": {"builtin_tools": True}, "toolIds": ["server:mcp:workstation"]}
        changed = pin.builtin_tools_change(stored, ["notes", "calendar"])
        self.assertEqual({**stored, "builtinTools": {"notes": False, "calendar": False}}, changed)
        self.assertNotIn("builtinTools", stored)  # the stored value is copied, not edited in place
        self.assertIsNone(pin.builtin_tools_change(changed, ["calendar", "notes"]))
        # The list is the whole truth: an Admin Panel switch lasts until the next start, and a
        # category taken off the list comes back.
        edited = {**stored, "builtinTools": {"notes": False, "web_search": False}}
        self.assertEqual({"notes": False, "calendar": False}, pin.builtin_tools_change(edited, ["notes", "calendar"])["builtinTools"])
        self.assertEqual(stored, pin.builtin_tools_change(changed, []))
        self.assertIsNone(pin.builtin_tools_change(stored, []))
        self.assertIsNone(pin.builtin_tools_change({**stored, "builtinTools": None}, []))
        self.assertEqual({"builtinTools": {"notes": False}}, pin.builtin_tools_change(None, ["notes"]))

    def test_builtin_tool_categories_are_read_from_the_installed_open_webui(self):
        source = """
    def is_builtin_tool_enabled(category: str, default: bool = True) -> bool:
        return builtin_tools.get(category, default)
    if is_builtin_tool_enabled('time'):
    if is_builtin_tool_enabled('user_input', True):
    if is_note_chat or (
        is_builtin_tool_enabled('notes') and config.get('notes.enable')
    """
        self.assertEqual({"time", "user_input", "notes"}, load_pin_config().builtin_tool_categories(source))

    def test_confirmation_exemption_follows_the_group_list(self):
        pin = load_pin_config()
        stored = {"chat": {"controls": True}, "features": {"web_search": True, "notes": True}}
        granted = pin.confirmation_change(stored, True)
        self.assertEqual({**stored["features"], "web_search_without_confirmation": True}, granted["features"])
        self.assertEqual(stored["chat"], granted["chat"])
        self.assertNotIn("web_search_without_confirmation", stored["features"])  # copied, not edited in place
        self.assertIsNone(pin.confirmation_change(granted, True))
        # A group that never had the permission and is not on the list is left alone.
        self.assertIsNone(pin.confirmation_change(stored, False))
        self.assertIsNone(pin.confirmation_change(None, False))
        # A grant made in the Admin Panel, or left from an earlier list, is removed.
        self.assertIs(False, pin.confirmation_change(granted, False)["features"]["web_search_without_confirmation"])
        self.assertEqual({"features": {"web_search_without_confirmation": True}}, pin.confirmation_change(None, True))


class AssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.large = service.kustomize(ROOT / "apps/overlays/assistant")
        cls.split = service.kustomize(ROOT / "apps/overlays/assistant-split")

    def test_family_theme_replaces_open_webui_static_sources(self):
        """Open WebUI empties its static folder at startup and copies /app/build/static,
        so the theme must mount over that source, one file each, from one small ConfigMap."""
        import base64, struct
        theme = ROOT / "apps/components/assistant/theme"
        sizes = {"favicon.png": 512, "favicon-96x96.png": 96, "apple-touch-icon.png": 180,
                 "web-app-manifest-192x192.png": 192, "web-app-manifest-512x512.png": 512,
                 "logo.png": 500, "splash.png": 500, "splash-dark.png": 500}
        for name, size in sizes.items():  # drop-in replacements at upstream dimensions
            header = (theme / name).read_bytes()[:24]
            self.assertEqual(b"\x89PNG\r\n\x1a\n", header[:8], name)
            self.assertEqual((size, size), struct.unpack(">II", header[16:24]), name)
        self.assertEqual((theme / "logo.svg").read_bytes(), (theme / "favicon.svg").read_bytes())
        manifest = json.loads((theme / "site.webmanifest").read_text())
        self.assertEqual("Family AI", manifest["name"])
        for objects in (self.large, self.split):
            config = next(o for o in objects if o["kind"] == "ConfigMap"
                          and o["metadata"]["name"].startswith("webui-theme-"))
            files = set(config.get("data", {})) | set(config.get("binaryData", {}))
            total = sum(len(v) for v in config.get("data", {}).values()) + sum(
                len(base64.b64decode(v)) for v in config.get("binaryData", {}).values())
            self.assertLess(total, 900_000)  # ConfigMaps are limited to 1 MiB
            app, _ = environment(objects)
            mounts = {m["subPath"]: m for m in app["volumeMounts"] if m["name"] == "webui-theme"}
            self.assertEqual(files, set(mounts))
            for name, mount in mounts.items():
                self.assertEqual(f"/app/build/static/{name}", mount["mountPath"])
                self.assertTrue(mount["readOnly"])

    def test_effective_family_configuration_retains_rag_and_private_gateway(self):
        for objects, model, images in ((self.large, "Qwen3.8-27B-FP8", "false"),
                                       (self.split, "Qwen3.5-9B", "true")):
            app, env = environment(objects)
            self.assertEqual(2, len(app["envFrom"]))
            self.assertEqual("/rag-models/Qwen3-Embedding-0.6B", env["RAG_EMBEDDING_MODEL"])
            self.assertEqual("Family AI", env["WEBUI_NAME"])
            self.assertEqual(model, env["DEFAULT_MODELS"])
            self.assertEqual(images, env["ENABLE_IMAGE_GENERATION"])
            # Admin Panel settings persist; the deployment's own settings are re-applied by
            # pin-config.py before Open WebUI starts (the list comes from service.py).
            self.assertEqual("true", env["ENABLE_PERSISTENT_CONFIG"])
            self.assertEqual(["python3 -I /webui-pins/pin-config.py && exec bash start.sh"], app["args"])
            self.assertIn({"name": "webui-config-pins", "mountPath": "/webui-pins", "readOnly": True}, app["volumeMounts"])
            self.assertEqual("false", env["ENABLE_SIGNUP"])
            self.assertEqual("false", env["ENABLE_DIRECT_CONNECTIONS"])
            self.assertEqual("true", env["ENABLE_FORWARD_USER_INFO_HEADERS"])
            self.assertEqual("http://litellm:4000/webui/v1", env["OPENAI_API_BASE_URL"])
            self.assertEqual("http://service-tools:8000/webui", env["IMAGES_OPENAI_API_BASE_URL"])
            self.assertEqual("workstation-qwen-image", env["IMAGE_GENERATION_MODEL"])
            self.assertEqual("false", env["ENABLE_IMAGE_EDIT"])
            self.assertEqual("", env["AUDIO_TTS_ENGINE"])
            self.assertEqual("", env["AUDIO_TTS_OPENAI_API_BASE_URL"])
            self.assertEqual("", env["AUDIO_STT_OPENAI_API_BASE_URL"])
            # Voice input is local: faster-whisper from the verified models volume, no
            # external speech service and no download.
            self.assertEqual("true", env["USER_PERMISSIONS_CHAT_STT"])
            self.assertEqual("/rag-models/faster-whisper-base", env["WHISPER_MODEL"])
            self.assertEqual("false", env["WHISPER_MODEL_AUTO_UPDATE"])
            self.assertEqual(4, len(json.loads(env["DEFAULT_PROMPT_SUGGESTIONS"])))
            pin = service.yaml.safe_load((ROOT / "apps/components/assistant/kustomization.yaml").read_text())["images"][0]
            image = pin["name"] + ":" + pin["newTag"] + "@" + pin["digest"]
            # The manifests pin the image versions.lock records (the webui AI-stack stage's output).
            lock = dict(line.split("=", 1) for line in (ROOT / "versions.lock").read_text().splitlines()
                        if "=" in line and not line.startswith("#"))
            self.assertEqual(lock["OPEN_WEBUI_IMAGE"], image)
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
            # Added: the assistant profile, its network policy, the pin-config.py ConfigMap
            # and the Family AI theme ConfigMap.
            self.assertEqual(len(previous) + 4, len(objects))
            pins = next(o for o in objects if o["kind"] == "ConfigMap" and "pin-config.py" in o.get("data", {}))
            # The tool-use system prompt ships beside the script that writes it to the model record.
            self.assertEqual({"pin-config.py", "system-prompt.md"}, set(pins["data"]))
            self.assertEqual((ROOT / "apps/components/assistant/system-prompt.md").read_text(), pins["data"]["system-prompt.md"])
            for obj in objects:
                self.assertNotEqual("Secret", obj["kind"])
                if obj["kind"] == "Deployment":
                    self.assertEqual(0, obj["spec"]["replicas"])
            pod = named(objects, "Deployment", "open-webui")["spec"]["template"]["spec"]
            old_pod = named(previous, "Deployment", "open-webui")["spec"]["template"]["spec"]
            self.assertEqual(old_pod["securityContext"], pod["securityContext"])
            # Storage is unchanged; the additions are read-only ConfigMaps (pins, theme).
            self.assertEqual(old_pod["volumes"],
                             [v for v in pod["volumes"] if v["name"] not in {"webui-config-pins", "webui-theme"}])
            tools = named(objects, "Deployment", "service-tools")["spec"]["template"]["spec"]
            old_tools = named(previous, "Deployment", "service-tools")["spec"]["template"]["spec"]
            self.assertEqual(old_tools["securityContext"], tools["securityContext"])
            for field in ("image", "command", "resources", "securityContext"):
                self.assertEqual(old_tools["containers"][0][field], tools["containers"][0][field])
            self.assertEqual(old_tools["volumes"], [v for v in tools["volumes"] if v["name"] != "web-search"])

    def test_family_keeps_the_search_confirmation_and_unused_tools_are_off(self):
        env = dict(line.split("=", 1) for line in (ROOT / "apps/components/assistant/assistant.env").read_text().splitlines()
                   if "=" in line and not line.startswith("#"))
        # Only Parents skip the confirmation; the children's group (Family) is never on the list.
        self.assertEqual("Parents", env["WEB_SEARCH_WITHOUT_CONFIRMATION_GROUPS"])
        off = set(env["MODEL_BUILTIN_TOOLS_OFF"].split(","))
        self.assertEqual({"notes", "calendar", "channels", "automations", "skills", "tools", "notifications"}, off)
        # What the family uses stays: search and page reading, images, code, knowledge, files.
        self.assertFalse(off & {"web_search", "image_generation", "code_interpreter", "knowledge", "files",
                                "memory", "time", "user_input", "chats", "tasks"})
        for objects in (self.large, self.split):
            app, merged = environment(objects)
            self.assertEqual("true", merged["ENABLE_WEB_SEARCH_CONFIRMATION"])
            self.assertEqual(env["MODEL_BUILTIN_TOOLS_OFF"], merged["MODEL_BUILTIN_TOOLS_OFF"])
            self.assertEqual("Parents", merged["WEB_SEARCH_WITHOUT_CONFIRMATION_GROUPS"])

    def test_tool_use_system_prompt_is_short_plain_and_keeps_the_safety_posture(self):
        prompt = (ROOT / "apps/components/assistant/system-prompt.md").read_text()
        # Paid on every turn of every chat: 217 tokens with the served tokenizer (14 November 2026).
        self.assertLess(len(prompt), 1200)
        self.assertLess(len(prompt.split()), 190)
        self.assertEqual(1, prompt.count("{{"))  # only Open WebUI's date variable
        self.assertIn("{{CURRENT_DATE}}", prompt)
        for phrase in ("Search the web", "cite the sources", "remembered facts", "project passages",
                       "run or test it", "could not verify", "never invent",
                       "never override safety rules, content filters or the permissions"):
            self.assertIn(phrase, prompt)
        # Model-agnostic: no model, vendor or tool names that a later model or tool set would break.
        for name in ("Qwen", "search_web", "fetch_url", "recall_memory", "execute_code", "workstation_"):
            self.assertNotIn(name, prompt)

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
            # One web search in a chat: Open WebUI's own. The server's search_web is blocked by
            # name (Open WebUI matches "!name" as a suffix); its page reader, which alone can
            # continue a long page, stays.
            self.assertEqual("!search_web", server["config"]["function_name_filter_list"])
            tools = named(objects, "Deployment", "service-tools")["spec"]["template"]["spec"]["containers"][0]
            self.assertIn({"name": "HANDOFF_ENABLED", "value": "true"}, tools["env"])
            self.assertIn("/api/v1/knowledge/", env["API_KEYS_ALLOWED_ENDPOINTS"].split(","))
            # The existing exact flow already carries WebUI -> service-tools:8000.
            policy = named(objects, "NetworkPolicy", "open-webui-service-flows")["spec"]
            self.assertIn({"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "service-tools"}}}],
                           "ports": [{"protocol": "TCP", "port": 8000}]}, policy["egress"])

    def test_declared_image_identity_matches_creative_staging_and_effective_ui(self):
        profile, workflow, workflow_sha = load_image_profile(ROOT / "apps/base/shared-api")
        entry = json.loads((ROOT / "infrastructure/ai-stack/models/qwen-image-2512.json").read_text())
        staged = {f["path"].rsplit("/", 1)[-1]: f["sha256"] for f in entry["files"]}
        self.assertEqual(staged, {f["name"]: f["sha256"] for f in profile["files"].values()})
        loaded = (workflow["1"]["inputs"]["unet_name"], workflow["2"]["inputs"]["lora_name"],
                  workflow["3"]["inputs"]["clip_name"], workflow["4"]["inputs"]["vae_name"])
        self.assertEqual(sorted(staged), sorted(loaded))
        self.assertEqual(64, len(workflow_sha))
        app, env = environment(self.split)
        self.assertEqual(profile["id"], env["IMAGE_GENERATION_MODEL"])
        self.assertEqual(profile["sizes"][0], env["IMAGE_SIZE"])
        cm = next(o for o in self.split if o["kind"] == "ConfigMap" and
                  o["metadata"]["name"].startswith("gateway-runtime-"))
        self.assertEqual(profile, json.loads(cm["data"]["image-profile.json"]))
        self.assertEqual(workflow, json.loads(cm["data"][profile["workflow"]]))

    def test_comfyui_gpu1_runs_the_qwen_image_stack_read_only(self):
        pod = named(self.split, "Deployment", "comfyui-gpu1")["spec"]["template"]["spec"]
        self.assertNotIn("swarmui", {o["metadata"]["name"] for o in self.split if o["kind"] == "Deployment"})
        container = pod["containers"][0]
        self.assertRegex(container["image"], r"^registry\.lan\.thespry\.family/local/creative-gfx1201@sha256:[a-f0-9]{64}$")
        self.assertEqual([987, 983], pod["securityContext"]["supplementalGroups"])
        self.assertEqual([{"name": "gpu-1", "resourceClaimName": "gpu-1"}], pod["resourceClaims"])
        # the picture stack from the local-NVMe creative volume, not the NAS ai-models volume
        self.assertIn({"name": "state", "mountPath": "/image", "subPath": "Image", "readOnly": True}, container["volumeMounts"])
        self.assertNotIn("models", [v["name"] for v in pod["volumes"]])
        config = next(v for v in pod["volumes"] if v["name"] == "config")["configMap"]["name"]
        self.assertTrue(config.startswith("comfyui-gpu1-model-paths"), config)
        paths = yaml.safe_load((ROOT / "apps/base/comfyui-gpu1/comfy-model-paths.yaml").read_text())
        self.assertEqual({"qwen_image": {"base_path": "/image", "diffusion_models": "diffusion_models", "loras": "loras",
                                         "text_encoders": "text_encoders", "vae": "vae"}}, paths)
        env = {e["name"]: e["value"] for e in container["env"]}
        self.assertEqual("1", env["COMFYUI_ENABLE_MIOPEN"])
        self.assertEqual("0:0-47", env["HSA_CU_MASK"])
        # MIOpen databases persist on the state volume; the init container creates them.
        mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
        self.assertEqual("Data/miopen-gpu1", mounts["/miopen"]["subPath"])
        for key in ("MIOPEN_USER_DB_PATH", "MIOPEN_CUSTOM_CACHE_DIR"):
            self.assertTrue(env[key].startswith("/miopen/"), key)
            self.assertIn(env[key].replace("/miopen/", "/state/Data/miopen-gpu1/", 1), pod["initContainers"][0]["args"][0])
        # the service's only route in: service-tools sends the fixed workflow to this port
        port = container["args"][container["args"].index("--port") + 1]
        self.assertEqual(int(port), named(self.split, "Service", "comfyui-gpu1")["spec"]["ports"][0]["port"])

    def test_comfyui_starts_with_the_measured_shared_card_flags(self):
        # Qwen-Image streamed from the NVMe (--fast-disk) with 2 GB of DynamicVRAM headroom; no fp8
        # downcast of the bf16 model. --reserve-vram keeps ComfyUI out of what SGLang grows into.
        args = named(self.split, "Deployment", "comfyui-gpu1")["spec"]["template"]["spec"]["containers"][0]["args"]
        for flag in ("--fp8_e4m3fn-unet", "--fp8_e4m3fn-text-enc", "--disable-dynamic-vram"):
            self.assertNotIn(flag, args)
        for flag in ("--fast-disk", "--use-ck-attention", "--disable-all-custom-nodes"):
            self.assertIn(flag, args)
        self.assertEqual("1.5", args[args.index("--reserve-vram") + 1])
        self.assertEqual("2", args[args.index("--vram-headroom") + 1])
        self.assertEqual("/config/comfy-model-paths.yaml", args[args.index("--extra-model-paths-config") + 1])
        self.assertNotIn("--cuda-device", args)  # DRA shows the pod only its claimed card, as ordinal zero

    def test_shared_mode_adds_a_second_comfyui_on_gpu0_only_in_shared_renders(self):
        shared = service.kustomize(ROOT / "apps/overlays/assistant-shared")
        memory = service.kustomize(ROOT / "apps/overlays/assistant-memory-shared")
        tools_env = lambda objects: {e["name"]: e.get("value") for e in next(
            c for c in named(objects, "Deployment", "service-tools")["spec"]["template"]["spec"]["containers"]
            if c["name"] == "tools").get("env", [])}
        for objects in (self.large, self.split):
            self.assertNotIn("comfyui-gpu0", {o["metadata"]["name"] for o in objects if o["kind"] == "Deployment"})
            self.assertNotIn("IMAGE_CONCURRENCY", tools_env(objects))  # one picture at a time
        for objects in (shared, memory):
            webui = named(objects, "Deployment", "open-webui")["spec"]["template"]["spec"]["containers"][0]
            env = {e["name"]: e.get("value") for e in webui["env"]}
            self.assertEqual("true", env["ENABLE_IMAGE_GENERATION"])
            self.assertEqual("2", tools_env(objects)["IMAGE_CONCURRENCY"])  # one per card
            self.assertNotIn("swarmui", {o["metadata"]["name"] for o in objects if o["kind"] == "Deployment"})
        # large-model budgets, not the 9B split ones
        self.assertEqual({e["name"]: e.get("value") for e in named(self.large, "Deployment", "open-webui")["spec"]["template"]["spec"]["containers"][0]["env"]}["WEB_SEARCH_RESULT_COUNT"],
                         {e["name"]: e.get("value") for e in named(shared, "Deployment", "open-webui")["spec"]["template"]["spec"]["containers"][0]["env"]}["WEB_SEARCH_RESULT_COUNT"])
        comfy = named(shared, "Deployment", "comfyui-gpu0")["spec"]["template"]["spec"]
        gpu1 = named(shared, "Deployment", "comfyui-gpu1")["spec"]["template"]["spec"]
        self.assertEqual(0, named(shared, "Deployment", "comfyui-gpu0")["spec"]["replicas"])
        self.assertEqual("qualified", named(shared, "Deployment", "comfyui-gpu1")["metadata"]["annotations"]["workstation.ai/qualification"])
        self.assertEqual([{"name": "gpu-0", "resourceClaimName": "gpu-0"}], comfy["resourceClaims"])
        self.assertEqual(gpu1["securityContext"], comfy["securityContext"])
        c, g = comfy["containers"][0], gpu1["containers"][0]
        self.assertEqual(g["image"], c["image"])
        # 8 CPUs per picture backend in shared mode (Qwen-Image 64 s warm instead of 74 s); the
        # split composition keeps comfyui-gpu1 at its measured 4.
        for container, memory in ((g, "12Gi"), (c, "10Gi")):
            self.assertEqual({"cpu": "8", "memory": memory}, container["resources"]["limits"])
            self.assertEqual(container["resources"]["requests"], {k: v for k, v in container["resources"]["limits"].items()})
        split_gpu1 = named(self.split, "Deployment", "comfyui-gpu1")["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual({"cpu": "4", "memory": "12Gi"}, split_gpu1["resources"]["limits"])
        self.assertEqual([{"name": "gpu-0"}], c["resources"]["claims"])
        # the same picture stack mount and file names on both cards
        image = {"name": "state", "mountPath": "/image", "subPath": "Image", "readOnly": True}
        self.assertIn(image, g["volumeMounts"])
        self.assertIn(image, c["volumeMounts"])
        self.assertNotIn("models", [v["name"] for v in comfy["volumes"]])
        paths = yaml.safe_load((ROOT / "apps/base/comfyui-gpu0/comfy-model-paths.yaml").read_text())
        self.assertEqual(yaml.safe_load((ROOT / "apps/base/comfyui-gpu1/comfy-model-paths.yaml").read_text())["qwen_image"],
                         paths["qwen_image"])
        # the same measured picture flags on both cards
        for flag in ("--fp8_e4m3fn-unet", "--fp8_e4m3fn-text-enc"):
            self.assertNotIn(flag, g["args"])
            self.assertNotIn(flag, c["args"])
        for flag in ("--fast-disk", "--use-ck-attention"):
            self.assertIn(flag, g["args"])
            self.assertIn(flag, c["args"])
        for flag in ("--reserve-vram", "--vram-headroom", "--port"):
            self.assertEqual(g["args"][g["args"].index(flag) + 1], c["args"][c["args"].index(flag) + 1], flag)
        cu_mask = lambda container: next(e["value"] for e in container["env"] if e["name"] == "HSA_CU_MASK")
        self.assertEqual(cu_mask(g), cu_mask(c))
        # the video backend caps DynamicVRAM (it ignores --reserve-vram): 2 GB free at a clip's peak
        self.assertEqual("2", c["args"][c["args"].index("--vram-headroom") + 1])
        self.assertNotIn("--disable-dynamic-vram", c["args"])
        # every directory ComfyUI is pointed at on the emptyDir exists before it starts
        for name, container in (("comfyui-gpu0", c), ("comfyui-gpu1", g)):
            init = named(shared, "Deployment", name)["spec"]["template"]["spec"]["initContainers"][0]
            self.assertIn({"name": "work", "mountPath": "/work"}, init["volumeMounts"])
            created = init["args"][0].split()
            for flag in ("--user-directory", "--input-directory", "--output-directory"):
                path = container["args"][container["args"].index(flag) + 1]
                self.assertTrue(path.startswith("/work/") and path in created, (name, flag, path))
            port = container["args"][container["args"].index("--port") + 1]
            service_port = named(shared, "Service", name)["spec"]["ports"][0]["port"]
            self.assertEqual(int(port), service_port)

    def test_changed_workflow_or_unsupported_profile_refuses_startup(self):
        base = ROOT / "apps/base/shared-api"
        for mutation in ("node", "route", "batch", "profile", "path", "serving-model", "no-serving-models", "file", "steps",
                         "save-node"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                profile = json.loads((base / "image-profile.json").read_text())
                workflow = json.loads((base / "qwen-image-workflow.json").read_text())
                if mutation == "node": workflow["11"]["class_type"] = "UnreviewedNode"
                if mutation == "route": workflow["6"]["inputs"]["clip"] = ["1", 0]
                if mutation == "batch": workflow["8"]["inputs"]["batch_size"] = 2
                if mutation == "file": profile["files"]["lora"]["name"] = "unreviewed.safetensors"
                if mutation == "steps": workflow["9"]["inputs"]["steps"] = 50
                if mutation == "save-node": workflow["11"]["class_type"] = "SwarmSaveImageWS"
                if mutation == "profile": profile["id"] = "unqualified-model"
                if mutation == "path": profile["workflow"] = "../untrusted.json"
                if mutation == "serving-model": profile["serving_models"].append("unqualified-model")
                if mutation == "no-serving-models": profile["serving_models"] = []
                (directory / "image-profile.json").write_text(json.dumps(profile))
                (directory / "qwen-image-workflow.json").write_text(json.dumps(workflow))
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
