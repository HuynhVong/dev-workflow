"""Run independent per-repo work side by side. Every AI call and every git fetch of one repo waits on a process or the
network, so doing the repos one after another only adds their waiting times."""
import contextvars
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

T, R = TypeVar("T"), TypeVar("R")
MAX_WORKERS = 4  # each worker can be a whole `claude` process; more only trips the plan's rate limit


def run_each(items: Iterable[T], fn: Callable[[T], R], workers: int = MAX_WORKERS) -> list[R]:
    """`fn` on every item, at most `workers` at a time. Results come back in the items' order; the first failure is raised
    once all have finished. Each call keeps the caller's context, so telemetry still attributes it to the right run and node."""
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fn, i) for i in items]
        return [f.result() for f in futures]
