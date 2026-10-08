# 02 - Multi-tool research/ops agent

**Goal:** an agent that searches, reads and calculates over a small doc set, cites its sources, and abstains when the docs do
not answer. Done when `python -m learning.run 02` passes 5/5 within 8 steps per question.

**Read first:** Anthropic's "Building effective agents"; Claude docs on tool use; the MCP docs (how real tools are served).
**In this repo:** `jira_implement/mcp_client.py`, `claude_code_mcp.py` (tools over MCP), `coding_agent.py` (`tool_policy`: how an
agent's tool calls are allowed or denied - the same idea as a tool error message).

**Stretch:** add a web-search-like tool with injected noise; add a "ask the user" tool and a rule for when to use it; log every
trace to a file and write the three most common failure modes you see.
