"""Hand GPU memory between chat's elastic KV tail and the picture/video ComfyUIs (shared mode only).

SGLang can back an extra "tail" of KV cache in memory the ComfyUIs are not using (serving patches
0031-0033, SGLANG_GFX1201_ELASTIC_KV_BASE_TOKENS). The tail is prefix-cache room only: the context
a conversation may use never changes, so chat never depends on it.

- Before a picture or clip: ask SGLang, through the gateway's /_service/kv_tail route, to release
  the tail. SGLang answers only once the memory is back on both cards. Media waits at most
  SHRINK_WAIT seconds for that answer and is otherwise refused; a chat is never interrupted.
- After IDLE_SECONDS with no picture or clip: ask every ComfyUI to unload its models, wait until
  their memory is free, then ask SGLang to take it back. The first picture after a quiet spell
  reloads its models; a run of pictures does not.

Off unless KV_TAIL_ENABLED=true. With it off nothing here calls anything.
"""
import asyncio
import contextlib
import time

import httpx

from assistant_metrics import METRICS
from boundary import Refusal, secret_json

IDLE_SECONDS = 600
SHRINK_WAIT = 15
FREE_SETTLE = 10
# ComfyUI counts as unloaded once PyTorch holds less than this on its card.
RELEASED_BYTES = 1536 * 1024 * 1024


class KvTail:
    def __init__(self, client, gateway, permissions_file, comfys, idle_seconds=IDLE_SECONDS,
                 shrink_wait=SHRINK_WAIT, free_settle=FREE_SETTLE):
        self.client, self.gateway, self.permissions_file = client, gateway, permissions_file
        self.comfys = tuple(dict.fromkeys(comfys))  # each ComfyUI once, in order
        self.idle_seconds, self.shrink_wait, self.free_settle = idle_seconds, shrink_wait, free_settle
        self.poll_seconds = 0.5
        self.lock = asyncio.Lock()
        self.jobs = 0  # pictures and clips between media() entry and exit
        self.backed = None  # None until SGLang has answered once
        self.generation = 0  # each schedule_grow() supersedes the previous timer
        self.timer = None
        self.last = None
        self._state("unknown")

    def status(self):
        return {"enabled": True, "backed": self.backed, "jobs": self.jobs, "last": self.last}

    def _state(self, state):
        METRICS.set_state("workstation_assistant_kv_tail_state", state=state)

    def _record(self, op, outcome, started, answer=None):
        elapsed = time.monotonic() - started
        METRICS.inc("workstation_assistant_kv_tail_operations_total", op=op, outcome=outcome)
        METRICS.observe("workstation_assistant_kv_tail_duration_seconds", elapsed, op=op)
        self.last = {"op": op, "outcome": outcome, "seconds": round(elapsed, 2), "at": int(time.time())}
        if answer:
            self.last.update({k: answer[k] for k in ("state", "tail_tokens", "migrated_tokens", "migrate_s",
                                                     "evicted_tail_tokens", "evicted_base_tokens", "elapsed_s") if k in answer})

    async def _sglang(self, action, timeout, **fields):
        """POST the action; return SGLang's answer, or a synthetic one when the route is unreachable."""
        key = secret_json(self.permissions_file)["service_key"]
        try:
            response = await self.client.post(self.gateway + "/_service/kv_tail", json={"action": action, **fields},
                                              headers={"authorization": "Bearer " + key}, timeout=timeout)
            answer = response.json()
        except (httpx.HTTPError, ValueError):
            return {"ok": False, "reason": "unavailable"}
        if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
            return {"ok": False, "reason": "unavailable"}
        return answer

    @contextlib.asynccontextmanager
    async def media(self):
        """Hold the cards for one picture or clip; refuses if chat cannot release the tail in time."""
        self.jobs += 1  # before any await: a grow that has not started yet will see it
        try:
            await self._shrink()
            yield
        finally:
            self.jobs -= 1
            if self.jobs == 0:
                self.schedule_grow()

    async def _shrink(self):
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.shrink_wait + 5):
                async with self.lock:
                    # Always ask, even when this process believes the tail is released: SGLang may have
                    # restarted or been grown by someone else, and a released tail answers in milliseconds.
                    answer = await self._sglang("shrink", self.shrink_wait + 10, timeout_s=self.shrink_wait)
        except TimeoutError:
            answer = {"ok": False, "reason": "timeout"}
        reason = answer.get("reason", "error")
        if answer["ok"] or reason == "disabled":
            # "disabled": SGLang runs without the tail (another arm), so nothing to release.
            self.backed = False
            self._state("unbacked" if answer["ok"] else "disabled")
            self._record("shrink", "ok" if answer["ok"] else "disabled", started, answer)
            return
        self._record("shrink", reason, started, answer)
        # The tail may still hold the memory the ComfyUI needs: never start the job.
        # busy: another grow or shrink holds SGLang; timeout: no answer in time (local or gateway).
        raise Refusal(503, "chat_memory_busy" if reason in {"busy", "timeout"} else "chat_memory_unconfirmed")

    def schedule_grow(self):
        self.generation += 1
        self.timer = asyncio.get_running_loop().create_task(self._grow_later(self.generation))

    async def _grow_later(self, generation):
        """After a quiet spell, make sure the tail is backed; then keep checking every quiet spell.

        The local `backed` flag is only a hint. SGLang's own state decides, so a tail lost to a restart
        or shrunk by someone else is grown again within one quiet spell.
        """
        await asyncio.sleep(self.idle_seconds)
        if generation != self.generation or self.jobs:
            return
        async with self.lock:
            if generation != self.generation or self.jobs:
                return
            state = (await self._sglang("status", 10)).get("state")
            if state == "backed":
                self.backed = True
                self._state("backed")
                self.schedule_grow()  # check again after the next quiet spell
                return
            if state == "disabled":
                self.backed = False
                self._state("disabled")
                return  # SGLang runs without the tail; nothing to grow
            self.backed = False if state == "unbacked" else self.backed
            started = time.monotonic()
            if not await self._free_comfys():
                self._record("free", "not_released", started)
                self.schedule_grow()  # try again after the next quiet spell
                return
            self._record("free", "ok", started)
            if self.jobs:  # a picture arrived while the models unloaded: it reloads them, chat waits
                return
            started = time.monotonic()
            answer = await self._sglang("grow", 60)
            if answer["ok"]:
                self.backed = True
                self._state("backed")
                self._record("grow", "ok", started, answer)
            else:
                self._record("grow", answer.get("reason", "error"), started, answer)
            self.schedule_grow()  # keep reconciling: retry a failed grow, or re-check a backed tail

    async def _free_comfys(self):
        """Unload every ComfyUI's models; True once each holds less than RELEASED_BYTES."""
        for comfy in self.comfys:
            try:
                await self.client.post(comfy + "/free", json={"unload_models": True, "free_memory": True}, timeout=5)
            except httpx.HTTPError:
                return False
        deadline = time.monotonic() + self.free_settle
        pending = list(self.comfys)
        while pending:
            still = []
            for comfy in pending:
                try:
                    stats = (await self.client.get(comfy + "/system_stats", timeout=5)).json()
                    held = max(int(d.get("torch_vram_total", 0)) for d in stats["devices"])
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    return False
                if held >= RELEASED_BYTES:
                    still.append(comfy)
            pending = still
            if pending:
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(self.poll_seconds)
        return True
