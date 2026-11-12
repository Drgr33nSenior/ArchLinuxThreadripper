"""Short text-to-video clips (Wan 2.2 T2V A14B, 4-step) on the shared-mode second ComfyUI.

One operator-owned workflow (wan-t2v-workflow.json, checked at startup against
video-profile.json); callers choose only the prompt and an orientation. The clip comes back
as an animated WEBP MCP image item, which Open WebUI stores under the requesting user and
shows in the chat. Access follows picture access: the user must be bound with images
(`service browser-bind --images`). One clip at a time.
"""
import asyncio
import base64
import copy
import hashlib
import json
import re
import secrets
import time
from pathlib import Path

from boundary import Refusal, secret_json

TOOL = "generate_video"
NODE_TYPES = {"1": "UNETLoader", "2": "UNETLoader", "3": "LoraLoaderModelOnly", "4": "LoraLoaderModelOnly",
              "5": "ModelSamplingSD3", "6": "ModelSamplingSD3", "7": "CLIPLoader", "8": "CLIPTextEncode",
              "9": "CLIPTextEncode", "10": "EmptyHunyuanLatentVideo", "11": "KSamplerAdvanced",
              "12": "KSamplerAdvanced", "13": "VAELoader", "14": "VAEDecodeTiled", "15": "SaveAnimatedWEBP"}
SIZE = re.compile(r"([1-9][0-9]{2,3})x([1-9][0-9]{2,3})\Z")


def load_video_profile(directory):
    """Load and check the operator-owned Wan profile and workflow; return (profile, workflow, sha256)."""
    directory = Path(directory)
    profile = json.loads((directory / "video-profile.json").read_text())
    expected = {"schema", "id", "catalog", "workflow", "sizes", "frames", "fps", "steps", "split_step",
                "cfg_scale", "shift", "timeout_seconds", "max_bytes", "serving_models"}
    if not isinstance(profile, dict) or set(profile) != expected or profile["schema"] != 1:
        raise ValueError("invalid video profile")
    sizes = profile["sizes"]
    if (profile["workflow"] != "wan-t2v-workflow.json" or not isinstance(sizes, dict)
            or set(sizes) != {"landscape", "portrait", "square"}
            or not all(isinstance(v, str) and SIZE.fullmatch(v) for v in sizes.values())
            # 832x480 is the measured clip (32,760 attention tokens at 81 frames); no size may exceed it
            or any(int(w) * int(h) > 832 * 480 for w, h in (v.split("x") for v in sizes.values()))
            or profile["frames"] != 81 or profile["fps"] != 16 or profile["steps"] != 4 or profile["split_step"] != 2
            or profile["cfg_scale"] != 1.0 or profile["shift"] != 5.0
            or type(profile["timeout_seconds"]) is not int or not 60 <= profile["timeout_seconds"] <= 1200
            or type(profile["max_bytes"]) is not int or not 1 << 20 <= profile["max_bytes"] <= 32 << 20
            or profile["serving_models"] != ["Qwen3.8-27B-MXFP4"]):
        raise ValueError("unsupported video profile")
    raw = (directory / profile["workflow"]).read_bytes()
    workflow = json.loads(raw)
    if (not isinstance(workflow, dict) or set(workflow) != set(NODE_TYPES)
            or any(not isinstance(workflow[n], dict) or workflow[n].get("class_type") != kind
                   for n, kind in NODE_TYPES.items())):
        raise ValueError("unsupported video workflow")
    first, second = workflow["11"]["inputs"], workflow["12"]["inputs"]
    for node, start, end, noise, leftover in ((first, 0, profile["split_step"], "enable", "enable"),
                                              (second, profile["split_step"], profile["steps"], "disable", "disable")):
        if (node.get("steps") != profile["steps"] or node.get("cfg") != profile["cfg_scale"]
                or node.get("start_at_step") != start or node.get("end_at_step") != end
                or node.get("add_noise") != noise or node.get("return_with_leftover_noise") != leftover
                or node.get("sampler_name") != "euler" or node.get("scheduler") != "simple"):
            raise ValueError("unsupported video sampler settings")
    if (workflow["5"]["inputs"].get("shift") != profile["shift"] or workflow["6"]["inputs"].get("shift") != profile["shift"]
            or workflow["10"]["inputs"].get("length") != profile["frames"] or workflow["15"]["inputs"].get("fps") != profile["fps"]
            or workflow["8"]["inputs"].get("text") != "" or workflow["9"]["inputs"].get("text") != ""):
        raise ValueError("unsupported video workflow settings")
    return profile, workflow, hashlib.sha256(raw).hexdigest()


def tool_spec(profile):
    return {"name": TOOL,
            "description": ("Make a short silent video clip (5 seconds, 16 fps) from a text description and show it to the "
                            "user in this chat. It takes about one to two minutes. Call it only when the user asks for a "
                            "video, clip or animation; for a still picture use image generation instead. Write the prompt "
                            "in English and describe the subject, setting, motion, camera movement, lighting and style in "
                            "one or two detailed sentences. One clip at a time; do not call it again while one is running."),
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
            "inputSchema": {"type": "object", "additionalProperties": False, "required": ["prompt"],
                            "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 1500},
                                           "orientation": {"type": "string", "enum": sorted(profile["sizes"]),
                                                           "description": "Frame shape; landscape unless the user asks."}}}}


class VideoTool:
    def __init__(self, upstream_json, client, comfy, gateway, permissions_file, profile_dir):
        self.upstream_json, self.client = upstream_json, client
        self.comfy, self.gateway, self.permissions_file = comfy, gateway, permissions_file
        self.profile, self.workflow, self.workflow_sha256 = load_video_profile(profile_dir)
        self.active = False
        self.poll_seconds = 2.0

    def tools(self):
        return [tool_spec(self.profile)]

    def allowed(self, user_id, now=None):
        record = secret_json(self.permissions_file).get("users", {}).get(user_id)
        return bool(record and record.get("images", False)
                    and record.get("expires_at", 0) > (time.time() if now is None else now))

    def build(self, prompt, orientation, seed):
        width, height = map(int, self.profile["sizes"][orientation].split("x"))
        workflow = copy.deepcopy(self.workflow)
        workflow["8"]["inputs"]["text"] = prompt
        workflow["10"]["inputs"].update(width=width, height=height)
        workflow["11"]["inputs"]["noise_seed"] = workflow["12"]["inputs"]["noise_seed"] = seed
        return workflow, f"{width}x{height}"

    async def call(self, args, user_id, is_disconnected):
        """Return MCP tool-result content; Refusal for anything the user or model should be told."""
        if not isinstance(args, dict) or "prompt" not in args or set(args) - {"prompt", "orientation"}:
            raise Refusal(400, "invalid_tool_arguments")
        prompt, orientation = args["prompt"], args.get("orientation", "landscape")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1500 or orientation not in self.profile["sizes"]:
            raise Refusal(400, "invalid_tool_arguments")
        if not self.allowed(user_id):
            raise Refusal(403, "video_permission_denied")
        state = await self.upstream_json("GET", self.gateway + "/_service/status", timeout=3)
        if (state.get("phase") != "ready" or not state.get("images")
                or state.get("model") not in self.profile["serving_models"]):
            raise Refusal(503, "video_requires_ready_image_mode")
        if self.active:
            raise Refusal(429, "video_busy")
        self.active = True  # no await between the check and the claim
        try:
            return await self.render(prompt.strip(), orientation, is_disconnected)
        finally:
            self.active = False

    async def render(self, prompt, orientation, is_disconnected):
        seed = secrets.randbits(48)
        workflow, size = self.build(prompt, orientation, seed)
        started = time.monotonic()
        queued = await self.upstream_json("POST", self.comfy + "/prompt",
                                          data={"prompt": workflow, "client_id": "service-tools-video"}, timeout=15)
        prompt_id = queued.get("prompt_id")
        if not isinstance(prompt_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", prompt_id):
            raise Refusal(502, "video_generation_failed")
        done = False
        try:
            async with asyncio.timeout(self.profile["timeout_seconds"]):
                while True:
                    await asyncio.sleep(self.poll_seconds)
                    if await is_disconnected():
                        raise Refusal(499, "video_request_cancelled")
                    entry = (await self.upstream_json("GET", self.comfy + "/history/" + prompt_id, timeout=10)).get(prompt_id)
                    status = (entry or {}).get("status", {})
                    if status.get("completed") is not None:
                        break
            done = True
        finally:
            if not done:
                # Drop it from the queue, or stop it if it is the job running now; a picture on the
                # same backend is untouched (/interrupt with a prompt_id only stops that prompt).
                for path, body in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {"prompt_id": prompt_id})):
                    try:
                        await self.client.post(self.comfy + path, json=body, timeout=5)
                    except Exception:  # noqa: BLE001 - best effort; the timeout already decided the result
                        pass
        if status.get("status_str") != "success":
            raise Refusal(502, "video_generation_failed")
        files = (entry.get("outputs", {}).get("15") or {}).get("images") or []
        if len(files) != 1 or not isinstance(files[0], dict):
            raise Refusal(502, "video_generation_failed")
        item = files[0]
        name, subfolder = item.get("filename", ""), item.get("subfolder", "")
        if (item.get("type") != "output" or not re.fullmatch(r"wan_[0-9]{5}_\.webp", str(name))
                or subfolder != "video"):
            raise Refusal(502, "invalid_video_artifact")
        raw = bytearray()
        async with self.client.stream("GET", self.comfy + "/view",
                                      params={"filename": name, "subfolder": subfolder, "type": "output"},
                                      timeout=30) as response:
            if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "image/webp":
                raise Refusal(502, "invalid_video_artifact")
            async for part in response.aiter_bytes():
                raw.extend(part)
                if len(raw) > self.profile["max_bytes"]:
                    raise Refusal(502, "video_artifact_too_large")
        if not (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
            raise Refusal(502, "invalid_video_artifact")
        summary = {"status": "ok", "seconds": round(time.monotonic() - started, 1), "size": size,
                   "frames": self.profile["frames"], "fps": self.profile["fps"],
                   "note": "The clip is already shown to the user in the chat. Describe it in one short "
                           "sentence; do not add links or markdown images."}
        return [{"type": "text", "text": json.dumps(summary)},
                {"type": "image", "mimeType": "image/webp", "data": base64.b64encode(bytes(raw)).decode()}]
