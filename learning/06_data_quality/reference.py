"""A hand-written rule set: the deterministic baseline. An LLM should PROPOSE rules like these, not read 200,000 rows."""
import csv
import datetime as dt
import re
from collections import Counter

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def find_issues(path: str, today: dt.date = dt.date(2026, 10, 8)) -> dict[str, list[int]]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    counts = Counter(r["id"] for r in rows)
    out = {"duplicate_id": sorted({int(i) for i, n in counts.items() if n > 1}), "negative_age": [], "bad_email": [],
           "missing_name": [], "future_signup": []}
    for r in rows:
        i = int(r["id"])
        if int(r["age"]) < 0:
            out["negative_age"].append(i)
        if not EMAIL.match(r["email"]):
            out["bad_email"].append(i)
        if not r["name"].strip():
            out["missing_name"].append(i)
        if dt.date.fromisoformat(r["signup_date"]) > today:
            out["future_signup"].append(i)
    return out
