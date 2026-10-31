"""kh_worker entry point (spec §4.6).

    python -m kh_worker check
    python -m kh_worker once --job distil|daily|promote [--project P] [--backfill]
    python -m kh_worker sweep-sessions
    python -m kh_worker run

Config comes from the environment (CF user-provided service credentials starting KH_, ANTHROPIC_, CLAUDE_ are
copied into env, never overwriting). Validation fails at start (bad zone, bad slot, unknown job, `*` without
person auth).
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo

from kh_worker import VERSION
from kh_worker.budget import Budget, BudgetExceeded
from kh_worker.jobs import Ctx, LeaseLost
from kh_worker.khclient import (FileLedger, KHBudget, KHClient, KHLedger, Lease, PersonAuth, PrincipalAuth,
                                StaticToken)
from kh_worker.llm import Metered, make_llm

log = logging.getLogger("kh_worker")

JOBS = ("distil", "daily", "promote")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Fixed-offset fallback when the OS has no tz database (Windows without tzdata).
_FIXED = {"Asia/Ho_Chi_Minh": 7, "Asia/Bangkok": 7, "Asia/Singapore": 8, "Asia/Tokyo": 9, "UTC": 0, "Etc/UTC": 0,
          "Europe/London": 0, "Asia/Kolkata": 5.5, "Australia/Sydney": 10}


def zone(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        if name in _FIXED:
            return timezone(timedelta(hours=_FIXED[name]), name)
        raise ValueError(f"unknown time zone '{name}'")


def parse_slot(s: str, allow_off: bool = False) -> tuple[int | None, int, int] | None:
    """'HH:MM' (daily) or 'fri 03:00' (weekly) -> (weekday|None, hour, minute); 'off' -> None if allowed."""
    v = (s or "").strip().lower()
    if allow_off and v == "off":
        return None
    parts = v.split()
    wd = None
    if len(parts) == 2:
        if parts[0] not in DAYS:
            raise ValueError(f"bad slot '{s}'")
        wd = DAYS.index(parts[0])
        parts = parts[1:]
    if len(parts) != 1 or ":" not in parts[0]:
        raise ValueError(f"bad slot '{s}'")
    h, m = parts[0].split(":", 1)
    hh, mm = int(h), int(m)
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"bad slot '{s}'")
    return wd, hh, mm


def copy_vcap_credentials(env=os.environ) -> None:
    raw = env.get("VCAP_SERVICES")
    if not raw:
        return
    try:
        vcap = json.loads(raw)
    except ValueError:
        return
    for entries in vcap.values():
        for e in entries or []:
            for k, v in ((e or {}).get("credentials") or {}).items():
                if k.startswith(("KH_", "ANTHROPIC_", "CLAUDE_")) and k not in env:
                    env[k] = str(v)


@dataclass
class Config:
    url: str = ""
    enabled: bool = False
    mode: str = "dry"
    llm: str = "anthropic"
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_tokens: int = 16000
    cap_run: float = 3.0
    cap_month: float = 60.0
    cap_backfill: float = 80.0
    projects: list = field(default_factory=list)
    tz: str = "Asia/Ho_Chi_Minh"
    project_tz: dict = field(default_factory=dict)
    slots: dict = field(default_factory=dict)
    sessions_at: tuple | None = None
    jobs: list = field(default_factory=lambda: list(JOBS))
    tick_s: float = 60
    auth: str = "principal"
    auto_approve: bool = False
    state_dir: str = "state"
    max_batches: int = 50
    daily_max_tokens: int = 4000
    fallbacks: bool = True

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        g = env.get

        def flag(name, default):
            v = (g(name) or "").strip().lower()
            return default if not v else v in ("1", "true", "on", "yes")

        c = cls(
            url=(g("KH_URL") or "").rstrip("/"), enabled=flag("KH_WORKER_ENABLED", False),
            mode=(g("KH_WORKER_MODE") or "dry").lower(), llm=(g("KH_WORKER_LLM") or "anthropic").lower(),
            model=g("KH_WORKER_MODEL") or "claude-opus-5-5", effort=g("KH_WORKER_EFFORT") or "medium",
            max_tokens=int(g("KH_WORKER_MAX_TOKENS") or 16000), cap_run=float(g("KH_WORKER_CAP_RUN_USD") or 3),
            cap_month=float(g("KH_WORKER_CAP_MONTH_USD") or 60), cap_backfill=float(g("KH_WORKER_CAP_BACKFILL_USD") or 80),
            projects=[p.strip() for p in (g("KH_WORKER_PROJECTS") or "").split(",") if p.strip()],
            tz=g("KH_WORKER_TZ") or "Asia/Ho_Chi_Minh",
            jobs=[j.strip() for j in (g("KH_WORKER_JOBS") or ",".join(JOBS)).split(",") if j.strip()],
            tick_s=float(g("KH_WORKER_TICK_S") or 60), auth=(g("KH_WORKER_AUTH") or "principal").lower(),
            auto_approve=flag("KH_WORKER_AUTO_APPROVE", False), state_dir=g("KH_WORKER_STATE_DIR") or "state",
            max_batches=int(g("KH_WORKER_DISTIL_MAX_BATCHES") or 50),
            daily_max_tokens=int(g("KH_WORKER_DAILY_MAX_TOKENS") or 4000), fallbacks=flag("KH_WORKER_FALLBACKS", True))
        for item in (g("KH_WORKER_PROJECT_TZ") or "").split(","):
            if "=" in item:
                p, z = item.split("=", 1)
                c.project_tz[p.strip()] = z.strip()
        c.slots = {"distil": g("KH_WORKER_DISTIL_AT") or "sat 03:00", "daily": g("KH_WORKER_DAILY_AT") or "02:30",
                   "promote": g("KH_WORKER_PROMOTE_AT") or "02:00"}
        c.sessions_at = g("KH_WORKER_SESSIONS_AT") or "02:00"
        return c

    def validate(self) -> "Config":
        zone(self.tz)
        for z in self.project_tz.values():
            zone(z)
        for j in self.jobs:
            if j not in JOBS:
                raise ValueError(f"unknown job '{j}'")
        self.slots = {k: (parse_slot(v) if isinstance(v, str) else v) for k, v in self.slots.items()}
        if isinstance(self.sessions_at, str):
            self.sessions_at = parse_slot(self.sessions_at, allow_off=True)
        if "*" in self.projects and self.auth != "person":
            raise ValueError("KH_WORKER_PROJECTS='*' needs KH_WORKER_AUTH=person")
        if self.mode not in ("dry", "on"):
            raise ValueError("KH_WORKER_MODE must be dry or on")
        if self.auth not in ("principal", "person"):
            raise ValueError("KH_WORKER_AUTH must be principal or person")
        return self


def make_client(cfg: Config, env=os.environ, session=None) -> tuple[KHClient, object]:
    if cfg.auth == "person":
        auth = PersonAuth(env.get("KH_WORKER_KC_TOKEN_URL", ""), env.get("KH_WORKER_KC_CLIENT_ID", ""),
                          env.get("KH_WORKER_KC_CLIENT_SECRET", ""), env.get("KH_WORKER_USERNAME", ""),
                          env.get("KH_WORKER_PASSWORD", ""))
    elif env.get("KH_WORKER_TOKEN"):
        auth = StaticToken(env["KH_WORKER_TOKEN"])
    else:
        auth = PrincipalAuth(env.get("KH_WORKER_CLIENT_ID", ""), env.get("KH_WORKER_CLIENT_SECRET", ""))
    kh = KHClient(cfg.url, auth, session=session, mode=cfg.mode)
    ledger = FileLedger(cfg.state_dir) if cfg.auth == "person" else KHLedger(kh)
    return kh, ledger


def resolve_projects(cfg: Config, kh) -> list[str]:
    if "*" in cfg.projects:
        return [p["project"] for p in (kh.distill_patterns(1).get("projects") or [])]
    return list(cfg.projects)


def run_job(cfg: Config, kh, ledger, job: str, projects: list[str], backfill: bool = False, llm=None) -> dict:
    """One job run: lease, month spend, budget, job, release, one `worker.run` summary event."""
    t0 = time.time()
    run_id = "wr_" + uuid.uuid4().hex[:10]
    llm = llm or make_llm(cfg.llm, cfg.model, cfg.effort, cfg.fallbacks)
    summary = {"job": job, "projects": projects, "mode": cfg.mode, "backfill": backfill, "llm": cfg.llm,
               "model": getattr(llm, "model", cfg.model), "version": VERSION, "run_id": run_id}
    lease = Lease(ledger, f"worker:{job}")
    if not lease.acquire():
        summary.update(status="lease_held", result=None, budget=None, seconds=round(time.time() - t0, 2))
        return summary
    try:
        billed = getattr(llm, "billed", False)
        try:
            month = ledger.month_spend()
        except Exception as e:  # noqa: BLE001
            if billed:
                summary.update(status="month_unknown", result=str(e)[:200], budget=None)
                return summary
            month = 0.0
        recorded = {"failed": False}

        def on_spend(usd, info):
            try:
                ledger.record_spend(usd, "backfill" if backfill else "run", info)
            except Exception as e:  # noqa: BLE001
                recorded["failed"] = True
                raise BudgetExceeded(f"cost_not_recorded: {e}")

        budget = Budget(cfg.cap_run, cfg.cap_month, month, backfill, cfg.cap_backfill, on_spend)
        metered = Metered(llm, budget)
        ctx = Ctx(kh=kh, llm=metered, mode=cfg.mode, projects=projects, max_batches=cfg.max_batches,
                  auto_approve=cfg.auto_approve and cfg.auth == "person", daily_max_tokens=cfg.daily_max_tokens,
                  max_tokens=cfg.max_tokens, backfill=backfill, lease=lease, log=log)
        try:
            mod = importlib.import_module(f"kh_worker.jobs.{job}")
            result = mod.run(ctx)
            status = "lease_lost" if lease.lost else "ok"
        except KHBudget as e:
            result, status = e.body, "kh_budget"
        except BudgetExceeded as e:
            result, status = str(e), ("cost_not_recorded" if recorded["failed"] else "budget")
        except LeaseLost as e:
            result, status = str(e), "lease_lost"
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job)
            result, status = str(e)[:500], "error"
        summary.update(status=status, result=result, budget=budget.summary(), events=len(ctx.events))
        return summary
    finally:
        lease.release()
        summary["seconds"] = round(time.time() - t0, 2)
        try:
            ledger.event("worker.run", {k: v for k, v in summary.items()})
        except Exception as e:  # noqa: BLE001
            log.warning("worker.run event not written: %s", e)


class Scheduler:
    """Each tick: for each job, projects whose local time is at/after the slot (right weekday) and not yet fired
    today; promote runs once for the placeholder project `common`. Fired-state is in memory."""

    def __init__(self, cfg: Config, kh, ledger, now=None, runner=run_job):
        self.cfg, self.kh, self.ledger = cfg, kh, ledger
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.runner = runner
        self.fired: dict[tuple[str, str], str] = {}

    def _due(self, slot, z: str, key: tuple[str, str]) -> bool:
        if slot is None:
            return False
        wd, hh, mm = slot
        local = self.now().astimezone(zone(z))
        if wd is not None and local.weekday() != wd:
            return False
        if (local.hour, local.minute) < (hh, mm):
            return False
        day = local.date().isoformat()
        if self.fired.get(key) == day:
            return False
        self.fired[key] = day
        return True

    def tick(self) -> list[dict]:
        out = []
        projects = resolve_projects(self.cfg, self.kh)
        for job in self.cfg.jobs:
            slot = self.cfg.slots.get(job)
            if job == "promote":
                if self._due(slot, self.cfg.tz, (job, "common")):
                    out.append(self.runner(self.cfg, self.kh, self.ledger, job, ["common"]))
                continue
            due = [p for p in projects if self._due(slot, self.cfg.project_tz.get(p, self.cfg.tz), (job, p))]
            if due:
                out.append(self.runner(self.cfg, self.kh, self.ledger, job, due))
        if self.cfg.mode == "on" and self._due(self.cfg.sessions_at, self.cfg.tz, ("sessions", "-")):
            try:
                out.append({"job": "sweep-sessions", "result": self.kh.session_sweep()})
            except Exception as e:  # noqa: BLE001
                out.append({"job": "sweep-sessions", "status": "error", "result": str(e)[:200]})
        return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    copy_vcap_credentials()
    ap = argparse.ArgumentParser(prog="kh_worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    o = sub.add_parser("once")
    o.add_argument("--job", required=True, choices=JOBS)
    o.add_argument("--project")
    o.add_argument("--backfill", action="store_true")
    sub.add_parser("sweep-sessions")
    sub.add_parser("run")
    a = ap.parse_args(argv)
    try:
        cfg = Config.from_env().validate()
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if a.cmd == "check":
        print(json.dumps({"version": VERSION, "enabled": cfg.enabled, "mode": cfg.mode, "llm": cfg.llm,
                          "model": cfg.model, "jobs": cfg.jobs, "projects": cfg.projects, "auth": cfg.auth}))
        return 0
    if not cfg.enabled:
        if a.cmd == "once":
            print("worker disabled (KH_WORKER_ENABLED=0)", file=sys.stderr)
            return 2
        if a.cmd == "run":
            while True:  # pragma: no cover - idle forever
                time.sleep(cfg.tick_s)
    kh, ledger = make_client(cfg)
    if a.cmd == "sweep-sessions":
        print(json.dumps(kh.session_sweep()))
        return 0
    if a.cmd == "once":
        projects = ["common"] if a.job == "promote" else ([a.project] if a.project else resolve_projects(cfg, kh))
        print(json.dumps(run_job(cfg, kh, ledger, a.job, projects, a.backfill), default=str))
        return 0
    sched = Scheduler(cfg, kh, ledger)
    while True:  # pragma: no cover - service loop
        try:
            for r in sched.tick():
                log.info("run: %s", json.dumps(r, default=str)[:2000])
        except Exception as e:  # noqa: BLE001
            log.warning("tick failed: %s", e)
        time.sleep(cfg.tick_s)
