"""Prefetch the two revision-pinned MiniLM models into the HF cache.

Used at Docker build time (HF_HOME baked into the image) so the container
never touches huggingface.co at runtime; also runnable locally to pre-warm.
Fails loudly: a Null fallback or a download error exits 1 — a silent no-op
prefetch defeats its own purpose.
"""
import sys

from src.retrieval.embedder import NullEmbedder, get_embedder
from src.signals.rerank import NullScorer, get_scorer


def main() -> int:
    emb = get_embedder()
    if isinstance(emb, NullEmbedder):
        print("FAIL: embedder degraded to NullEmbedder — install signals[rerank]", file=sys.stderr)
        return 1
    vec = emb.embed(["prefetch probe"])
    if not vec or len(vec[0]) != 384:
        print(f"FAIL: embedder returned {vec!r}", file=sys.stderr)
        return 1
    print(f"embedder ok: {getattr(emb, 'model', emb.__class__.__name__)}")

    rr = get_scorer()
    if isinstance(rr, NullScorer):
        print("FAIL: reranker degraded to NullScorer — install signals[rerank]", file=sys.stderr)
        return 1
    # get_scorer() alone downloads nothing: the hf_hub fetch is lazy inside
    # _ensure_loaded(), triggered only by score_pairs() (rerank.py:76-79).
    # A type-only check would bake the embedder and silently skip the
    # reranker model — the exact failure this script exists to prevent.
    scores = rr.score_pairs("prefetch probe", [("prefetch probe", "probe summary")])
    if not scores:
        print(f"FAIL: scorer returned {scores!r}", file=sys.stderr)
        return 1
    print(f"scorer ok: {type(rr).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
