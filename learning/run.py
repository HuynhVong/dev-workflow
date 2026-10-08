"""python -m learning.run 01   (or the folder name: 01_structured_extraction)"""
import importlib.util
import sys
from pathlib import Path

from learning.evalkit import evaluate, load_cases

ROOT = Path(__file__).parent


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def exercise_dir(prefix: str) -> Path:
    found = sorted(d for d in ROOT.iterdir() if d.is_dir() and d.name.startswith(prefix))
    if not found:
        raise SystemExit(f"no exercise starting with '{prefix}' in {ROOT}")
    return found[0]


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    d = exercise_dir(args[0])
    check = load(d / "check.py", f"check_{d.name}").check
    starter = load(d / "starter.py", f"starter_{d.name}")
    print(f"exercise {d.name}")
    r = evaluate(starter.solve, load_cases(d / "cases.jsonl"), check)
    return 0 if r["rate"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
