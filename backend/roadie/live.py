"""Live mode: a reduced Qloo run for one visitor-chosen comedian, with abuse limits.

Off unless the operator enables it (ROADIE_LIVE=1) on a host that has the qloo CLI set up. The
flow is search (one Qloo call, the visitor picks a match), then plan (about 15 calls) which writes
the results into a temporary folder (under the private data dir) in the fixtures/<slug>/ layout, runs the same ``plan_tour`` on
it, narrates with the template only (never a model), and deletes the folder.

Safety rules kept here:
- Error responses and logs carry fixed codes only: never an exception message, a subprocess
  command line, raw Qloo output or an environment value.
- Qloo text is untrusted: strings are length-capped before they leave the server.
- Every Qloo call, retries included, is charged to a per-run budget (hard cap), and calls are paced
  client-side so they never exceed the hackathon key's rate (5 per second; Roadie stays at 4).
- Runs are capped per IP per hour, globally per day, and per calendar month (the key has a monthly quota).
- The hackathon key is deactivated after the event: past ROADIE_LIVE_UNTIL, or when Qloo stops answering,
  live mode answers with a clear message and the gallery keeps working.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import secrets
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .cities import CITIES
from .narrator import TemplateNarrator, narrate
from .pipeline import COMEDY_CLUB_TAG, plan_tour
from .qloo_client import FixtureClient, LiveClient, QlooClient, QlooError, build_chosen, city_slug, slugify
from .ranking import _load_city
from .settings import Settings
from .trim import trim_payload

log = logging.getLogger("roadie.live")

REDUCED_CITIES = ["New York, NY", "Los Angeles, CA", "Chicago, IL", "Austin, TX", "Atlanta, GA", "Seattle, WA"]
assert len(REDUCED_CITIES) == 6 and set(REDUCED_CITIES) <= set(CITIES)

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{0,58}[A-Za-z.]$")  # 2 to 60 characters
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_TEXT = 160  # cap for any Qloo-derived string before it enters a plan
MAX_RESPONSE_TEXT = 2000  # backstop on every string in a final result
MAX_CANDIDATES = 5
CANDIDATE_TTL = 3600
MAX_CANDIDATES_STORED = 500
MAX_CACHED_RESULTS = 200
MAX_LIVE_WORKERS = 6  # hard maximum for ROADIE_LIVE_WORKERS

MESSAGES = {
    "live_disabled": "Live search is turned off on this server. The demo gallery is still available.",
    "invalid_request": "The request was not valid.",
    "unknown_entity": "Search for the comedian first, then pick one of the matches.",
    "not_found": "Not found.",
    "ip_limit": "You have reached the hourly limit for live runs. Try again later.",
    "daily_limit": "The server has reached its daily limit for live runs. Try again tomorrow. The demo gallery is still available.",
    "monthly_limit": "The server has reached its monthly limit for live runs. The demo gallery is still available.",
    "live_ended": "Live search has ended: the Qloo access key used for the hackathon is no longer active. The demo gallery is still available.",
    "search_limit": "Too many searches. Try again later.",
    "too_many_jobs": "Several live runs are already in progress. Try again in a few minutes.",
    "qloo_unavailable": "Live Qloo data is not available right now (the service could not be reached, or the hackathon key is no longer active). The demo gallery is still available.",
    "call_cap_reached": "The run hit its Qloo call limit and was stopped.",
    "plan_failed": "The plan could not be built from the Qloo results.",
    "internal_error": "Something went wrong.",
}


class LiveError(Exception):
    """A failure with a fixed code; the message is looked up, never taken from the cause."""

    def __init__(self, status: int, code: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.retry_after = retry_after

    @property
    def message(self) -> str:
        return MESSAGES.get(self.code, MESSAGES["internal_error"])


class BudgetExceeded(Exception):
    pass


class RatePacer:
    """Spaces Qloo calls so they never exceed ``per_second``, across every run and search.

    The hackathon key allows 5 requests per second; the default pace is 4 (one call at least
    0.25 s after the previous one, plus a small margin). ``per_second`` of 0 turns pacing off.
    """

    MARGIN = 1.05

    def __init__(
        self,
        per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = (self.MARGIN / per_second) if per_second > 0 else 0.0
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until this call may start; slots are handed out in order, so threads queue up."""
        if not self.interval:
            return
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            self._sleep(start - now)


class CallBudget:
    """Counts every Qloo call. LiveClient's retry sleeps are charged too, so the cap is hard."""

    def __init__(self, cap: int, pacer: RatePacer | None = None) -> None:
        self.cap = cap
        self.count = 0
        self.pacer = pacer
        self._lock = threading.Lock()  # workers charge concurrently; the check and the increment are one step

    def charge(self) -> None:
        with self._lock:
            if self.count >= self.cap:
                raise BudgetExceeded
            self.count += 1

    def retry_sleep(self, seconds: float) -> None:
        self.charge()
        time.sleep(seconds)
        if self.pacer:
            self.pacer.wait()  # a retry is a call too: it takes a slot in the shared pace


def _settle(future: Future, warnings: list[str]) -> Any:
    """Wait for one call's result, in the caller's fixed order; a failed call becomes a warning."""
    data, warning = future.result()
    if warning:
        warnings.append(warning)
    return data


class PacedClient(QlooClient):
    """Wraps a QlooClient so every call first takes a slot from the shared pacer."""

    def __init__(self, inner: QlooClient, pacer: RatePacer) -> None:
        self.inner, self.pacer = inner, pacer

    def search_person(self, name: str) -> list[dict[str, Any]]:
        self.pacer.wait()
        return self.inner.search_person(name)

    def where_popular(self, entity_id: str, city: str) -> dict[str, Any]:
        self.pacer.wait()
        return self.inner.where_popular(entity_id, city)

    def similar(self, entity_id: str) -> list[dict[str, Any]]:
        self.pacer.wait()
        return self.inner.similar(entity_id)

    def places(self, entity_id: str, city: str, category_tag: str) -> list[dict[str, Any]]:
        self.pacer.wait()
        return self.inner.places(entity_id, city, category_tag)

    def brands(self, entity_id: str) -> list[dict[str, Any]]:
        self.pacer.wait()
        return self.inner.brands(entity_id)

    def person_description(self, entity_id: str) -> str | None:
        self.pacer.wait()
        return self.inner.person_description(entity_id)


class MonthlyBudget:
    """Live plan runs per calendar month (UTC), kept in memory and, when the data dir exists, in
    ``<data_dir>/live_budget.json`` so a restart does not forget what was spent. Counts runs only."""

    FILENAME = "live_budget.json"

    def __init__(self, limit: int, data_dir: Path | None, now: Callable[[], datetime]) -> None:
        self.limit = limit
        self._now = now
        self._path = (data_dir / self.FILENAME) if data_dir is not None and data_dir.is_dir() else None
        self._month, self._used = "", 0
        self._load()

    def _key(self) -> str:
        return self._now().strftime("%Y-%m")

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("month"), str) and type(data.get("runs")) is int:
                self._month, self._used = data["month"], max(0, data["runs"])
        except (OSError, ValueError):
            pass

    def _roll(self) -> None:
        if self._month != self._key():
            self._month, self._used = self._key(), 0

    def remaining(self) -> int:
        self._roll()
        return max(0, self.limit - self._used)

    def record(self) -> None:
        self._roll()
        self._used += 1
        if self._path is not None:
            try:
                self._path.write_text(json.dumps({"month": self._month, "runs": self._used}), encoding="utf-8")
            except OSError:
                log.warning("could not save the monthly live budget")


ClientFactory = Callable[[Callable[[float], None]], QlooClient]


def default_client_factory(settings: Settings) -> ClientFactory:
    return lambda sleep: LiveClient(qloo_bin=settings.qloo_bin, sleep=sleep)


def cap_strings(value: Any, limit: int = MAX_TEXT) -> Any:
    """Qloo text is untrusted: bound every string's length (whitespace collapsed)."""
    if isinstance(value, str):
        value = " ".join(value.split())
        return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
    if isinstance(value, list):
        return [cap_strings(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: cap_strings(v, limit) for k, v in value.items()}
    return value


def client_ip(direct: str | None, forwarded_for: str | None, trust_proxy: bool, hops: int = 1) -> str:
    """The direct peer, unless ROADIE_TRUST_PROXY: then the X-Forwarded-For entry ``hops`` places from
    the right end. Each trusted proxy appends the address it saw to the END of the list, so the
    left-hand entries are whatever the client sent and are never used. A header with fewer entries
    than hops, or an unusable entry, falls back to the direct peer."""
    if trust_proxy and forwarded_for and hops >= 1:
        entries = [e.strip() for e in forwarded_for.split(",")]
        if len(entries) >= hops:
            try:
                return str(ipaddress.ip_address(entries[-hops]))
            except ValueError:
                pass
    return direct or "unknown"


class SlidingWindow:
    def __init__(self, limit: int, window: float, clock: Callable[[], float]) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        cutoff = self.clock() - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits and len(self._hits) > 10_000:  # bound memory under many distinct keys
            self._hits.pop(key, None)
            return deque()
        return hits

    def retry_after(self, key: str) -> int | None:
        """None if one more hit is allowed, else seconds until it would be."""
        hits = self._prune(key)
        if len(hits) < self.limit:
            return None
        return max(1, int(hits[0] + self.window - self.clock()) + 1)

    def remaining(self, key: str) -> int:
        return max(0, self.limit - len(self._prune(key)))

    def record(self, key: str) -> None:
        self._prune(key)
        self._hits.setdefault(key, deque()).append(self.clock())


class Job:
    def __init__(self, entity_id: str, expires: float) -> None:
        self.id = secrets.token_urlsafe(18)
        self.entity_id = entity_id
        self.expires = expires
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.done = False
        self._cond = threading.Condition()

    def emit(self, event: str, data: dict[str, Any]) -> None:
        with self._cond:
            self.events.append((event, data))
            self._cond.notify_all()

    def finish(self) -> None:
        with self._cond:
            self.done = True
            self._cond.notify_all()

    def stream(self, clock: Callable[[], float], keepalive: float = 15.0) -> Iterator[str]:
        """Server-sent events. Yields ': keep-alive' comments while idle; ends when the job is done."""
        i = 0
        while True:
            batch: list[tuple[str, dict[str, Any]]] = []
            idle = False
            with self._cond:
                if i < len(self.events):
                    batch = self.events[i:]
                elif self.done:
                    return
                else:
                    idle = not self._cond.wait(timeout=keepalive)
            for event, data in batch:
                i += 1
                yield f"id: {i}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if idle:
                if clock() > self.expires:
                    return
                yield ": keep-alive\n\n"


class LiveService:
    def __init__(
        self,
        settings: Settings,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        pacer: RatePacer | None = None,
    ) -> None:
        self.s = settings
        self.pacer = pacer or RatePacer(settings.max_qloo_per_second)
        factory = client_factory or default_client_factory(settings)
        self.factory: ClientFactory = lambda sleep: PacedClient(factory(sleep), self.pacer)
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._monthly = MonthlyBudget(settings.monthly_runs, settings.data_dir, self.now)
        self._lock = threading.Lock()
        self._ip_runs = SlidingWindow(settings.ip_runs_per_hour, 3600, clock)
        self._global_runs = SlidingWindow(settings.global_runs_per_day, 86400, clock)
        self._ip_searches = SlidingWindow(settings.ip_searches_per_hour, 3600, clock)
        self._global_searches = SlidingWindow(settings.global_searches_per_day, 86400, clock)
        self._candidates: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache: dict[str, tuple[float, list[tuple[str, dict[str, Any]]]]] = {}
        self._inflight: dict[str, Job] = {}
        self._jobs: dict[str, Job] = {}
        self._running = 0

    # ------------------------------------------------------------------ status

    def ended(self) -> bool:
        """True once the day after ROADIE_LIVE_UNTIL has begun (UTC): the hackathon key is gone."""
        if not self.s.live_until:
            return False
        return self.now().date() > date.fromisoformat(self.s.live_until)

    def budget_remaining(self) -> int:
        """Live plan runs still allowed right now: the smaller of today's and this month's allowance."""
        with self._lock:
            return min(self._global_runs.remaining("*"), self._monthly.remaining())

    # ------------------------------------------------------------------ search

    def search(self, ip: str, name: str) -> list[dict[str, Any]]:
        if not NAME_RE.fullmatch(name):
            raise LiveError(422, "invalid_request")
        with self._lock:
            wait = self._ip_searches.retry_after(ip) or self._global_searches.retry_after("*")
            if wait:
                raise LiveError(429, "search_limit", wait)
            self._ip_searches.record(ip)
            self._global_searches.record("*")
        try:
            raw = self.factory(CallBudget(3, self.pacer).retry_sleep).search_person(name)  # LiveClient bounds this at 1 + 2 retries
        except Exception as exc:
            log.warning("live search failed: %s", type(exc).__name__)
            raise LiveError(502, "qloo_unavailable") from None
        found: list[dict[str, Any]] = []
        for e in raw if isinstance(raw, list) else []:
            if not isinstance(e, dict):
                continue
            eid, nm, pop = e.get("entity_id"), e.get("name"), e.get("popularity")
            if not (isinstance(eid, str) and UUID_RE.fullmatch(eid) and isinstance(nm, str) and nm.strip()):
                continue
            if isinstance(pop, bool) or not isinstance(pop, (int, float)):
                continue  # popularity is needed to classify comics to bill
            desc = (e.get("properties") or {}).get("short_description") if isinstance(e.get("properties"), dict) else None
            found.append(
                {
                    "entity_id": eid.upper(),
                    "name": cap_strings(nm, 100),
                    "description": cap_strings(desc) if isinstance(desc, str) and desc.strip() else None,
                    "popularity": round(float(pop), 4),
                }
            )
            if len(found) >= MAX_CANDIDATES:
                break
        now = self.clock()
        with self._lock:
            for c in found:
                self._candidates[c["entity_id"]] = (now, c)
            for key in [k for k, (t, _) in self._candidates.items() if now - t > CANDIDATE_TTL]:
                del self._candidates[key]
            while len(self._candidates) > MAX_CANDIDATES_STORED:
                del self._candidates[next(iter(self._candidates))]
        return found

    # -------------------------------------------------------------------- plan

    def start(self, ip: str, entity_id: str) -> tuple[Job, bool]:
        """Begin a run (or reuse one). Returns (job, reused)."""
        if not UUID_RE.fullmatch(entity_id):
            raise LiveError(422, "invalid_request")
        entity_id = entity_id.upper()
        now = self.clock()
        with self._lock:
            self._expire(now)
            entry = self._candidates.get(entity_id)
            if entry is None:
                raise LiveError(404, "unknown_entity")
            if entity_id in self._inflight:  # same comedian already running: share it, no new calls
                return self._inflight[entity_id], True
            cached = self._cache.get(entity_id)
            if cached is not None:  # within the cache window: replay, no new Qloo calls
                job = Job(entity_id, now + self.s.job_ttl_seconds)
                job.events = list(cached[1])
                job.done = True
                self._jobs[job.id] = job
                return job, True
            if self._running >= self.s.max_running_jobs:
                raise LiveError(429, "too_many_jobs", 30)
            wait = self._ip_runs.retry_after(ip)
            if wait:
                raise LiveError(429, "ip_limit", wait)
            wait = self._global_runs.retry_after("*")
            if wait:
                raise LiveError(429, "daily_limit", wait)
            if self._monthly.remaining() <= 0:
                raise LiveError(429, "monthly_limit")
            self._ip_runs.record(ip)
            self._global_runs.record("*")
            self._monthly.record()
            job = Job(entity_id, now + self.s.job_ttl_seconds)
            self._jobs[job.id] = job
            self._inflight[entity_id] = job
            self._running += 1
        threading.Thread(target=self._worker, args=(job, entry[1]), daemon=True).start()
        return job, False

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            self._expire(self.clock())
            return self._jobs.get(job_id)

    def _expire(self, now: float) -> None:
        for jid in [j for j, job in self._jobs.items() if now > job.expires]:
            del self._jobs[jid]
        for eid in [e for e, (t, _) in self._cache.items() if now - t > self.s.cache_seconds]:
            del self._cache[eid]

    def _worker(self, job: Job, candidate: dict[str, Any]) -> None:
        try:
            result = self._run(job, candidate)
            job.emit("result", result)
            with self._lock:
                self._cache[job.entity_id] = (self.clock(), list(job.events))
                while len(self._cache) > MAX_CACHED_RESULTS:
                    del self._cache[next(iter(self._cache))]
        except LiveError as exc:
            job.emit("error", {"error": exc.code, "message": exc.message})
        except BudgetExceeded:
            job.emit("error", {"error": "call_cap_reached", "message": MESSAGES["call_cap_reached"]})
        except Exception as exc:  # fixed code out, class name only in the log
            log.error("live run crashed: %s", type(exc).__name__)
            job.emit("error", {"error": "internal_error", "message": MESSAGES["internal_error"]})
        finally:
            job.finish()
            with self._lock:
                self._running -= 1
                self._inflight.pop(job.entity_id, None)

    def _run(self, job: Job, candidate: dict[str, Any]) -> dict[str, Any]:
        budget = CallBudget(self.s.max_qloo_calls, self.pacer)
        client = self.factory(budget.retry_sleep)
        eid = candidate["entity_id"]
        slug = slugify(candidate["name"])[:40] or "live"
        warnings: list[str] = []
        # Temporary run folders live under the private data dir (never in the repo), unless overridden.
        parent = Path(self.s.work_dir) if self.s.work_dir else self.s.data_dir / "live_tmp"
        parent.mkdir(parents=True, exist_ok=True)

        def call(fn: Callable[[], Any], warning: str) -> tuple[Any, str | None]:
            """Runs in a worker. Returns (data, None) or (None, warning); only BudgetExceeded escapes."""
            budget.charge()
            try:
                return fn(), None
            except BudgetExceeded:
                raise
            except Exception as exc:  # soft failure: flag it, keep going
                log.warning("qloo call failed (%s): %s", warning, type(exc).__name__)
                return None, warning

        with tempfile.TemporaryDirectory(prefix="roadie-live-", dir=str(parent)) as tmp:
            root = Path(tmp)
            out = root / slug
            out.mkdir()
            search_record = {
                "name": candidate["name"],
                "entity_id": eid,
                "popularity": candidate["popularity"],
                **({"properties": {"short_description": candidate["description"]}} if candidate["description"] else {}),
            }
            (out / "search.json").write_text(json.dumps([search_record]), encoding="utf-8")
            # Description only, never tags: the same record capture_comedian.py saves.
            chosen = build_chosen([search_record], eid)
            (out / "chosen.json").write_text(json.dumps(chosen), encoding="utf-8")

            # All independent calls go to a small worker pool at once (the shared pacer still spaces them,
            # and the budget lock keeps the cap exact). Results are read back in the fixed order below,
            # so the plan and the event order never depend on which call finishes first.
            tasks: dict[str, list[tuple[str, Future]]] = {"where_popular": [], "places": [], "similar": [], "brands": []}
            pool = ThreadPoolExecutor(max_workers=max(1, min(self.s.live_workers, MAX_LIVE_WORKERS)), thread_name_prefix="roadie-live")
            try:
                for city in REDUCED_CITIES:
                    tasks["where_popular"].append(
                        (city, pool.submit(call, lambda c=city: client.where_popular(eid, c), f"where_popular_failed:{city_slug(city)}"))
                    )
                for city in REDUCED_CITIES:
                    tasks["places"].append(
                        (
                            city,
                            pool.submit(
                                call,
                                lambda c=city: cap_strings(trim_payload(client.places(eid, c, COMEDY_CLUB_TAG))),
                                f"places_failed:{city_slug(city)}",
                            ),
                        )
                    )
                tasks["similar"].append(("", pool.submit(call, lambda: cap_strings(trim_payload(client.similar(eid))), "similar_comics_failed")))
                tasks["brands"].append(("", pool.submit(call, lambda: cap_strings(trim_payload(client.brands(eid))), "brands_failed")))

                job.emit("started", {"group": "where_popular", "label": "audience affinity in each candidate city", "calls": len(REDUCED_CITIES)})
                ok_cities = 0
                for city, fut in tasks["where_popular"]:
                    data = _settle(fut, warnings)
                    path = out / f"where_popular_{city_slug(city)}.json"
                    if data is None:
                        continue
                    try:
                        path.write_text(json.dumps(data), encoding="utf-8")
                        if _load_city(path)[0] != city:
                            raise ValueError("unexpected city")
                        ok_cities += 1
                    except Exception:
                        path.unlink(missing_ok=True)
                        warnings.append(f"where_popular_unusable:{city_slug(city)}")
                if ok_cities == 0:
                    raise LiveError(502, "qloo_unavailable")

                job.emit("started", {"group": "places", "label": "comedy venues in each city", "calls": len(REDUCED_CITIES)})
                for city, fut in tasks["places"]:
                    places = _settle(fut, warnings)
                    if places is not None:
                        (out / f"places_{city_slug(city)}.json").write_text(json.dumps(places), encoding="utf-8")

                job.emit("started", {"group": "similar_comics", "label": "people with overlapping audiences", "calls": 1})
                similar = _settle(tasks["similar"][0][1], warnings)
                (out / "openers.json").write_text(json.dumps(similar or []), encoding="utf-8")

                job.emit("started", {"group": "brands", "label": "brands with audience overlap", "calls": 1})
                brands = _settle(tasks["brands"][0][1], warnings)
                if brands is not None:
                    (out / "brands.json").write_text(json.dumps(brands), encoding="utf-8")
            finally:
                pool.shutdown(wait=True, cancel_futures=True)  # on any error, queued calls never start
            # No descriptions.json: live runs do no description lookups, so identified_as_comedian stays null.

            try:
                plan = plan_tour(
                    slug,
                    FixtureClient(slug, root),
                    on_step=lambda ev: job.emit("step", cap_strings(asdict(ev), MAX_RESPONSE_TEXT)),
                ).to_dict()
            except Exception as exc:
                log.warning("live plan failed: %s", type(exc).__name__)
                raise LiveError(500, "plan_failed") from None
        # the temporary folder is gone here; the plan lives only in memory
        narration = narrate(plan, TemplateNarrator())  # never a model in live mode
        return cap_strings(
            {
                "mode": "live",
                "plan": plan,
                "narration": narration,
                "warnings": warnings,
                "qloo_calls": budget.count,
            },
            MAX_RESPONSE_TEXT,
        )
