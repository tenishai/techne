"""A minimal fake LSP server for testing LspClient hermetically (techne#320).

Speaks just enough of the protocol: the initialize handshake (advertising
references/definition/callHierarchy), a server→client `workspace/configuration`
request during init (to exercise the client's auto-reply), and canned results for
references / prepareCallHierarchy / incoming+outgoing calls / definition.

Set env `FAKE_LSP_NO_CAPS=1` to advertise NO capabilities (tests graceful degrade).
"""
import json
import os
import re
import sys

_RE = re.compile(rb"Content-Length: (\d+)")


def read():
    headers = b""
    while b"\r\n\r\n" not in headers:
        ch = sys.stdin.buffer.read(1)
        if not ch:
            return None
        headers += ch
    n = int(_RE.search(headers).group(1))
    body = b""
    while len(body) < n:
        body += sys.stdin.buffer.read(n - len(body))
    return json.loads(body)


def send(msg):
    data = json.dumps(msg).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    sys.stdout.buffer.flush()


def loc(uri, line, char, endchar):
    return {"uri": uri, "range": {"start": {"line": line, "character": char},
                                  "end": {"line": line, "character": endchar}}}


def item(name, uri, line, char):
    r = {"start": {"line": line, "character": char}, "end": {"line": line, "character": char + len(name)}}
    return {"name": name, "kind": 12, "uri": uri, "range": r, "selectionRange": r}


def main():
    no_caps = os.environ.get("FAKE_LSP_NO_CAPS") == "1"
    while True:
        msg = read()
        if msg is None:
            break
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            # Fire a server→client request first: the client MUST reply or a real
            # server would stall. We don't block on it here, but the flow exercises it.
            send({"jsonrpc": "2.0", "id": 9001, "method": "workspace/configuration",
                  "params": {"items": [{"section": "python"}]}})
            caps = {} if no_caps else {
                "referencesProvider": True,
                "definitionProvider": True,
                "callHierarchyProvider": True,
            }
            send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": caps}})
        elif method == "textDocument/references":
            send({"jsonrpc": "2.0", "id": mid, "result": [
                loc("file:///repo/a.py", 10, 4, 13),
                loc("file:///repo/b.py", 20, 8, 17),
            ]})
        elif method == "textDocument/definition":
            send({"jsonrpc": "2.0", "id": mid, "result": [loc("file:///repo/a.py", 0, 4, 10)]})
        elif method == "textDocument/prepareCallHierarchy":
            send({"jsonrpc": "2.0", "id": mid, "result": [item("target", "file:///repo/a.py", 0, 4)]})
        elif method == "callHierarchy/incomingCalls":
            send({"jsonrpc": "2.0", "id": mid, "result": [
                {"from": item("caller_fn", "file:///repo/c.py", 5, 4), "fromRanges": []}
            ]})
        elif method == "callHierarchy/outgoingCalls":
            send({"jsonrpc": "2.0", "id": mid, "result": [
                {"to": item("callee_fn", "file:///repo/d.py", 8, 4), "fromRanges": []}
            ]})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": mid, "result": None})
        elif method == "exit":
            break
        # notifications (initialized, didOpen, …): ignore


if __name__ == "__main__":
    main()
