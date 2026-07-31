"""All MLX inference funnels through one persistent thread.

MLX streams are bound to the thread that created them (see mlx-audio's
realtime server for the same constraint). Ollie calls MLX from ephemeral
threads — one per utterance, one per aside — and once enough have come and
gone, an op can reference a stream whose thread no longer exists:

    transcription failed: There is no Stream(gpu, 3)

One shared, never-dying worker sidesteps that entire class of failure and
serializes GPU use between whisper and kokoro as a side effect.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")


def run(fn, *args, **kwargs):
    """Run fn on the MLX thread and return its result (exceptions propagate)."""
    return _EXECUTOR.submit(fn, *args, **kwargs).result()
