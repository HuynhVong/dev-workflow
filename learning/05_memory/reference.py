"""A baseline memory store: keep each user sentence as a fact, retrieve by word overlap, honour 'forget'. It does not know that
'I moved to Da Nang' replaces 'I live in Hanoi' - that is the gap exercise 05 asks you to close."""
import re


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}


class MemoryStore:
    def __init__(self):
        self.facts: list[dict] = []  # {"text", "session"}

    def add_session(self, messages: list[dict]) -> None:
        n = max((f["session"] for f in self.facts), default=-1) + 1
        for m in messages:
            if m["role"] != "user":
                continue
            text = m["text"].strip()
            forget = re.match(r"please forget (?:that |my |about )?(.*?)[,.]", text, flags=re.I)
            if forget:
                gone = _words(forget.group(1))
                self.facts = [f for f in self.facts if not (gone & _words(f["text"]))]
            else:
                self.facts.append({"text": text, "session": n})

    def context_for(self, question: str, budget_chars: int) -> str:
        q = _words(question)
        scored = sorted(((len(q & _words(f["text"])), f["session"], f["text"]) for f in self.facts), reverse=True)
        out, used = [], 0
        for score, _, text in scored:
            if score == 0 or used + len(text) + 1 > budget_chars:
                continue
            out.append(text)
            used += len(text) + 1
        return "\n".join(out)
