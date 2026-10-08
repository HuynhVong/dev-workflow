"""A worked agent loop to compare with yours AFTER you try. `model` is any callable from learning/llm.py."""

SYSTEM = ("You answer questions about a company's docs. Use the tools; never answer from memory. Use the calculator for every "
          "calculation. Finish with a short answer, then a line 'Sources: file1.md, file2.md' naming only files you used. If the "
          "docs do not contain the answer, say you cannot find it and cite nothing.")


def run_agent(question: str, model, funcs: dict, specs: list, max_steps: int = 8) -> dict:
    messages = [{"role": "user", "content": question}]
    for step in range(1, max_steps + 1):
        out = model(SYSTEM, messages, specs)
        if not out["tool_calls"]:
            text = out["text"]
            cites = []
            if "Sources:" in text:
                text, _, src = text.rpartition("Sources:")
                cites = [s.strip() for s in src.split(",") if s.strip().endswith(".md")]
            return {"answer": text.strip(), "citations": cites, "steps": step}
        messages.append({"role": "assistant", "content": out["text"], "tool_calls": out["tool_calls"]})
        for call in out["tool_calls"]:
            fn = funcs.get(call["name"])
            try:
                result = fn(**call["args"]) if fn else f"error: unknown tool {call['name']!r}"
            except Exception as e:  # noqa: BLE001 - give the model the error so it can recover
                result = f"error: {type(e).__name__}: {e}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
    return {"answer": "I could not finish within the step limit.", "citations": [], "steps": max_steps + 1}
