"""Minimal, server-agnostic LSP client — the transport + protocol layer (techne#320).

Drives a language server over stdio JSON-RPC so codex can resolve call/reference
edges *semantically* (real name-binding/type resolution) instead of tree-sitter
name-matching. Proven against pyright in the techne#319 spike; this productionizes it.

Server-agnostic on purpose: give it a launch command and a workspace root, and it
speaks LSP. Per-language wiring (pyright / gopls / rust-analyzer) lives on top
(techne#321, #324). Capability-aware — every query returns ``None`` when the server
doesn't advertise the capability, so callers can fall back to the tree-sitter graph.

    with LspClient(["pyright-langserver", "--stdio"], repo) as lsp:
        lsp.open_file(path)
        callers = lsp.incoming_calls(path, line, char)   # None if unsupported
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_HEADER_RE = re.compile(rb"Content-Length: (\d+)")


class LspError(RuntimeError):
    """A server-side error response, or a timeout waiting for one."""


@dataclass(frozen=True)
class Location:
    """A source position (LSP-native 0-based line/character)."""

    path: str
    line: int
    character: int


@dataclass(frozen=True)
class CallSite:
    """A symbol at one end of a call edge (from `callHierarchy`)."""

    name: str
    kind: int  # LSP SymbolKind
    path: str
    line: int
    character: int


def _uri(path: str | Path) -> str:
    return "file://" + str(Path(path).resolve())


def _path(uri: str) -> str:
    return uri[len("file://"):] if uri.startswith("file://") else uri


class LspClient:
    """Talk to one language server over stdio. Start it, query it, stop it.

    Thread model: a background reader drains stdout, replies to the server's own
    requests (so it never blocks), and hands responses back to the caller thread by
    id. Both threads may write to stdin, so writes are serialized under a lock.
    """

    def __init__(self, command: list[str], root: str | Path, *, timeout: float = 45.0):
        self.command = list(command)
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.capabilities: dict = {}
        self._proc: subprocess.Popen | None = None
        self._responses: dict[int, dict] = {}
        self._lock = threading.Lock()  # guards _responses + _next_id
        self._write_lock = threading.Lock()  # serializes stdin writes across threads
        self._next_id = 0

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> LspClient:
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        init = self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": _uri(self.root),
                "workspaceFolders": [{"uri": _uri(self.root), "name": self.root.name}],
                "capabilities": {
                    "textDocument": {
                        "references": {},
                        "definition": {},
                        "callHierarchy": {"dynamicRegistration": False},
                    }
                },
            },
        )
        self.capabilities = (init or {}).get("capabilities", {})
        self._notify("initialized", {})
        return self

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._request("shutdown", None, timeout=2.0)
            self._notify("exit", {})
        except Exception:
            pass
        try:
            self._proc.terminate()
        except Exception:
            pass

    def __enter__(self) -> LspClient:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # --- protocol ----------------------------------------------------------

    def _send(self, msg: dict) -> None:
        data = json.dumps(msg).encode("utf-8")
        frame = f"Content-Length: {len(data)}\r\n\r\n".encode("utf-8") + data
        with self._write_lock:
            self._proc.stdin.write(frame)
            self._proc.stdin.flush()

    def _read_message(self) -> dict | None:
        headers = b""
        while b"\r\n\r\n" not in headers:
            ch = self._proc.stdout.read(1)
            if not ch:
                return None
            headers += ch
        n = int(_HEADER_RE.search(headers).group(1))
        body = b""
        while len(body) < n:
            chunk = self._proc.stdout.read(n - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body)

    def _read_loop(self) -> None:
        while True:
            try:
                msg = self._read_message()
            except Exception:
                break
            if msg is None:
                break
            if "id" in msg and "method" in msg:
                # A server→client request. Reply so the server never blocks —
                # workspace/configuration wants one result per requested item.
                if msg["method"] == "workspace/configuration":
                    result: object = [None] * len(msg.get("params", {}).get("items", []))
                else:
                    result = None
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            elif "id" in msg:
                with self._lock:
                    self._responses[msg["id"]] = msg
            # else: a notification ($/progress, diagnostics, …) — ignore.

    def _request(self, method: str, params, timeout: float | None = None):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + (self.timeout if timeout is None else timeout)
        while time.time() < deadline:
            with self._lock:
                resp = self._responses.pop(rid, None)
            if resp is not None:
                if "error" in resp:
                    raise LspError(f"{method}: {resp['error']}")
                return resp.get("result")
            time.sleep(0.02)
        raise LspError(f"timeout waiting for {method}")

    def _notify(self, method: str, params) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # --- capabilities ------------------------------------------------------

    def supports(self, capability: str) -> bool:
        """True if the server advertised *capability* (e.g. 'callHierarchyProvider')."""
        return bool(self.capabilities.get(capability))

    # --- document sync -----------------------------------------------------

    def open_file(self, path: str | Path, *, language_id: str = "python") -> None:
        p = Path(path).resolve()
        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": _uri(p),
                    "languageId": language_id,
                    "version": 1,
                    "text": p.read_text(encoding="utf-8"),
                }
            },
        )

    # --- queries -----------------------------------------------------------
    #
    # Servers index asynchronously, so a query can return empty before analysis
    # finishes. `settle=(tries, delay)` re-runs while the result is empty — set it
    # high (e.g. (20, 2.0)) against a cold server, leave it (1, 0) once warm.

    def references(
        self, path, line, character, *, include_declaration=True, settle=(1, 0.0)
    ) -> list[Location] | None:
        if not self.supports("referencesProvider"):
            return None
        params = {
            "textDocument": {"uri": _uri(path)},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration},
        }
        res = self._settle(lambda: self._request("textDocument/references", params), settle)
        return [
            Location(_path(loc["uri"]), loc["range"]["start"]["line"], loc["range"]["start"]["character"])
            for loc in (res or [])
        ]

    def definition(self, path, line, character) -> list[Location] | None:
        if not self.supports("definitionProvider"):
            return None
        params = {"textDocument": {"uri": _uri(path)}, "position": {"line": line, "character": character}}
        res = self._request("textDocument/definition", params) or []
        res = res if isinstance(res, list) else [res]  # spec allows a single Location
        return [
            Location(_path(loc["uri"]), loc["range"]["start"]["line"], loc["range"]["start"]["character"])
            for loc in res
        ]

    def incoming_calls(self, path, line, character, *, settle=(1, 0.0)) -> list[CallSite] | None:
        """Who calls the symbol at (path, line, character). None if unsupported."""
        return self._call_hierarchy(path, line, character, "incomingCalls", "from", settle)

    def outgoing_calls(self, path, line, character, *, settle=(1, 0.0)) -> list[CallSite] | None:
        """What the symbol at (path, line, character) calls. None if unsupported."""
        return self._call_hierarchy(path, line, character, "outgoingCalls", "to", settle)

    def _call_hierarchy(self, path, line, character, direction, key, settle) -> list[CallSite] | None:
        if not self.supports("callHierarchyProvider"):
            return None
        prep = {"textDocument": {"uri": _uri(path)}, "position": {"line": line, "character": character}}
        items = self._settle(
            lambda: self._request("textDocument/prepareCallHierarchy", prep), settle
        )
        if not items:
            return []
        calls = self._request(f"callHierarchy/{direction}", {"item": items[0]}) or []
        return [self._callsite(c[key]) for c in calls]

    @staticmethod
    def _callsite(item: dict) -> CallSite:
        pos = item.get("selectionRange", item["range"])["start"]
        return CallSite(item["name"], item.get("kind", 0), _path(item["uri"]), pos["line"], pos["character"])

    @staticmethod
    def _settle(fn, settle):
        tries, delay = settle
        res = fn()
        for _ in range(max(0, tries - 1)):
            if res:
                return res
            time.sleep(delay)
            res = fn()
        return res
