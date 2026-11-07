"""Development-only LLM backend: the local `claude` CLI in headless mode.

Mimics the small part of the OpenAI client the agent uses
(client.chat.completions.create -> response.choices[0].message), so the
agent loop is unchanged. Each call is stateless: the whole conversation is
sent as a transcript and the reply comes back as schema-checked JSON that
names the tool calls.

Not for the submission: the hackathon build runs on Vultr Serverless Inference.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from types import SimpleNamespace

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "Short note on what you are doing"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
                "required": ["name", "arguments"],
            },
        },
    },
    "required": ["message", "tool_calls"],
}


class _Msg:
    def __init__(self, content: str, calls: list[dict]):
        self.content = content
        self.tool_calls = [
            SimpleNamespace(id=f"call_{uuid.uuid4().hex[:8]}", type="function",
                            function=SimpleNamespace(name=c["name"], arguments=json.dumps(c.get("arguments", {}))))
            for c in calls
        ] or None

    def model_dump(self, exclude_none: bool = True) -> dict:
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": t.id, "type": "function",
                                "function": {"name": t.function.name, "arguments": t.function.arguments}}
                               for t in self.tool_calls]
        return d


def _transcript(messages: list[dict]) -> tuple[str, str]:
    system, lines = "", []
    for m in messages:
        role = m["role"]
        if role == "system":
            system = m["content"]
        elif role == "user":
            lines.append(f"### USER\n{m['content']}")
        elif role == "assistant":
            calls = [{"name": t["function"]["name"], "arguments": json.loads(t["function"]["arguments"] or "{}")}
                     for t in m.get("tool_calls", [])]
            lines.append(f"### ASSISTANT\n{m.get('content') or ''}\nTOOL CALLS: {json.dumps(calls)}")
        elif role == "tool":
            lines.append(f"### TOOL RESULT ({m['tool_call_id']})\n{m['content']}")
    return system, "\n\n".join(lines)


class ClaudeCLIClient:
    def __init__(self, timeout: int = 300):
        self.timeout = timeout
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model: str, messages: list[dict], tools: list[dict], **_):
        system, convo = _transcript(messages)
        tool_doc = json.dumps([t["function"] for t in tools], indent=1)
        system = (system + "\n\nTOOLS. You call tools only through your JSON reply: a short `message` and "
                  "`tool_calls`, a list of {name, arguments}. Every tool below IS available this way, "
                  "including record_finding and finish; listing a call in tool_calls runs it, and its "
                  "result comes back in the next TOOL RESULT. You have no other tools. Call one or more "
                  "tools every turn.\n" + tool_doc)
        prompt = convo + "\n\n### YOUR TURN (assistant). Reply with the JSON object only."
        with tempfile.TemporaryDirectory() as cwd:  # empty folder: no CLAUDE.md is picked up
            p = subprocess.run(
                ["claude", "-p", "--model", model, "--tools", "", "--strict-mcp-config",
                 "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
                 "--system-prompt", system, "--output-format", "json",
                 "--json-schema", json.dumps(TURN_SCHEMA)],
                input=prompt, capture_output=True, text=True, timeout=self.timeout, cwd=cwd)
        if p.returncode != 0:
            raise RuntimeError(f"UNAVAILABLE claude cli exit {p.returncode}: {(p.stderr or p.stdout)[:300]}")
        out = json.loads(p.stdout)
        turn = out.get("structured_output") or json.loads(out.get("result") or "{}")
        usage = out.get("usage", {})
        total = sum(usage.get(k, 0) or 0 for k in
                    ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        return SimpleNamespace(choices=[SimpleNamespace(message=_Msg(turn.get("message", ""), turn.get("tool_calls", [])))],
                               usage=SimpleNamespace(total_tokens=total))
