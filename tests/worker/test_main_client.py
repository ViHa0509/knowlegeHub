"""Config constants, run_job statuses, scheduler, CLI and the KH client / ledgers (spec §4.6). No network."""
import json
from datetime import datetime, timezone

import pytest

from kh_worker import khclient as K
from kh_worker import main as M
from kh_worker.budget import BudgetExceeded

from .conftest import FakeLLM

# -- config -----------------------------------------------------------------------------------------------


def test_config_defaults_exactly_as_spec():
    c = M.Config.from_env({})
    assert (c.url, c.enabled, c.mode, c.llm) == ("", False, "dry", "anthropic")
    assert (c.model, c.effort, c.max_tokens) == ("claude-opus-5-5", "medium", 16000)
    assert (c.cap_run, c.cap_month, c.cap_backfill) == (3.0, 60.0, 80.0)
    assert c.projects == [] and c.tz == "Asia/Ho_Chi_Minh"
    assert c.slots == {"distil": "sat 03:00", "daily": "02:30", "promote": "02:00"} and c.sessions_at == "02:00"
    assert c.jobs == ["distil", "daily", "promote"] and c.tick_s == 60 and c.auth == "principal"
    assert c.auto_approve is False and c.state_dir == "state" and c.max_batches == 50
    assert c.daily_max_tokens == 4000 and c.fallbacks is True
    c.validate()
    assert c.slots == {"distil": (5, 3, 0), "daily": (None, 2, 30), "promote": (None, 2, 0)}
    assert c.sessions_at == (None, 2, 0)


def test_config_from_env_values():
    c = M.Config.from_env({"KH_URL": "https://kh/", "KH_WORKER_ENABLED": "1", "KH_WORKER_MODE": "ON",
                           "KH_WORKER_PROJECTS": "a, b,", "KH_WORKER_PROJECT_TZ": "a=UTC,b=Asia/Tokyo",
                           "KH_WORKER_SESSIONS_AT": "off", "KH_WORKER_JOBS": "distil",
                           "KH_WORKER_FALLBACKS": "0", "KH_WORKER_CAP_RUN_USD": "1.5"}).validate()
    assert c.url == "https://kh" and c.enabled and c.mode == "on" and c.projects == ["a", "b"]
    assert c.project_tz == {"a": "UTC", "b": "Asia/Tokyo"} and c.sessions_at is None
    assert c.jobs == ["distil"] and c.fallbacks is False and c.cap_run == 1.5


@pytest.mark.parametrize("env", [
    {"KH_WORKER_TZ": "Mars/Base"}, {"KH_WORKER_DAILY_AT": "25:00"}, {"KH_WORKER_DISTIL_AT": "xyz 03:00"},
    {"KH_WORKER_JOBS": "distil,nope"}, {"KH_WORKER_PROJECTS": "*"}, {"KH_WORKER_MODE": "maybe"},
    {"KH_WORKER_AUTH": "magic"}, {"KH_WORKER_DAILY_AT": "off"},
])
def test_config_validation_fails(env):
    with pytest.raises(ValueError):
        M.Config.from_env(env).validate()


def test_star_projects_with_person_auth():
    c = M.Config.from_env({"KH_WORKER_PROJECTS": "*", "KH_WORKER_AUTH": "person"}).validate()
    kh = type("KH", (), {"distill_patterns": lambda self, d: {"projects": [{"project": "p1"}, {"project": "p2"}]}})()
    assert M.resolve_projects(c, kh) == ["p1", "p2"]


def test_vcap_credentials_copied_never_overwriting():
    env = {"KH_URL": "keep", "VCAP_SERVICES": json.dumps({"user-provided": [{"credentials": {
        "KH_URL": "new", "ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t", "OTHER": "x"}}]})}
    M.copy_vcap_credentials(env)
    assert env["KH_URL"] == "keep" and env["ANTHROPIC_API_KEY"] == "k" and env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"
    assert "OTHER" not in env


def test_cli_once_disabled_exits_2_and_check(monkeypatch, capsys):
    for k in ("KH_WORKER_ENABLED", "KH_WORKER_PROJECTS", "KH_WORKER_AUTH", "VCAP_SERVICES"):
        monkeypatch.delenv(k, raising=False)
    assert M.main(["once", "--job", "distil"]) == 2
    assert M.main(["check"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["enabled"] is False and out["mode"] == "dry"
    monkeypatch.setenv("KH_WORKER_JOBS", "bogus")
    assert M.main(["check"]) == 2


# -- run_job ----------------------------------------------------------------------------------------------


class FakeLedger:
    def __init__(self, acquire=1, month=0.0, record_fails=False):
        self.acquire, self.month, self.record_fails = acquire, month, record_fails
        self.spends, self.events, self.released = [], [], []

    def lease_acquire(self, name, ttl):
        return self.acquire

    def lease_renew(self, name, epoch, ttl):
        return True

    def lease_release(self, name, epoch):
        self.released.append(name)

    def month_spend(self):
        if isinstance(self.month, Exception):
            raise self.month
        return self.month

    def record_spend(self, usd, kind="run", info=None):
        if self.record_fails:
            raise RuntimeError("metrics down")
        self.spends.append((usd, kind))

    def event(self, kind, body):
        self.events.append((kind, body))


@pytest.fixture
def cfg():
    return M.Config.from_env({"KH_WORKER_MODE": "on"}).validate()


def _job(monkeypatch, fn):
    import kh_worker.jobs.distil as d
    monkeypatch.setattr(d, "run", fn)


def test_run_job_ok_records_spend_and_summary_event(cfg, kh, monkeypatch):
    def job(ctx):
        ctx.llm.complete("s", "u", {}, 100)
        return {"done": True}

    _job(monkeypatch, job)
    led = FakeLedger()
    s = M.run_job(cfg, kh, led, "distil", ["alpha"], llm=FakeLLM([{}], cost=0.05))
    assert s["status"] == "ok" and s["result"] == {"done": True} and s["budget"]["billed_usd"] == 0.05
    assert led.spends == [(0.05, "run")] and led.released == ["worker:distil"]
    kind, body = led.events[-1]
    assert kind == "worker.run"
    for k in ("job", "projects", "mode", "backfill", "llm", "model", "version", "run_id", "status", "result",
              "budget", "seconds"):
        assert k in body


def test_run_job_backfill_kind(cfg, kh, monkeypatch):
    _job(monkeypatch, lambda ctx: ctx.llm.complete("s", "u", {}, 100) and {})
    led = FakeLedger(month=1000.0)  # month cap ignored for backfill
    s = M.run_job(cfg, kh, led, "distil", ["a"], backfill=True, llm=FakeLLM([{}], cost=0.05))
    assert s["status"] == "ok" and led.spends == [(0.05, "backfill")] and s["budget"]["cap_run_usd"] == 80.0


def test_run_job_lease_held(cfg, kh):
    led = FakeLedger(acquire=None)
    s = M.run_job(cfg, kh, led, "distil", ["a"], llm=FakeLLM())
    assert s["status"] == "lease_held" and led.events == []


def test_run_job_month_unknown_fails_closed_only_when_billed(cfg, kh, monkeypatch):
    _job(monkeypatch, lambda ctx: {})
    led = FakeLedger(month=RuntimeError("no metrics"))
    assert M.run_job(cfg, kh, led, "distil", ["a"], llm=FakeLLM(billed=True))["status"] == "month_unknown"
    assert led.released == ["worker:distil"] and led.events[-1][1]["status"] == "month_unknown"
    assert M.run_job(cfg, kh, led, "distil", ["a"], llm=FakeLLM(billed=False))["status"] == "ok"


def test_run_job_status_mapping(cfg, kh, monkeypatch):
    def raises(e):
        def job(ctx):
            raise e
        return job

    cases = [(BudgetExceeded("cap"), "budget"), (K.KHBudget(429, {"error": "budget"}), "kh_budget"),
             (M.LeaseLost("x"), "lease_lost"), (RuntimeError("boom"), "error")]
    for exc, status in cases:
        _job(monkeypatch, raises(exc))
        assert M.run_job(cfg, kh, FakeLedger(), "distil", ["a"], llm=FakeLLM())["status"] == status


def test_run_job_cost_not_recorded(cfg, kh, monkeypatch):
    _job(monkeypatch, lambda ctx: ctx.llm.complete("s", "u", {}, 100))
    s = M.run_job(cfg, kh, FakeLedger(record_fails=True), "distil", ["a"], llm=FakeLLM([{}], cost=0.05))
    assert s["status"] == "cost_not_recorded"


def test_run_job_auto_approve_only_for_person(cfg, kh, monkeypatch):
    seen = []
    _job(monkeypatch, lambda ctx: seen.append(ctx.auto_approve) or {})
    cfg.auto_approve = True
    M.run_job(cfg, kh, FakeLedger(), "distil", ["a"], llm=FakeLLM())
    cfg.auth = "person"
    M.run_job(cfg, kh, FakeLedger(), "distil", ["a"], llm=FakeLLM())
    assert seen == [False, True]


# -- scheduler --------------------------------------------------------------------------------------------


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def test_scheduler_slots_weekday_once_per_day_and_promote_common(kh):
    cfg = M.Config.from_env({"KH_WORKER_PROJECTS": "a,b", "KH_WORKER_PROJECT_TZ": "b=UTC",
                             "KH_WORKER_MODE": "on"}).validate()
    runs = []
    kh.session_sweep = lambda: runs.append(("sweep",)) or {"deleted": 0}
    clock = Clock(datetime(2026, 10, 30, 19, 0, tzinfo=timezone.utc))  # Sat 02:00 in HCM, Fri 19:00 UTC
    s = M.Scheduler(cfg, kh, None, now=clock, runner=lambda c, k, l, job, p: runs.append((job, p)) or {"job": job})
    s.tick()
    assert ("promote", ["common"]) in runs and ("sweep",) in runs
    # a: Sat 02:00 local is before daily 02:30 and distil 03:00; b: Fri 19:00 UTC is past 02:30 but not Saturday
    assert ("daily", ["b"]) in runs and not any(r[0] == "distil" for r in runs)
    assert not any(r[0] == "daily" and "a" in r[1] for r in runs)
    runs.clear()
    clock.dt = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)  # Sat 03:00 HCM
    s.tick()
    assert ("distil", ["a"]) in runs and ("daily", ["a"]) in runs and ("promote", ["common"]) not in runs
    runs.clear()
    s.tick()
    assert runs == []  # fired once per local day
    clock.dt = datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)  # Sat 03:00 UTC for b
    s.tick()
    assert ("distil", ["b"]) in runs and ("daily", ["b"]) in runs


def test_scheduler_skips_session_sweep_in_dry_mode(kh):
    cfg = M.Config.from_env({"KH_WORKER_JOBS": "daily"}).validate()
    swept = []
    kh.session_sweep = lambda: swept.append(1)
    s = M.Scheduler(cfg, kh, None, now=lambda: datetime(2026, 10, 30, 23, 0, tzinfo=timezone.utc),
                    runner=lambda *a: {})
    s.tick()
    assert swept == []


# -- KH client, auth, leases, ledgers ---------------------------------------------------------------------


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):  # noqa: A002
        self.calls.append({"method": method, "url": url, "json": json, "params": params, "headers": headers,
                           "timeout": timeout})
        r = self.routes.get((method, url.split("https://kh", 1)[-1]), (200, {}))
        return Resp(*(r(json) if callable(r) else r))

    def post(self, url, data=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "data": data})
        return Resp(*self.routes[("POST", url)])


def test_client_dry_enforcement_timeout_and_headers(monkeypatch):
    monkeypatch.delenv("KH_WORKER_HTTP_TIMEOUT_S", raising=False)
    sess = Session({})
    kh = K.KHClient("https://kh/", K.StaticToken("T"), session=sess, mode="dry")
    assert kh.timeout_s == 180
    kh.distill_submit({"items": []})
    kh.daily_submit({"x": 1})
    kh.promote_submit({"items": []})
    assert all(c["json"]["dry_run"] is True for c in sess.calls)
    assert sess.calls[0]["headers"]["Authorization"] == "Bearer T" and sess.calls[0]["url"] == "https://kh/distill/submit"
    on = K.KHClient("https://kh", K.StaticToken("T"), session=sess, mode="on")
    on.distill_submit({"items": []})
    assert "dry_run" not in sess.calls[-1]["json"]
    on.distill_next("alpha", 20)
    assert sess.calls[-1]["params"] == {"project": "alpha", "limit": 20}


def test_client_maps_429_budget_and_errors():
    sess = Session({("GET", "/distill/next"): (429, {"error": "budget", "month_usd": 61, "cap_usd": 60}),
                    ("GET", "/daily/next"): (500, {"detail": "x"})})
    kh = K.KHClient("https://kh", None, session=sess)
    with pytest.raises(K.KHBudget) as e:
        kh.distill_next("a")
    assert e.value.body["cap_usd"] == 60
    with pytest.raises(K.KHError) as e2:
        kh.daily_next("a")
    assert e2.value.status == 500 and not isinstance(e2.value, K.KHBudget)


def test_principal_auth_flow_and_remint(monkeypatch):
    routes = {("POST", "/v1/token"): (200, {"access_token": "NS"}),
              ("POST", "/v1/kh_people/principals"): (409, {"error": "exists"}),
              ("POST", "/v1/kh_people/principals/worker/token"): (200, {"access_token": "W", "expires_in": 900})}
    sess = Session(routes)
    auth = K.PrincipalAuth("cid", "sec")
    kh = K.KHClient("https://kh", auth, session=sess)
    kh.promote_next()
    paths = [c["url"].split("https://kh")[1] for c in sess.calls]
    assert paths == ["/v1/token", "/v1/kh_people/principals", "/v1/kh_people/principals/worker/token", "/promote/next"]
    assert sess.calls[0]["json"] == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "sec"}
    assert sess.calls[1]["json"] == {"role": "distiller", "principal_id": "worker"}
    assert sess.calls[1]["headers"]["Authorization"] == "Bearer NS" and sess.calls[3]["headers"]["Authorization"] == "Bearer W"
    kh.promote_next()
    assert len(sess.calls) == 5  # token reused
    auth._exp = K.time.time() + 89  # within 90 s of expiry -> re-mint
    kh.promote_next()
    assert len(sess.calls) == 9


def test_person_auth_password_then_refresh():
    url = "https://kc/token"
    sess = Session({("POST", url): (200, {"access_token": "A", "refresh_token": "R", "expires_in": 300})})
    a = K.PersonAuth(url, "kh-agent-distill", "s", "u", "p", session=sess)
    assert a.bearer(None) == "A" and sess.calls[0]["data"]["grant_type"] == "password"
    a._exp = 0
    a.bearer(None)
    assert sess.calls[1]["data"]["grant_type"] == "refresh_token" and sess.calls[1]["data"]["refresh_token"] == "R"


def test_kh_ledger_routes_and_month_excludes_backfill():
    routes = {("POST", "/v1/kh_people/leases/worker:distil:acquire"): (200, {"epoch": 7}),
              ("GET", "/v1/kh_people/metrics:aggregate"): (200, {"groups": [{"dims.kind": "run", "sum": 2.5},
                                                                           {"dims.kind": "backfill", "sum": 40}]})}
    sess = Session(routes)
    led = K.KHLedger(K.KHClient("https://kh", None, session=sess))
    assert led.lease_acquire("worker:distil", 300) == 7
    assert led.month_spend() == 2.5
    assert sess.calls[-1]["params"]["series"] == "worker.cost_usd" and sess.calls[-1]["params"]["group_by"] == "dims.kind"
    led.record_spend(0.1, "run", {"model": "m", "usage": {}})
    assert sess.calls[-1]["json"] == {"series": "worker.cost_usd", "value": 0.1, "dims": {"kind": "run", "model": "m"}}
    sess.routes[("POST", "/v1/kh_people/leases/worker:daily:acquire")] = (409, {"error": "held"})
    assert led.lease_acquire("worker:daily", 300) is None


def test_file_ledger(tmp_path):
    a, b = K.FileLedger(str(tmp_path)), K.FileLedger(str(tmp_path))
    assert a.lease_acquire("worker:distil", 300) == 1
    assert b.lease_acquire("worker:distil", 300) is None
    assert a.lease_renew("worker:distil", 1, 300) and not b.lease_renew("worker:distil", 1, 300)
    a.lease_release("worker:distil", 1)
    assert b.lease_acquire("worker:distil", 300) == 2
    a.record_spend(1.0, "run")
    a.record_spend(5.0, "backfill")
    assert b.month_spend() == 1.0
    a.event("worker.run", {"x": 1})
    assert a.events()[0]["body"] == {"x": 1}


def test_lease_renewal_failure_sets_lost():
    class L(FakeLedger):
        def lease_renew(self, name, epoch, ttl):
            return False

    lease = K.Lease(L(), "worker:distil", grant_s=0.03)
    assert lease.acquire()
    lease._t.join(1)
    assert lease.lost
    lease.release()
    assert K.Lease(FakeLedger(), "x").grant_s == 300
