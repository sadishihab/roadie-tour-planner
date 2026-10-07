"""A persistent Qloo harness: ONE long-running ``qloo mcp`` child process for every live call.

Why: on a 0.1 CPU host every one-off ``qloo exec`` / ``qloo api`` call took about 47 s, mostly Node
start-up. ``qloo mcp`` started once took about 42 s, after which calls took 0.6 to 4 s. The start-up
is paid once and every live call goes through the same process (JSON-RPC 2.0, one JSON object per
line on stdin/stdout).

Safety rules kept here (same as live.py):
- Failures carry fixed codes only (``HarnessError.code``). An exception message, the command line,
  the environment, the key, stderr and raw Qloo text are never logged or returned. The child's
  stderr is discarded. The child inherits the process environment (QLOO_API_KEY, QLOO_BASE_URL,
  QLOO_TRUSTED_BASE_URL); this module never reads or prints a value from it.
- A broken harness never raises into the server: the supervisor thread catches everything, restarts
  the child at most ``MAX_RESTARTS`` times with backoff, then reports ``unavailable``. Gallery
  endpoints do not touch this module.
- Every call the client makes is still charged to the per-run budget and the shared pacer by the
  caller (``LiveService``); retries go through the same ``sleep`` hook (``CallBudget.retry_sleep``).

Verified protocol facts (a real probe, 0.1 CPU): initialize takes about 40 s; tools/list returns 10
tools including qloo_where_popular (entity, within), qloo_recommend (signals ARRAY, target_type,
filter_location, include_tags, limit) and qloo_find_tags; a tools/call result has content[] items
whose ``text`` is a JSON document, and result.isError is true on a tool error. NOT verified: which
tool (if any) searches people by name, the exact key path of affinity in the documents (the mapping
below fails with ``harness_bad_response`` when it cannot find a number, it never guesses), and any
behavior after a crash on a real host.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from typing import Any, Callable, Protocol

from .qloo_client import QlooClient, QlooError
from .trim import trim_person

log = logging.getLogger("roadie.mcp")

PROTOCOL_VERSION = "2024-11-05"
STARTUP_TIMEOUT = 120.0  # initialize took about 40 s on a 0.1 CPU host
CALL_TIMEOUT = 60.0  # tool calls took 0.6 to 4 s there
MAX_RESTARTS = 3
BACKOFF = (1.0, 2.0, 4.0)  # seconds before restart 1, 2, 3
MAX_CONSECUTIVE_TIMEOUTS = 2  # a live process that answers nothing twice in a row is treated as wedged
MAX_LINE = 8_000_000  # bytes read for one JSON line
REQUIRED_TOOLS = ("qloo_where_popular", "qloo_recommend")
MAX_ITEMS = 50

# Fixed codes. LiveService maps them to its own fixed API errors.
STARTING = "harness_starting"
UNAVAILABLE = "harness_unavailable"
TIMEOUT = "harness_timeout"
TOOL_ERROR = "harness_tool_error"
BAD_RESPONSE = "harness_bad_response"


class HarnessError(QlooError):
    """A failure with a fixed code. The message is the code: never a cause, command or Qloo text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ChildProcess(Protocol):
    """What the harness needs from a child. Tests supply a scripted stand-in; production uses SubprocessChild."""

    def write_line(self, line: str) -> None: ...

    def read_line(self) -> str | None:
        """One line, or None at end of output (the process is gone)."""

    def kill(self) -> None: ...


class SubprocessChild:
    """``<qloo_bin> mcp`` with the inherited environment. stderr is discarded, never read or logged."""

    def __init__(self, qloo_bin: str = "qloo") -> None:
        self._p = subprocess.Popen(
            [qloo_bin, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=None,  # inherit: the harness reads QLOO_API_KEY and the base URLs itself
        )

    def write_line(self, line: str) -> None:
        assert self._p.stdin is not None
        self._p.stdin.write(line + "\n")
        self._p.stdin.flush()

    def read_line(self) -> str | None:
        assert self._p.stdout is not None
        line = self._p.stdout.readline(MAX_LINE)
        return line if line else None

    def kill(self) -> None:
        try:
            self._p.kill()
        except OSError:
            pass
        for stream in (self._p.stdin, self._p.stdout):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass
        try:
            self._p.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


class _Pending:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict[str, Any] | None = None
        self.error: str | None = None


class PersistentHarness:
    """Owns one ``qloo mcp`` child. Thread-safe. ``start()`` never blocks and never raises.

    status: ``starting`` (not started yet, booting, or restarting), ``ready`` (initialize finished and
    the process is alive) or ``unavailable`` (gave up after MAX_RESTARTS restarts).
    ``max_in_flight`` bounds concurrent requests on the single pipe (1 = serialized; responses are
    matched by id either way, so out-of-order answers are handled).
    """

    def __init__(
        self,
        spawn: Callable[[], ChildProcess] | None = None,
        qloo_bin: str = "qloo",
        startup_timeout: float = STARTUP_TIMEOUT,
        call_timeout: float = CALL_TIMEOUT,
        max_restarts: int = MAX_RESTARTS,
        backoff: tuple[float, ...] = BACKOFF,
        max_in_flight: int = 1,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._spawn = spawn or (lambda: SubprocessChild(qloo_bin))
        self.startup_timeout, self.call_timeout = startup_timeout, call_timeout
        self.max_restarts, self.backoff = max_restarts, backoff
        self._sleep = sleep
        self._cond = threading.Condition()
        self._state = "idle"  # idle | starting | ready | failed
        self._stopped = False
        self._child: ChildProcess | None = None
        self._pending: dict[int, _Pending] = {}
        self._next_id = 1
        self._write_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, max_in_flight))
        self._down = threading.Event()
        self._failures = 0  # consecutive failed lives of the process; a good call resets it
        self._timeouts = 0
        self._eof = False  # the live child's output ended (it exited)
        self._tools: tuple[str, ...] = ()
        self._supervisor: threading.Thread | None = None

    # ------------------------------------------------------------------ status

    @property
    def status(self) -> str:
        with self._cond:
            return {"ready": "ready", "failed": "unavailable"}.get(self._state, "starting")

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def tool_names(self) -> tuple[str, ...]:
        with self._cond:
            return self._tools

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Begin booting in the background (idempotent)."""
        with self._cond:
            if self._state != "idle" or self._stopped:
                return
            self._state = "starting"
            self._supervisor = threading.Thread(target=self._supervise, name="roadie-qloo-harness", daemon=True)
            self._supervisor.start()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            child = self._child
            self._cond.notify_all()
        self._down.set()
        if child is not None:
            child.kill()

    def _supervise(self) -> None:
        try:
            while True:
                self._down.clear()
                ok = self._boot()
                if ok:
                    self._down.wait()  # until the process exits or is judged wedged
                with self._cond:
                    if self._state == "ready":
                        self._state = "starting"  # callers wait for the restart instead of writing to a dead pipe
                self._retire_child()
                with self._cond:
                    if self._stopped:
                        return
                    self._failures += 1
                    if self._failures > self.max_restarts:
                        self._state = "failed"
                        self._cond.notify_all()
                        log.error("qloo harness gave up after %d restarts", self.max_restarts)
                        return
                    self._state = "starting"
                    delay = self.backoff[min(self._failures, len(self.backoff)) - 1] if self.backoff else 0.0
                log.warning("qloo harness restart %d of %d", self._failures, self.max_restarts)
                self._sleep(delay)
        except Exception as exc:  # never crash the server: end as unavailable
            log.error("qloo harness supervisor stopped: %s", type(exc).__name__)
            with self._cond:
                self._state = "failed"
                self._cond.notify_all()

    def _retire_child(self) -> None:
        with self._cond:
            child, self._child = self._child, None
        if child is not None:
            child.kill()
        self._fail_all_pending(UNAVAILABLE)

    def _boot(self) -> bool:
        """Spawn the child and run the handshake. True when initialize finished and tools are present."""
        try:
            child = self._spawn()
        except Exception as exc:
            log.warning("qloo harness could not start: %s", type(exc).__name__)
            return False
        with self._cond:
            if self._stopped:
                child.kill()
                return False
            self._child = child
            self._eof = False
            self._timeouts = 0
        threading.Thread(target=self._reader, args=(child,), name="roadie-qloo-reader", daemon=True).start()
        try:
            self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "roadie", "version": "0"}}, self.startup_timeout, boot=True)
            self._notify("notifications/initialized")
            listed = self._request("tools/list", {}, self.call_timeout, boot=True)
            tools = listed.get("tools") if isinstance(listed, dict) else None
            names = tuple(t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)) if isinstance(tools, list) else ()
            if not all(t in names for t in REQUIRED_TOOLS):
                log.warning("qloo harness lacks a required tool")
                return False
        except HarnessError as exc:
            log.warning("qloo harness handshake failed: %s", exc.code)
            return False
        except Exception as exc:
            log.warning("qloo harness handshake failed: %s", type(exc).__name__)
            return False
        with self._cond:
            if self._stopped or self._child is not child:
                return False
            self._tools = names
            self._state = "ready"
            self._cond.notify_all()
        log.info("qloo harness ready")
        return True

    # ---------------------------------------------------------------- transport

    def _reader(self, child: ChildProcess) -> None:
        try:
            while True:
                line = child.read_line()
                if line is None:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # not JSON: ignore (never logged)
                if not isinstance(msg, dict) or "method" in msg:
                    continue  # notifications and server-initiated requests are not needed
                with self._cond:
                    pending = self._pending.get(msg.get("id")) if isinstance(msg.get("id"), int) else None
                if pending is not None:
                    pending.response = msg
                    pending.event.set()
        except Exception as exc:
            log.warning("qloo harness reader stopped: %s", type(exc).__name__)
        with self._cond:
            current = self._child is child
            if current:
                self._eof = True  # set under the lock that registers requests, so none can miss it
                if self._state == "ready":
                    self._state = "starting"  # callers now wait for the restart
        if current:  # this child is the live one and its output ended: it exited
            self._fail_all_pending(UNAVAILABLE)
            self._down.set()

    def _fail_all_pending(self, code: str) -> None:
        with self._cond:
            pendings = list(self._pending.values())
            self._pending.clear()
        for p in pendings:
            p.error = code
            p.event.set()

    def _send(self, message: dict[str, Any]) -> None:
        with self._cond:
            child = self._child
        if child is None:
            raise HarnessError(UNAVAILABLE)
        try:
            with self._write_lock:  # one message at a time: lines never interleave
                child.write_line(json.dumps(message, separators=(",", ":")))
        except Exception:
            self._down.set()
            raise HarnessError(UNAVAILABLE) from None

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _request(self, method: str, params: dict[str, Any], timeout: float, boot: bool = False) -> Any:
        """Send one request and wait for the response with the same id. Raises HarnessError only."""
        pending = _Pending()
        with self._cond:
            rid = self._next_id
            self._next_id += 1
            if self._eof:
                raise HarnessError(UNAVAILABLE)
            self._pending[rid] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if not pending.event.wait(timeout):
                with self._cond:
                    self._pending.pop(rid, None)
                if boot:
                    self._down.set()  # a boot that does not answer in time is a failed life
                else:
                    self._note_timeout()
                raise HarnessError(TIMEOUT)
        finally:
            with self._cond:
                self._pending.pop(rid, None)
        if pending.error:
            raise HarnessError(pending.error)
        msg = pending.response or {}
        if "error" in msg or not isinstance(msg.get("result"), dict):
            raise HarnessError(TOOL_ERROR if not boot else BAD_RESPONSE)
        with self._cond:
            self._timeouts = 0
        return msg["result"]

    def _note_timeout(self) -> None:
        with self._cond:
            self._timeouts += 1
            wedged = self._timeouts >= MAX_CONSECUTIVE_TIMEOUTS
            if wedged and self._state == "ready":
                self._state = "starting"
        if wedged:
            log.warning("qloo harness answered nothing twice in a row; restarting it")
            self._down.set()

    # -------------------------------------------------------------------- calls

    def _wait_ready(self) -> None:
        self.start()  # lazy start for a caller that arrives first
        deadline = time.monotonic() + self.startup_timeout
        with self._cond:
            while self._state != "ready":
                if self._state == "failed" or self._stopped:
                    raise HarnessError(UNAVAILABLE)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise HarnessError(STARTING)
                self._cond.wait(left)

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """Call one tool and return its JSON document (a dict). Fixed-code HarnessError on any failure.

        A tool result with isError true becomes TOOL_ERROR without its message. A call that times out
        fails alone: a late answer is dropped by id and later calls are not blocked.
        """
        wait = timeout or self.call_timeout
        self._wait_ready()
        if not self._slots.acquire(timeout=wait):
            raise HarnessError(TIMEOUT)
        try:
            result = self._request("tools/call", {"name": name, "arguments": arguments}, wait)
        finally:
            self._slots.release()
        if result.get("isError") is True:
            raise HarnessError(TOOL_ERROR)
        content = result.get("content")
        text = next((c.get("text") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)), None) if isinstance(content, list) else None
        try:
            doc = json.loads(text) if text is not None else None
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            raise HarnessError(BAD_RESPONSE)
        with self._cond:
            self._failures = 0  # a good answer: the process is healthy again
        return doc


# ------------------------------------------------------------------ response mapping


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _items(doc: dict[str, Any]) -> list[Any]:
    for key in ("results", "entities"):
        if isinstance(doc.get(key), list):
            return doc[key][:MAX_ITEMS]
    raise HarnessError(BAD_RESPONSE)


def _affinity(item: dict[str, Any]) -> Any:
    query = item.get("query")
    if isinstance(query, dict) and _num(query.get("affinity")):
        return query["affinity"]
    return item.get("affinity")  # a flat variant; anything else is not guessed


def map_where_popular(doc: dict[str, Any], city: str) -> dict[str, Any]:
    """The shape ``qloo exec where_popular`` returns and ranking._load_city reads: operation, status,
    interpretation.within, and results[] cells with query.affinity and query.popularity. ``within`` is
    the city that was asked for. A cell without both numbers fails the call: nothing is guessed."""
    if doc.get("status", "ok") != "ok":
        raise HarnessError(BAD_RESPONSE)
    cells: list[dict[str, Any]] = []
    for cell in _items(doc):
        query = cell.get("query") if isinstance(cell, dict) else None
        if not (isinstance(query, dict) and _num(query.get("affinity")) and _num(query.get("popularity"))):
            raise HarnessError(BAD_RESPONSE)
        out: dict[str, Any] = {"query": {"affinity": query["affinity"], "popularity": query["popularity"]}}
        loc = cell.get("location")
        if isinstance(loc, dict):
            out["location"] = {k: loc[k] for k in ("latitude", "longitude") if _num(loc.get(k))}
            if isinstance(loc.get("geohash"), str):
                out["location"]["geohash"] = loc["geohash"][:16]
        cells.append(out)
    return {"operation": "where_popular", "status": "ok", "interpretation": {"within": city}, "results": cells}


def map_entities(doc: dict[str, Any], person: bool = False) -> list[dict[str, Any]]:
    """The list ``qloo api insights`` entities have: name, entity_id, popularity, query.affinity, plus
    type, subtype, short_description and tags when present (people never keep tags, see trim.py).
    Items missing a required field are dropped; if every item is dropped the call fails."""
    raw = _items(doc)
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        eid = item.get("entity_id", item.get("entityId"))
        affinity = _affinity(item)
        if not (isinstance(item.get("name"), str) and item["name"].strip() and isinstance(eid, str) and eid and _num(item.get("popularity")) and _num(affinity)):
            continue
        entity = {**item, "entity_id": eid, "query": {"affinity": affinity}}
        entity.pop("entityId", None)
        entity.pop("affinity", None)
        out.append(trim_person(entity) if person else entity)
    if raw and not out:
        raise HarnessError(BAD_RESPONSE)
    return out


class McpClient(QlooClient):
    """The QlooClient interface on top of one PersistentHarness; output matches LiveClient's shapes.

    search_person stays on the one-off client (``search_client``): the verified tool list does not name
    a person-search tool, so none is guessed. person_description is not needed in live runs.
    Only timeouts are retried (bounded), each retry through ``sleep`` so the caller's budget and pacer see it.
    """

    RETRIES = 1

    def __init__(
        self,
        harness: PersistentHarness,
        search_client: QlooClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = RETRIES,
    ) -> None:
        self.harness, self.search_client = harness, search_client
        self._sleep = sleep
        self.retries = min(retries, 2)

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        last: HarnessError | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(2.0 * attempt)  # charged to the run budget and paced by the caller's hook
            try:
                return self.harness.call_tool(tool, arguments)
            except HarnessError as exc:
                if exc.code != TIMEOUT:
                    raise
                last = exc
        assert last is not None
        raise last

    def search_person(self, name: str) -> list[dict[str, Any]]:
        if self.search_client is None:
            raise HarnessError(UNAVAILABLE)
        return self.search_client.search_person(name)

    def where_popular(self, entity_id: str, city: str) -> dict[str, Any]:
        return map_where_popular(self._call("qloo_where_popular", {"entity": entity_id, "within": city}), city)

    def similar(self, entity_id: str) -> list[dict[str, Any]]:
        # By entity ID in an array, never by name.
        doc = self._call("qloo_recommend", {"signals": [entity_id], "target_type": "person", "limit": 10})
        return map_entities(doc, person=True)

    def places(self, entity_id: str, city: str, category_tag: str) -> list[dict[str, Any]]:
        doc = self._call(
            "qloo_recommend",
            {"signals": [entity_id], "target_type": "place", "filter_location": city, "include_tags": [category_tag], "limit": 10},
        )
        return map_entities(doc)

    def brands(self, entity_id: str) -> list[dict[str, Any]]:
        return map_entities(self._call("qloo_recommend", {"signals": [entity_id], "target_type": "brand", "limit": 10}))

    def person_description(self, entity_id: str) -> str | None:
        raise NotImplementedError("live runs do no description lookups")

