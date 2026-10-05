from collections import defaultdict, deque


class FakeLLM:
    """Returns queued responses per schema type and records every prompt."""

    def __init__(self, responses: dict):
        self.queues = defaultdict(deque, {k: deque(v if isinstance(v, list) else [v]) for k, v in responses.items()})
        self.calls: list[tuple[type, str]] = []

    def structured(self, system, prompt, schema):
        self.calls.append((schema, prompt))
        q = self.queues[schema]
        if not q:
            raise AssertionError(f"No fake response queued for {schema.__name__}")
        return q.popleft() if len(q) > 1 else q[0]
