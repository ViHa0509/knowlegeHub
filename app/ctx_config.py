"""Context engine constants (spec §3.1). Every KH_* constant can be overridden by an env var of the same name.

Modules read these at call time (`ctx_config.STEP_FLOOR`), so tests may monkeypatch them.
"""
from __future__ import annotations

from app import config
from app.config import _env, _float, _int


def _words(name: str, default: str) -> tuple[str, ...]:
    raw = _env(name, default) or ""
    return tuple(w.strip() for w in raw.split(",") if w.strip())


def _bonus(name: str, default: float) -> float:
    raw = (_env(name) or "").strip().lower()
    if raw in ("off", "false", "no", "0"):
        return 0.0
    return _float(name, default)


# /context/step (§3.3)
STEP_BUDGET_CHARS = _int("KH_STEP_BUDGET_CHARS", 6000)
STEP_ITEM_CHARS = _int("KH_STEP_ITEM_CHARS", 600)
STEP_K = _int("KH_STEP_K", 6)
STEP_THREAD_LIMIT = _int("KH_STEP_THREAD_LIMIT", 5)
STEP_FLOOR = _float("KH_STEP_FLOOR", 0.68)
STEP_KNOW_ENOUGH = _float("KH_STEP_KNOW_ENOUGH", 0.75)
STEP_ENTITY_LIMIT = _int("KH_STEP_ENTITY_LIMIT", 4)
STEP_RECENT_LIMIT = _int("KH_STEP_RECENT_LIMIT", 8)
STEP_KEY_LIMIT = _int("KH_STEP_KEY_LIMIT", 6)
STEP_CONTEXT_LIMIT = _int("KH_STEP_CONTEXT_LIMIT", 40)
STEP_CONTEXT_BONUS = _bonus("KH_STEP_CONTEXT_BONUS", 0.05)
STEP_TIME_WORDS = _words(
    "KH_STEP_TIME_WORDS",
    "latest,most recent,recent,recently,newest,yesterday,today,this week,last week,since,what changed,"
    "current status,hom qua,hom nay,moi nhat,gan day,tuan nay")
STEP_DAILY = _int("KH_STEP_DAILY", 0)
STEP_DAILY_RECENT = _int("KH_STEP_DAILY_RECENT", 4)
STEP_DAILY_WEEK_DAYS = _int("KH_STEP_DAILY_WEEK_DAYS", 3)
STEP_NS_ITEMS = _int("KH_STEP_NS_ITEMS", 0)
STEP_NS_SHARE = _float("KH_STEP_NS_SHARE", 0.4)
MIN_ITEM_CHARS = 200

# /turn/context (§3.4)
TURN_LESSON_PROJECT = config.LESSON_PROJECT
TURN_LESSON_K = _int("KH_TURN_LESSON_K", 3)
TURN_LESSON_FLOOR = _float("KH_TURN_LESSON_FLOOR", 0.6)
TURN_DISTILLED_K = _int("KH_TURN_DISTILLED_K", 0)
TURN_DISTILLED_FLOOR = _float("KH_TURN_DISTILLED_FLOOR", 0.75)
TURN_DISTILLED_CHARS = _int("KH_TURN_DISTILLED_CHARS", 400)
TURN_ENTITY_LIMIT = _int("KH_TURN_ENTITY_LIMIT", 5)
TURN_FINDING_LIMIT = _int("KH_TURN_FINDING_LIMIT", 5)
TURN_WHEN_CHARS = _int("KH_TURN_WHEN_CHARS", 160)
TURN_TEXT_CHARS = _int("KH_TURN_TEXT_CHARS", 2000)

# policy action table (§3.7)
POLICY_DESC_MAX = _int("KH_POLICY_DESC_MAX", 400)
POLICY_RULE_MAX = _int("KH_POLICY_RULE_MAX", 1200)
POLICY_PLAN_MAX = _int("KH_POLICY_PLAN_MAX", 6000)
POLICY_OVERLAP = _float("KH_POLICY_OVERLAP", 0.6)
POLICY_UNUSED_DAYS = _int("KH_POLICY_UNUSED_DAYS", 30)
POLICY_MATCH_THRESHOLD = 0.6

# hook rule budgets
HOOK_MAX = {"SessionStart": 9000, "UserPromptSubmit": 1500, "PreToolUse": 2000, "Stop": 1500}
MENU_PLACEHOLDER = "{menu}"

# project match / sessions
PROJECT_MATCH_WEAK_KEYWORD_LEN = _int("KH_PROJECT_MATCH_WEAK_KEYWORD_LEN", 4)
SESSION_BINDING_TTL_HOURS = _int("KH_SESSION_BINDING_TTL_HOURS", 72)

# kernel (§3.8)
KERNEL_PART_MAX = _int("KH_KERNEL_PART_MAX", 9000)
KERNEL_SAME_SCORE = 0.35
KERNEL_CONTENT_MAX = 1500
KERNEL_TAGS_MIN = 2
KERNEL_TAGS_MAX = 12

# usage (§3.12)
USAGE_WINDOW_DAYS = 14
USAGE_MIN_DAYS = 3
USAGE_SAMPLE_CHARS = 120
USAGE_LIST_MAX = 20

# personalization (§3.9)
PERSONALIZATION_MAX_CHARS = 16000

# NEW (rebuild): load Appendix A seeds into empty tables at startup.
SEED_DEFAULTS = config._bool("KH_SEED_DEFAULTS", True)
