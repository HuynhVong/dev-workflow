"""Provided tools for the research agent: search the corpus, read a file, calculate. Plain functions + JSON-schema specs."""
import ast
import operator
import re
from pathlib import Path

CORPUS = Path(__file__).parent / "corpus"
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.Mod: operator.mod}


def search(query: str) -> str:
    """Files whose lines best match the query words, with the matching lines."""
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    hits = []
    for f in sorted(CORPUS.glob("*.md")):
        for line in f.read_text().splitlines():
            n = len(words & set(re.findall(r"[a-z0-9]+", line.lower())))
            if n:
                hits.append((n, f.name, line.strip()))
    hits.sort(key=lambda h: -h[0])
    return "\n".join(f"{name}: {line}" for _, name, line in hits[:6]) or "no matches"


def read_file(name: str) -> str:
    p = (CORPUS / Path(name).name)
    return p.read_text() if p.is_file() else f"error: no such file {name!r}; files: {[f.name for f in sorted(CORPUS.glob('*.md'))]}"


def calculator(expression: str) -> str:
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("only numbers and + - * / ** % are allowed")
    try:
        return str(round(ev(ast.parse(expression, mode="eval").body), 6))
    except Exception as e:  # noqa: BLE001 - a tool error is a message for the model, never a crash
        return f"error: {e}"


FUNCS = {"search": search, "read_file": read_file, "calculator": calculator}
SPECS = [
    {"name": "search", "description": "Search the company docs. Returns matching lines with their file names.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_file", "description": "Read a whole doc file by name, e.g. pricing.md.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "calculator", "description": "Evaluate an arithmetic expression, e.g. 3*20*12*0.85. Use it for every calculation.",
     "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}},
]
