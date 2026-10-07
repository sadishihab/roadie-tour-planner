"""Server settings, read once from environment variables with safe defaults.

Only the variables named here are read. Nothing else from the environment is touched, and no
setting ever holds a secret: the Qloo credential lives in the qloo CLI's own setup on the host, and
the OpenAI key is read by the narrator in the gallery build script only.

Real Qloo data (fixtures and the gallery) is private and lives under ROADIE_DATA_DIR; see paths.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

from .paths import DEFAULT_DATA_DIR, data_dir as _data_dir, fixtures_dir as _fixtures_dir, gallery_dir as _gallery_dir

QLOO_MODES = ("persistent", "oneshot")
_TRUTHY = {"1", "true", "yes", "on"}


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _int(value: str | None, default: int, low: int = 0, high: int = 1_000_000) -> int:
    try:
        return min(max(int((value or "").strip()), low), high)
    except ValueError:
        return default


def _float(value: str | None, default: float, low: float, high: float) -> float:
    try:
        number = float((value or "").strip())
    except ValueError:
        return default
    return default if number != number else min(max(number, low), high)  # NaN falls back to the default


def _date(value: str | None) -> str:
    """A YYYY-MM-DD string, or "" when unset or malformed (a bad value must not end live mode early)."""
    text = (value or "").strip()
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def _mode(value: str | None) -> str:
    """persistent or oneshot; anything else (including unset) is the default, persistent."""
    text = (value or "").strip().lower()
    return text if text in QLOO_MODES else "persistent"


@dataclass(frozen=True)
class Settings:
    data_dir: Path = DEFAULT_DATA_DIR  # ROADIE_DATA_DIR; holds fixtures/ and gallery/ (private, never committed)
    live: bool = False  # ROADIE_LIVE=1; also needs the qloo CLI on the host
    allowed_origins: tuple[str, ...] = ()  # exact origins; "*" is ignored
    trust_proxy: bool = False  # ROADIE_TRUST_PROXY: take the client IP from X-Forwarded-For
    trusted_proxy_hops: int = 1  # ROADIE_TRUSTED_PROXY_HOPS: entries counted from the right end of X-Forwarded-For
    live_workers: int = 4  # ROADIE_LIVE_WORKERS: concurrent Qloo calls inside one live run (1 = serial, hard max 6)
    qloo_bin: str = "qloo"
    qloo_mode: str = "persistent"  # ROADIE_QLOO_MODE: persistent (one long-running `qloo mcp` process) or oneshot (one process per call)
    work_dir: Path | None = None  # parent of the temporary run folders; None = <data_dir>/live_tmp
    ip_runs_per_hour: int = 3
    global_runs_per_day: int = 15  # the hackathon key allows 10,000 requests a month; a run is at most 20
    monthly_runs: int = 300  # calendar month (UTC) ceiling on live plan runs: 300 x 20 = 6,000 calls
    max_qloo_per_second: float = 4.0  # client-side pacing; the key allows 5 per second. 0 turns pacing off
    live_until: str = ""  # ROADIE_LIVE_UNTIL=YYYY-MM-DD: the last day live mode runs (the key is deactivated after the hackathon)
    ip_searches_per_hour: int = 20
    global_searches_per_day: int = 200
    cache_seconds: int = 24 * 3600
    max_qloo_calls: int = 20  # hard cap per run, retries included
    max_running_jobs: int = 3
    job_ttl_seconds: int = 600

    @property
    def fixtures_dir(self) -> Path:
        return _fixtures_dir(self.data_dir)

    @property
    def gallery_dir(self) -> Path:
        return _gallery_dir(self.data_dir)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        origins = tuple(
            o.strip().rstrip("/") for o in (env.get("ROADIE_ALLOWED_ORIGINS") or "").split(",") if o.strip() and o.strip() != "*"
        )
        work_dir = (env.get("ROADIE_LIVE_WORK_DIR") or "").strip()
        return cls(
            data_dir=_data_dir(env),
            live=_flag(env.get("ROADIE_LIVE")),
            allowed_origins=origins,
            trust_proxy=_flag(env.get("ROADIE_TRUST_PROXY")),
            trusted_proxy_hops=_int(env.get("ROADIE_TRUSTED_PROXY_HOPS"), 1, 1, 10),
            live_workers=_int(env.get("ROADIE_LIVE_WORKERS"), 4, 1, 6),
            work_dir=Path(work_dir) if work_dir else None,
            qloo_mode=_mode(env.get("ROADIE_QLOO_MODE")),
            ip_runs_per_hour=_int(env.get("ROADIE_IP_RUNS_PER_HOUR"), 3),
            global_runs_per_day=_int(env.get("ROADIE_GLOBAL_RUNS_PER_DAY"), 15),
            monthly_runs=_int(env.get("ROADIE_MONTHLY_RUNS"), 300),
            max_qloo_per_second=_float(env.get("ROADIE_MAX_QLOO_PER_SECOND"), 4.0, 0.0, 5.0),
            live_until=_date(env.get("ROADIE_LIVE_UNTIL")),
            ip_searches_per_hour=_int(env.get("ROADIE_IP_SEARCHES_PER_HOUR"), 20),
            global_searches_per_day=_int(env.get("ROADIE_GLOBAL_SEARCHES_PER_DAY"), 200),
            cache_seconds=_int(env.get("ROADIE_CACHE_SECONDS"), 24 * 3600),
            max_qloo_calls=_int(env.get("ROADIE_MAX_QLOO_CALLS"), 20, 1, 100),
            max_running_jobs=_int(env.get("ROADIE_MAX_RUNNING_JOBS"), 3, 1, 20),
        )
