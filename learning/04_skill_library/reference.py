"""A keyword baseline to beat. It is cheap and fast, and it breaks on paraphrases: that is the point of exercise 04."""
import re
from pathlib import Path

KEYWORDS = {
    "hr-policy": {"vacation", "leave", "payroll", "employee", "employees", "benefits", "parental"},
    "sql-help": {"query", "queries", "join", "select", "sql", "table", "database"},
    "unit-conversion": {"convert", "miles", "km", "ounces", "liters", "litres", "kg", "pounds", "celsius"},
}


class SkillLibrary:
    def __init__(self, directory):
        self.skills = {}
        for f in sorted(Path(directory).glob("*/SKILL.md")):
            text = f.read_text()
            meta = dict(re.findall(r"^(\w+): (.*)$", text.split("---")[1], flags=re.M))
            self.skills[meta["name"]] = {"description": meta["description"], "body": text.split("---", 2)[2].strip()}

    def catalog(self) -> str:
        """The cheap part: name + one line per skill. This is all the router ever needs to see."""
        return "\n".join(f"- {n}: {s['description']}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        """The expensive part: a skill's full instructions, loaded only once it is chosen."""
        return self.skills[name]["body"]

    def route(self, question: str) -> str | None:
        words = set(re.findall(r"[a-z]+", question.lower()))
        scores = {n: len(words & k) for n, k in KEYWORDS.items()}
        best = max(scores, key=scores.get)
        return best if scores[best] else None
