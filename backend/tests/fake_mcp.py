"""A scripted stand-in for the `qloo mcp` child: same line-delimited JSON-RPC, no process, no network.

Documents come from the invented fixtures in tests/synthetic (wrapped the way a tools/call result wraps
a JSON document in content[].text). Nothing here is real Qloo data.
"""

import json
import queue
import threading
import time

from roadie.mcp_client import ChildProcess  # noqa: F401  (the protocol the fake implements)
from roadie.qloo_client import FixtureClient
from synthetic import JUNE, SYNTH_DIR

TOOLS = ["qloo_where_popular", "qloo_recommend", "qloo_find_tags"]


def wrap(doc):
    return {"content": [{"type": "text", "text": json.dumps(doc)}], "isError": False}


def tool_error(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def entities_doc(items):
    return {"operation": "recommend", "result_count": len(items), "results": items}


class FixtureServer:
    """Answers tools/call from the synthetic June Marlowe fixtures. Subclass or set hooks to misbehave."""

    def __init__(self, tools=None):
        self.fx = FixtureClient(JUNE, SYNTH_DIR)
        self.tools = TOOLS if tools is None else tools
        self.calls = []  # (tool, arguments) in arrival order
        self.lock = threading.Lock()

    def tool_result(self, name, args):
        if name == "qloo_where_popular":
            return wrap(self.fx.where_popular(args["entity"], args["within"]))
        if name == "qloo_recommend":
            kind = args["target_type"]
            if kind == "person":
                return wrap(entities_doc(self.fx.similar(args["signals"][0])))
            if kind == "place":
                return wrap(entities_doc(self.fx.places(args["signals"][0], args["filter_location"], args["include_tags"][0])))
            if kind == "brand":
                return wrap(entities_doc(self.fx.brands(args["signals"][0])))
        return tool_error("Unknown tool")

    def handle(self, msg, child):
        """Return the result for a request, or None to stay silent. Runs on the child's worker thread."""
        method = msg.get("method")
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
        if method == "tools/list":
            return {"tools": [{"name": t} for t in self.tools]}
        if method == "tools/call":
            p = msg["params"]
            with self.lock:
                self.calls.append((p["name"], p["arguments"]))
            return self.tool_result(p["name"], p["arguments"])
        return {}


class FakeChild:
    """Implements write_line / read_line / kill. Incoming lines are handled on a worker thread so a
    handler may block (a gate) without blocking the writer."""

    def __init__(self, server=None):
        self.server = server or FixtureServer()
        self.received = []  # every parsed message, in order
        self.killed = False
        self._in = queue.Queue()
        self._out = queue.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def write_line(self, line):
        if self.killed:
            raise BrokenPipeError
        self._in.put(line)

    def read_line(self):
        item = self._out.get()
        if item is None:
            self._out.put(None)  # stay at end of output
        return item

    def kill(self):
        self.killed = True
        self._in.put(None)
        self._out.put(None)

    def exit(self):
        """The process dies on its own: its output ends."""
        self._out.put(None)

    def reply(self, rid, result):
        self._out.put(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")

    def _work(self):
        while True:
            line = self._in.get()
            if line is None:
                return
            msg = json.loads(line)
            self.received.append(msg)
            if "id" not in msg:
                continue
            result = self.server.handle(msg, self)
            if result is not None:
                self.reply(msg["id"], result)


class Spawner:
    """A spawn callable that hands out the given children in order; running out is a spawn failure."""

    def __init__(self, *children):
        self.children = list(children)
        self.count = 0

    def __call__(self):
        self.count += 1
        if not self.children:
            raise OSError("no more fake children")
        nxt = self.children.pop(0)
        return nxt() if callable(nxt) else nxt


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False
