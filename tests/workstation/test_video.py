"""generate_video: the operator-owned Wan workflow, picture-level access, one clip at a time."""
import asyncio
import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "apps/base/shared-api"
sys.path.insert(0, str(API))
from service_tools import ServiceTools
import video

WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8X" + b"\x00" * 16


class ProfileTests(unittest.TestCase):
    def test_shipped_profile_and_workflow_load(self):
        profile, workflow, digest = video.load_video_profile(API)
        self.assertEqual({"landscape": "832x480", "portrait": "480x832", "square": "624x624"}, profile["sizes"])
        self.assertEqual(64, len(digest))
        self.assertEqual("SaveAnimatedWEBP", workflow["15"]["class_type"])

    def test_changed_sampler_settings_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "video-profile.json").write_bytes((API / "video-profile.json").read_bytes())
            workflow = json.loads((API / "wan-t2v-workflow.json").read_text())
            for change in (("11", "steps", 20), ("12", "cfg", 3.5), ("11", "end_at_step", 3), ("12", "add_noise", "enable")):
                edited = copy.deepcopy(workflow)
                edited[change[0]]["inputs"][change[1]] = change[2]
                (target / "wan-t2v-workflow.json").write_text(json.dumps(edited))
                with self.assertRaises(ValueError, msg=change):
                    video.load_video_profile(target)
            edited = copy.deepcopy(workflow)
            edited["16"] = {"class_type": "SaveVideo", "inputs": {}}
            (target / "wan-t2v-workflow.json").write_text(json.dumps(edited))
            with self.assertRaises(ValueError):
                video.load_video_profile(target)

    def test_no_size_is_larger_than_the_measured_clip(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            profile = json.loads((API / "video-profile.json").read_text())
            profile["sizes"]["landscape"] = "1280x720"
            (target / "video-profile.json").write_text(json.dumps(profile))
            (target / "wan-t2v-workflow.json").write_bytes((API / "wan-t2v-workflow.json").read_bytes())
            with self.assertRaises(ValueError):
                video.load_video_profile(target)


class VideoToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.mapping = self.root / "mapping.json"
        self.mapping.write_text(json.dumps({"service_key": "s" * 32, "users": {
            "alice": {"images": True, "expires_at": 9999999999},
            "bob": {"images": False, "expires_at": 9999999999}}}))
        self.user = "alice"
        self.calls, self.submitted = [], []
        self.ready = {"phase": "ready", "model": "Qwen3.8-27B-MXFP4", "images": True}
        self.status = "success"
        self.gate = None
        self.view_type = "image/webp"

        async def upstream(req):
            self.calls.append(req)
            path = req.url.path
            if path == "/api/v1/users/user/status":
                return httpx.Response(200, json={"id": self.user, "role": "user"})
            if path == "/_service/status":
                return httpx.Response(200, json=self.ready)
            if req.url.host == "comfy" and path == "/prompt":
                self.submitted.append(json.loads(req.content)["prompt"])
                return httpx.Response(200, json={"prompt_id": "abcd-1234-ef56"})
            if req.url.host == "comfy" and path == "/history/abcd-1234-ef56":
                if self.gate is not None:
                    await self.gate.wait()
                return httpx.Response(200, json={"abcd-1234-ef56": {
                    "status": {"completed": self.status == "success", "status_str": self.status},
                    "outputs": {"15": {"images": [{"filename": "wan_00001_.webp", "subfolder": "video", "type": "output"}]}}}})
            if req.url.host == "comfy" and path == "/view":
                self.assertEqual({"filename": "wan_00001_.webp", "subfolder": "video", "type": "output"}, dict(req.url.params))
                return httpx.Response(200, content=WEBP, headers={"Content-Type": self.view_type})
            if req.url.host == "comfy" and path in {"/queue", "/interrupt"}:
                return httpx.Response(200, json={})
            raise AssertionError(f"unexpected upstream route {req.url}")

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.tools = ServiceTools(self.upstream, "http://webui", "http://swarm", "http://gateway",
                                  self.mapping, self.root / "state", public_webui="https://chat.test",
                                  video_comfy="http://comfy")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.tools.app), base_url="http://tools")
        self.tools.video.poll_seconds = 0

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()

    async def call(self, arguments):
        response = await self.client.post("/mcp", headers={"Authorization": "Bearer fixture-user-token"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "generate_video", "arguments": arguments}})
        return response

    async def test_tool_is_listed_with_only_prompt_and_orientation(self):
        response = await self.client.post("/mcp", headers={"Authorization": "Bearer fixture-user-token"},
                                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        spec = next(t for t in response.json()["result"]["tools"] if t["name"] == "generate_video")
        self.assertEqual({"prompt", "orientation"}, set(spec["inputSchema"]["properties"]))
        self.assertFalse(spec["inputSchema"]["additionalProperties"])

    async def test_clip_comes_back_as_an_animated_webp_image_item(self):
        response = await self.call({"prompt": "  a puppy runs through a meadow  ", "orientation": "portrait"})
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        text, image = result["content"]
        self.assertEqual("ok", json.loads(text["text"])["status"])
        self.assertEqual({"type": "image", "mimeType": "image/webp"}, {k: image[k] for k in ("type", "mimeType")})
        self.assertEqual(WEBP, base64.b64decode(image["data"]))
        workflow = self.submitted[0]
        self.assertEqual("a puppy runs through a meadow", workflow["8"]["inputs"]["text"])
        self.assertEqual((480, 832), (workflow["10"]["inputs"]["width"], workflow["10"]["inputs"]["height"]))
        self.assertEqual(workflow["11"]["inputs"]["noise_seed"], workflow["12"]["inputs"]["noise_seed"])
        # the loaded workflow is copied per request, never edited in place
        self.assertEqual("", self.tools.video.workflow["8"]["inputs"]["text"])

    async def test_users_without_picture_access_are_refused_before_any_render(self):
        self.user = "bob"
        result = (await self.call({"prompt": "a clip"})).json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual({"error": "video_permission_denied"}, json.loads(result["content"][0]["text"]))
        self.user = "carol"
        result = (await self.call({"prompt": "a clip"})).json()["result"]
        self.assertEqual({"error": "video_permission_denied"}, json.loads(result["content"][0]["text"]))
        self.assertEqual([], self.submitted)

    async def test_video_needs_ready_picture_mode_on_the_contract_model(self):
        for state in ({"phase": "ready", "model": "Qwen3.8-27B-MXFP4", "images": False},
                      {"phase": "maintenance", "model": None, "images": False},
                      {"phase": "ready", "model": "Qwen3.5-9B", "images": True}):
            self.ready = state
            result = (await self.call({"prompt": "a clip"})).json()["result"]
            self.assertEqual({"error": "video_requires_ready_image_mode"}, json.loads(result["content"][0]["text"]))
        self.assertEqual([], self.submitted)

    async def test_one_clip_at_a_time(self):
        self.gate = asyncio.Event()
        first = asyncio.create_task(self.call({"prompt": "first"}))
        while not self.submitted:
            await asyncio.sleep(0)
        busy = (await self.call({"prompt": "second"})).json()["result"]
        self.assertEqual({"error": "video_busy"}, json.loads(busy["content"][0]["text"]))
        self.gate.set()
        self.assertFalse((await first).json()["result"]["isError"])
        self.assertEqual(1, len(self.submitted))

    async def test_bad_arguments_and_bad_artifacts_are_refused(self):
        for arguments in ({}, {"prompt": ""}, {"prompt": "x", "orientation": "ultrawide"}, {"prompt": "x", "steps": 50}):
            self.assertEqual(400, (await self.call(arguments)).status_code, arguments)
        self.view_type = "text/html"
        result = (await self.call({"prompt": "a clip"})).json()["result"]
        self.assertEqual({"error": "invalid_video_artifact"}, json.loads(result["content"][0]["text"]))
        self.status = "error"
        self.view_type = "image/webp"
        result = (await self.call({"prompt": "a clip"})).json()["result"]
        self.assertEqual({"error": "video_generation_failed"}, json.loads(result["content"][0]["text"]))

    async def test_disabled_video_is_not_listed(self):
        tools = ServiceTools(self.upstream, "http://webui", "http://swarm", "http://gateway",
                             self.mapping, self.root / "state", public_webui="https://chat.test")
        self.assertIsNone(tools.video)


if __name__ == "__main__":
    unittest.main()
