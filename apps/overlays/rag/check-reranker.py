"""Offline load-and-score check for the reviewed cross-encoder (WebUI init container).

Open WebUI 0.13.3 loads RAG_RERANKING_MODEL at startup with
sentence_transformers.CrossEncoder(path, device=DEVICE_TYPE ('cpu' here),
trust_remote_code=RAG_RERANKING_MODEL_TRUST_REMOTE_CODE, backend='torch',
activation_fn=Sigmoid()) (routers/retrieval.py get_rf). If that load fails it
only logs the error and continues with embedding-cosine scoring. This check runs
the same constructor in the same image, after verify-models.sh has checked the
file hashes, so the pod does not start with a missing or incompatible reranker.
It prints one JSON status line and exits non-zero with a fixed error token.
"""
import json
import math
import os
import sys
import tempfile

PROFILE = "ms-marco-minilm-l6-v2"
MODEL = "/rag-models/ms-marco-MiniLM-L6-v2"
MAX_TOKENS = 512


def fail(code, detail=""):
    print(json.dumps({"reranker": PROFILE, "state": "incompatible", "error": code, "detail": detail[:120]}))
    sys.exit(1)


def main():
    if os.environ.get("RAG_RERANKER_PROFILE") != PROFILE or os.environ.get("RAG_RERANKING_MODEL") != MODEL:
        fail("unreviewed_reranker_profile")
    if os.environ.get("USE_SLIM_DOCKER", "false").lower() == "true":
        fail("slim_image_has_no_local_reranker")
    # The model is a local path: no cache, hub lookup or download is needed.
    scratch = tempfile.mkdtemp(prefix="reranker-check-")
    for key in ("HOME", "HF_HOME", "XDG_CACHE_HOME", "SENTENCE_TRANSFORMERS_HOME"):
        os.environ[key] = scratch
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import torch
        from sentence_transformers import CrossEncoder
    except Exception as exc:  # noqa: BLE001 - any import failure means no local reranker
        fail("reranker_runtime_unavailable", type(exc).__name__)
    try:
        encoder = CrossEncoder(MODEL, device="cpu", trust_remote_code=False, backend="torch",
                               model_kwargs=None, activation_fn=torch.nn.Sigmoid(), local_files_only=True)
    except Exception as exc:  # noqa: BLE001
        fail("reranker_load_failed", type(exc).__name__)
    if encoder.max_seq_length != MAX_TOKENS:
        fail("reranker_max_length_changed", str(encoder.max_seq_length))
    pairs = [("Which component reorders retrieved passages?",
              "A cross-encoder scores each query and passage pair and reorders the retrieved candidates."),
             ("Which component reorders retrieved passages?",
              "The bakery opens at seven and sells rye bread.")]
    try:
        scores = [float(s) for s in encoder.predict(pairs, batch_size=int(os.environ.get("RAG_RERANKING_BATCH_SIZE", "8")),
                                                    show_progress_bar=False)]
    except Exception as exc:  # noqa: BLE001
        fail("reranker_scoring_failed", type(exc).__name__)
    if len(scores) != 2 or not all(math.isfinite(s) and 0.0 <= s <= 1.0 for s in scores):
        fail("reranker_invalid_scores")
    if not scores[0] > scores[1]:
        fail("reranker_ordering_check_failed")
    print(json.dumps({"reranker": PROFILE, "state": "active", "model": MODEL, "max_tokens": MAX_TOKENS,
                      "threads": torch.get_num_threads(), "device": "cpu", "score_kind": "sigmoid"}))


if __name__ == "__main__":
    main()
