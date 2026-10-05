"""Background run manager for `devflow ui`: drives runs on worker threads so closing the browser never stops a run.

- New runs start while fewer than `max_parallel_runs` runs are RUNNING (in this process or another, e.g. the CLI);
  the rest stay PENDING in a queue and start when a slot frees up. A run waiting for you holds no slot.
- Answers, resumes and aborts go through the same Session methods the CLI uses, so the same rules apply.
- A ticker keeps the heartbeat of in-process runs fresh during long nodes (a Claude Code session can take a while).
"""
import threading
import time
from collections import deque

from ..jira_implement.runner import Session


class RunManager:
    def __init__(self, session: Session, max_parallel: int | None = None, tick_s: float = 2.0):
        self.session = session
        self.max_parallel = max_parallel or session.ws.max_parallel_runs
        self.threads: dict[str, threading.Thread] = {}
        self.queue: deque[str] = deque()
        self.lock = threading.Lock()
        self.tick_s = tick_s
        self._stop = threading.Event()
        self._requeue_pending()
        self._ticker = threading.Thread(target=self._tick, name="devflow-ticker", daemon=True)
        self._ticker.start()

    # ------------------------------------------------------------------ public
    def start(self, workflow: str, values: dict) -> str:
        run_id, _ = self.session.create(workflow, values)
        with self.lock:
            self.queue.append(run_id)
        self._pump()
        return run_id

    def answer(self, run_id: str, ans: dict) -> None:
        self._spawn(run_id, lambda: self.session.answer(run_id, ans))

    def resume(self, run_id: str, reopen: bool = False) -> None:
        self._spawn(run_id, lambda: self.session.resume(run_id, reopen=reopen))

    def abort(self, run_id: str, note: str = "") -> str:
        """Waiting, failed or queued runs abort now; a run working in this process stops at the next node boundary."""
        with self.lock:
            if run_id in self.queue:
                self.queue.remove(run_id)
            alive = self.is_active(run_id)
        if alive:
            self.session.request_cancel(run_id, note)
            return "ABORTING"
        return self.session.abort(run_id, note)

    def is_active(self, run_id: str) -> bool:
        t = self.threads.get(run_id)
        return bool(t and t.is_alive())

    def queued(self) -> list[str]:
        with self.lock:
            return list(self.queue)

    def slots(self) -> dict:
        running = self._running_count()
        return {"max": self.max_parallel, "running": running, "queued": len(self.queue)}

    def shutdown(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ internals
    def _running_count(self) -> int:
        in_process = sum(1 for t in self.threads.values() if t.is_alive())
        elsewhere = sum(1 for r in self.session.store.all_runs() if r["status"] == "RUNNING" and not r["stale"]
                        and not self.is_active(r["run_id"]))
        return in_process + elsewhere

    def _spawn(self, run_id: str, fn) -> None:
        with self.lock:
            if self.is_active(run_id):
                raise RuntimeError(f"{run_id} is already running")

            def target():
                try:
                    fn()
                finally:
                    self._pump()

            t = threading.Thread(target=target, name=f"devflow-{run_id}", daemon=True)
            self.threads[run_id] = t
            t.start()

    def _pump(self) -> None:
        """Start queued runs while slots are free."""
        while True:
            with self.lock:
                if not self.queue or self._running_count() >= self.max_parallel:
                    return
                run_id = self.queue.popleft()
            run = self.session.store.run(run_id)
            if not run or run["status"] != "PENDING":
                continue
            self.session.store.heartbeat(run_id, "starting")  # RUNNING now, so it counts against the slots at once
            self._spawn(run_id, lambda rid=run_id: self.session.drive(rid, self.session.store.inputs(rid)))

    def _requeue_pending(self) -> None:
        """Runs created but never started (e.g. queued before a restart) go back in the queue, oldest first."""
        pending = [r for r in self.session.store.all_runs() if r["status"] == "PENDING" and self.session.store.inputs(r["run_id"])]
        for r in sorted(pending, key=lambda r: r["created_at"]):
            try:
                if self.session.graph_for(r["workflow"]).get_state(self.session.cfg(r["run_id"])).created_at is None:
                    self.queue.append(r["run_id"])
            except Exception:  # noqa: BLE001 - an unknown or broken workflow stays PENDING; the UI shows it
                continue

    def _tick(self) -> None:
        while not self._stop.wait(self.tick_s):
            for run_id, t in list(self.threads.items()):
                if t.is_alive():
                    self.session.store.touch(run_id)
            try:
                self._pump()
            except Exception:  # noqa: BLE001 - the ticker must survive anything
                pass

    def wait_idle(self, timeout: float = 30) -> bool:
        """Tests: wait until no run thread is alive and the queue is empty."""
        end = time.time() + timeout
        while time.time() < end:
            if not any(t.is_alive() for t in self.threads.values()) and not self.queue:
                return True
            time.sleep(0.05)
        return False
