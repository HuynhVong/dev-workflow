"""Regenerates data.csv and cases.jsonl (deterministic). The injected problems and their row ids are the answer key."""
import csv
import json
import random
from pathlib import Path

HERE = Path(__file__).parent
random.seed(7)
rows = []
for i in range(1, 201):
    rows.append({"id": i, "name": f"Customer {i}", "email": f"user{i}@example.com", "age": random.randint(18, 80),
                 "signup_date": f"2025-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"})
expect = {"duplicate_id": [], "negative_age": [], "bad_email": [], "missing_name": [], "future_signup": []}
for i in (17, 63, 150):                      # a second row re-using an id
    rows.append({**rows[i - 1], "name": f"Copy of {i}"})
    expect["duplicate_id"].append(i)
for i in (5, 88):
    rows[i - 1]["age"] = -random.randint(1, 30)
    expect["negative_age"].append(i)
for i in (23, 24, 140):
    rows[i - 1]["email"] = ["user-at-example.com", "user@@example", "@example.com"][i % 3]
    expect["bad_email"].append(i)
for i in (9, 120):
    rows[i - 1]["name"] = ""
    expect["missing_name"].append(i)
for i in (77,):
    rows[i - 1]["signup_date"] = "2031-01-15"
    expect["future_signup"].append(i)
with open(HERE / "data.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["id", "name", "email", "age", "signup_date"])
    w.writeheader()
    w.writerows(rows)
(HERE / "cases.jsonl").write_text(json.dumps({"id": "customers", "input": "data.csv", "expect": expect}) + "\n")
print("wrote data.csv and cases.jsonl")
