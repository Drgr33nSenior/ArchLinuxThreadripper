"""Conditional stdlib model of the pinned recurrent-checkpoint pool rules.

This replays a derived request menu under the reading of the pinned native
sources recorded in CLAUDE-WARM-C2-REVIEW-v1 (mamba.py c1086e3a,
unified_tree_core.py c232f023, unified_radix_cache.py f7adb0b5,
schedule_batch.py cd4283b5, batch_result_processor.py 2f5baf66). It is a
diagnostic model used only by CPU tests. Its outputs are conditional on that
reading and are not GPU evidence; physical acceptance needs a qualification run.

Modelled rules:
- The warm match key is capped at prompt length minus one, so only a stash
  frontier (2048-aligned chunk boundary) can serve an identical prompt.
- Every stash is one pool allocation; the pool evicts the least recently used
  unlocked state when full. Request main slots are pinned while running.
- The matched node is locked before the COW allocation, and the request lock
  moves to each newer stash, leaving the frontier unlocked after the final chunk.
- Per-path trimming (cap > 0) evicts the shallowest unlocked non-fork, non-leaf
  ancestors beyond the cap after every insert; cap -1 disables trimming.

Explicit uncertainty parameters, each exercised by the tests:
- finish_new_leaf: whether the finished output donates a new leaf beyond the
  prompt (a branch per request) or only releases the prompt-length lock.
- admission: "b-first" (the retained operation-200 order) or "a-first".
"""

import itertools
import math
import re

CHUNK = 2048
POOL = 19
ROOT = "root"


class Node:
    __slots__ = ("path", "pos", "parent", "children", "value", "locked", "mru")

    def __init__(self, path, pos, parent):
        self.path, self.pos, self.parent = path, pos, parent
        self.children = []
        self.value = False
        self.locked = False
        self.mru = 0
        if parent is not None:
            parent.children.append(self)


class Pool:
    def __init__(self, cap, finish_new_leaf):
        if type(cap) is not int or cap == 0 or cap < -1:
            raise ValueError("cap must be -1 (unlimited) or a positive integer")
        self.cap, self.finish_new_leaf = cap, finish_new_leaf
        self.clock = itertools.count(1)
        self.nodes = []
        self.chains = {}   # path -> {pos: Node}
        self.pinned = set()
        self.events = []

    # -- pool accounting -------------------------------------------------
    def used(self):
        return sum(1 for n in self.nodes if n.value) + len(self.pinned)

    def evict_lru(self):
        candidates = [n for n in self.nodes if n.value and not n.locked]
        if not candidates:
            raise RuntimeError("pool exhausted with only locked states")
        victim = min(candidates, key=lambda n: n.mru)
        victim.value = False
        self.events.append(("evict", victim.path, victim.pos))

    def allocate(self):
        if self.used() >= POOL:
            self.evict_lru()

    # -- tree ------------------------------------------------------------
    def chain_node(self, path, pos):
        chain = self.chains.setdefault(path, {})
        if pos not in chain:
            below = [p for p in chain if p < pos]
            parent = chain[max(below)] if below else None
            node = Node(path, pos, parent)
            chain[pos] = node
            self.nodes.append(node)
        return chain[pos]

    def trim(self, tail):
        if self.cap < 0:
            return
        holders = []
        node = tail
        while node is not None:
            if node.value:
                holders.append(node)
            node = node.parent
        excess = len(holders) - self.cap
        for node in reversed(holders):            # shallow first
            if excess <= 0 or node is tail:
                break
            if node.locked or len(node.children) != 1:
                continue                          # locked, fork or leaf preserved
            node.value = False
            excess -= 1
            self.events.append(("trim", node.path, node.pos))

    def insert(self, path, pos, lock):
        """One stash: allocate, refresh or create the value, move the lock, trim."""
        self.allocate()
        node = self.chain_node(path, pos)
        node.value = True                         # existing value: slot freed again
        node.mru = next(self.clock)
        if lock is not None:
            lock.locked = False
        node.locked = True
        self.trim(node)
        return node

    # -- requests --------------------------------------------------------
    def match(self, path, prompt):
        chain = self.chains.get(path, {})
        hits = [n for p, n in chain.items() if n.value and p <= prompt - 1]
        if not hits:
            return None
        best = max(hits, key=lambda n: n.pos)
        best.mru = next(self.clock)
        best.locked = True                        # locked before the COW allocation
        return best

    def prefill(self, rid, path, prompt):
        matched = self.match(path, prompt)
        cached = matched.pos if matched is not None else 0
        self.allocate()                           # request main slot (COW copy when warm)
        self.pinned.add(rid)
        lock, pos = matched, cached
        chunks = math.ceil((prompt - cached) / CHUNK)
        for i in range(chunks):
            pos = min(pos + CHUNK, prompt)
            if i < chunks - 1:
                lock = self.insert(path, pos, lock)
        lock = self.insert(path, prompt, lock)    # cache_unfinished_req after the final chunk
        return cached, lock

    def finish(self, rid, path, prompt, completion, lock):
        if self.finish_new_leaf:
            self.allocate()
            leaf = Node(path, prompt + completion, self.chains[path][prompt])
            leaf.value, leaf.mru = True, next(self.clock)
            self.nodes.append(leaf)
            self.trim(leaf)
        lock.locked = False
        lock.mru = next(self.clock)
        self.pinned.discard(rid)

    def group(self, requests):
        """Serialized chunked prefill in admission order, shorter completion finishes first."""
        locks, cached = {}, {}
        for rid, path, prompt, _ in requests:
            cached[rid], locks[rid] = self.prefill(rid, path, prompt)
        for rid, path, prompt, completion in sorted(requests, key=lambda r: r[3]):
            self.finish(rid, path, prompt, completion, locks[rid])
        return cached


# Retained operation-200 completion counts (CLAUDE-WARM-C2-REVIEW-v1 table). Other
# rows were never measured; the nominal value is an explicit assumption.
RETAINED_COMPLETIONS = {"long-c1-cold": 737, "long-c1-warm": 737, "long-c2-cold-a": 804,
                        "long-c2-cold-b": 538, "long-c2-warm-a": 804, "long-c2-warm-b": 538}
NOMINAL_COMPLETION = 512


def completion_for(case):
    family = "-".join(part for part in case.split("-") if not re.fullmatch(r"r\d+", part))
    return RETAINED_COMPLETIONS.get(family, NOMINAL_COMPLETION)


def replay(menu, cap, finish_new_leaf, admission="b-first"):
    """Cached-token outcome per ordinal of a hyperloom_workload.derive menu."""
    if admission not in ("b-first", "a-first"):
        raise ValueError("admission must be b-first or a-first")
    pool = Pool(cap, finish_new_leaf)
    results = {}
    for group in menu["groups"]:
        ordinals = list(group["ordinals"])
        if admission == "b-first":
            ordinals.reverse()
        requests = []
        for ordinal in ordinals:
            row = menu["bound"][ordinal]
            requests.append((ordinal, row["body"]["cache_salt"], row["input_tokens"],
                             completion_for(row["expected"]["case"])))
        results.update(pool.group(requests))
    return dict(sorted(results.items()))
