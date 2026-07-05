"""Minimal, self-contained MCP (Streamable HTTP) client for Z.ai MCP servers.

BabelForge is a Python project with no Node dependency, so instead of shelling
out to the OpenClaw `zai-mcp` Node scripts we speak the MCP Streamable-HTTP
protocol directly over urllib: initialize → notifications/initialized →
tools/call. The Coding-Plan key (`GLM_API_KEY` / `ZAI_API_KEY`) authenticates
the same MCP endpoints the REST API is billed against.

Only `web_search(query)` is exposed for now — the auto-glossary uses it to
ground name/place translations in real published usage rather than letting the
model guess. Everything here fails soft: any error returns an empty result and
never raises, so translation is never blocked by a search hiccup.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

import config


def _api_key() -> str | None:
    return (os.environ.get(config.ZAI_API_KEY_ENV)
            or os.environ.get(config.ZAI_API_KEY_FALLBACK_ENV))


class _StreamableHttpMcp:
    """One-shot MCP session over Streamable HTTP. Not thread-safe; make one per
    call sequence. Responses may arrive as JSON or as SSE (text/event-stream);
    both are handled."""

    def __init__(self, server_url: str, api_key: str, timeout: float):
        self._url = server_url
        self._key = api_key
        self._timeout = timeout
        self._session_id: str | None = None

    def _post(self, payload: dict) -> list[dict]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._key}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self._url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8", "replace")
        msgs: list[dict] = []
        if "text/event-stream" in ctype:
            # SSE frames: collect every `data:` line and JSON-decode it.
            for line in body.splitlines():
                if line.startswith("data:"):
                    try:
                        msgs.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        pass
        elif body.strip():
            try:
                msgs.append(json.loads(body))
            except json.JSONDecodeError:
                pass
        return msgs

    def call_tool(self, name: str, arguments: dict) -> dict | None:
        # Handshake.
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "babelforge", "version": "0.1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized",
                    "params": {}})
        msgs = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
        for m in msgs:
            if m.get("id") == 2 and "result" in m:
                return m["result"]
        return None


def _parse_possibly_stringified(text):
    """Z.ai MCP tools often return a JSON string nested inside the text field."""
    if not isinstance(text, str):
        return text
    try:
        once = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(once, str):
        try:
            return json.loads(once)
        except json.JSONDecodeError:
            return once
    return once


def web_search(query: str, count: int = 5,
               timeout: float | None = None) -> list[dict]:
    """Return web search results as a list of {title, link, content} dicts.

    Fails soft: returns [] if the key is missing, the endpoint errors, or the
    payload can't be parsed. `query` is truncated to ~70 chars (the tool's
    recommendation) by the caller if needed.
    """
    key = _api_key()
    if not key or not query.strip():
        return []
    timeout = timeout or config.ZAI_MCP_TIMEOUT_S
    try:
        client = _StreamableHttpMcp(config.ZAI_MCP_WEB_SEARCH_URL, key, timeout)
        result = client.call_tool("web_search_prime",
                                  {"search_query": query, "count": count})
    except Exception as e:  # network, HTTP, protocol — never propagate
        sys.stderr.write(f"[mcp_client] web_search failed: {e}\n")
        return []
    if not result:
        return []
    content = result.get("content") or []
    text = content[0].get("text") if content else None
    parsed = _parse_possibly_stringified(text)
    if isinstance(parsed, dict):
        parsed = parsed.get("results") or parsed.get("data") or []
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        if isinstance(item, dict):
            out.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "content": item.get("content", ""),
            })
    return out


if __name__ == "__main__":  # quick manual probe: python mcp_client.py "<query>"
    q = " ".join(sys.argv[1:]) or "test query"
    for r in web_search(q, count=3):
        print("-", r["title"][:80])
