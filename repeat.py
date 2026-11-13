"""Bounded recurrence arithmetic/occurrences, never a scheduler or sender."""

from datetime import datetime, timedelta
import math
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .wake_store import PENDING, number, parse_appointment, text, valid_record


def zone_info(zone):
    if not text(zone, 64):
        raise ValueError("invalid_timezone")
    try:
        return ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        raise ValueError("invalid_timezone") from None


def next_due(plan, after):
    if plan["kind"] == "interval":
        anchor, interval = plan["anchor"], plan["interval"]
        return anchor + max(0, math.floor((after - anchor) / interval) + 1) * interval
    zone = zone_info(plan["timezone"])
    start = datetime.fromtimestamp(after, zone).date()
    hour, minute = map(int, plan["clock"].split(":"))
    for offset in range(9):
        day = start + timedelta(days=offset)
        naive = datetime(day.year, day.month, day.day, hour, minute)
        candidates = []
        for fold in (0, 1):
            stamp = naive.replace(tzinfo=zone, fold=fold).timestamp()
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == naive:
                candidates.append(stamp)
        # One occurrence per local day: gap skips day; fold always earlier.
        if candidates and min(candidates) > after:
            return min(candidates)
    raise ValueError("daily_time_unavailable")


def occurrence(plan, due):
    return {
        "id": uuid.uuid4().hex,
        "session": plan["session"],
        "due": due,
        "retry": due,
        "at": datetime.fromtimestamp(due, zone_info(plan["timezone"])).isoformat(),
        "timezone": plan["timezone"],
        "intent": plan["intent"],
        "status": "scheduled",
        "attempted": 0,
        "task": "",
        "block": "",
        "shown": False,
    }


def advance(plan, now, status=None, block=""):
    current = plan["current"]
    if status:
        current.update(status=status, block=block)
    if status == "expired" and not current["attempted"]:
        plan["last_skip"] = {"due": current["due"], "reason": block}
        if not plan["last"] or not plan["last"]["attempted"]:
            plan["last"] = current
    else:
        plan["last"] = current
    plan["current"] = occurrence(plan, next_due(plan, now))


def expires(plan):
    return min(plan["current"]["due"] + 86400, next_due(plan, plan["current"]["due"]))


def valid_plan(plan):
    if not isinstance(plan, dict):
        return False
    if (
        not re.fullmatch("[a-f0-9]{32}", str(plan.get("id", "")))
        or not text(plan.get("session"), 128)
        or not text(plan.get("intent"), 240)
        or not text(plan.get("timezone"), 64)
        or type(plan.get("enabled")) is not bool
        or plan.get("state") not in ("active", "paused", "revoked")
        or plan.get("enabled") is not (plan.get("state") == "active")
        or plan.get("kind") not in ("interval", "daily")
        or not number(plan.get("attempted"))
        or not number(plan.get("anchor"))
        or plan["anchor"] > 253402214400
        or type(plan.get("interval")) is not int
        or not 0 <= plan["interval"] <= 604800
        or not isinstance(plan.get("clock"), str)
        or not valid_record(plan.get("current"))
        or (plan.get("last") is not None and not valid_record(plan["last"]))
        or plan["current"]["session"] != plan["session"]
        or (plan.get("last") is not None and plan["last"]["session"] != plan["session"])
        or (plan.get("last") is not None and plan["last"]["status"] in PENDING)
        or (plan["state"] != "revoked" and plan["current"]["status"] not in PENDING)
        or plan["current"]["due"] > 253402214400
        or (
            "last_skip" in plan
            and (
                not isinstance(plan["last_skip"], dict)
                or not number(plan["last_skip"].get("due"))
                or not text(plan["last_skip"].get("reason"), 64)
            )
        )
    ):
        return False
    if plan["kind"] == "interval" and (plan["interval"] < 60 or plan["clock"]):
        return False
    if plan["kind"] == "daily" and (
        plan["interval"] != 0 or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", plan["clock"])
    ):
        return False
    try:
        zone_info(plan["timezone"])
    except ValueError:
        return False
    return True


def set_plan(
    data,
    target,
    session,
    now,
    *,
    kind="",
    timezone="",
    intent="",
    interval_minutes=0,
    local_time="",
    at="",
):
    previous = data.get("repeats", {}).get(target)
    old = previous if previous and previous["session"] == session else None
    kind = kind or (old["kind"] if old else "")
    timezone = timezone or (old["timezone"] if old else "")
    intent = intent or (old["intent"] if old else "")
    zone_info(timezone)
    if kind not in ("interval", "daily") or not text(intent, 240):
        raise ValueError("invalid_repeat")
    if type(interval_minutes) is not int or not 0 <= interval_minutes <= 10080:
        raise ValueError("invalid_repeat_interval")
    interval = (interval_minutes * 60) or (old["interval"] if old and old["kind"] == kind else 0)
    clock = local_time or (old["clock"] if old and old["kind"] == kind else "")
    if kind == "interval" and (interval < 60 or clock):
        raise ValueError("invalid_repeat_interval")
    if kind == "daily" and (
        interval_minutes
        or at
        or not isinstance(clock, str)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock)
    ):
        raise ValueError("invalid_repeat_clock")
    anchor = (
        parse_appointment(at, timezone, intent, now)[0]
        if at
        else old["anchor"]
        if old and old["kind"] == kind and old["interval"] == interval
        else now + interval
        if kind == "interval"
        else now
    )
    same = (
        old
        and old["state"] != "revoked"
        and old["session"] == session
        and not at
        and (kind, timezone, interval, clock)
        == (old["kind"], old["timezone"], old["interval"], old["clock"])
    )
    if same:
        old["intent"] = intent.strip()
        if old["current"]["status"] in PENDING:
            old["current"]["intent"] = intent.strip()
        return old
    plan = {
        "id": uuid.uuid4().hex,
        "session": session,
        "kind": kind,
        "timezone": timezone,
        "intent": intent.strip(),
        "anchor": anchor,
        "interval": interval if kind == "interval" else 0,
        "clock": clock if kind == "daily" else "",
        "enabled": not old or old["state"] != "paused",
        "state": "paused" if old and old["state"] == "paused" else "active",
        "attempted": previous["attempted"] if previous else 0,
        "last": old["last"] if old else None,
    }
    plan["current"] = occurrence(plan, next_due(plan, now))
    if old and "last_skip" in old:
        plan["last_skip"] = old["last_skip"]
    data["repeats"][target] = plan
    return plan


def change_plan(data, target, action, now):
    plan = data.get("repeats", {}).get(target)
    if not plan:
        raise ValueError("repeat_not_found")
    if plan["state"] == "revoked" and action in ("repeat_pause", "repeat_resume"):
        raise ValueError("repeat_revoked")
    if action == "repeat_pause":
        plan.update(enabled=False, state="paused")
    elif action == "repeat_resume":
        if not plan["enabled"]:
            plan.update(enabled=True, state="active")
            plan["current"] = occurrence(plan, next_due(plan, now))
    elif action == "repeat_delete":
        # Retain only a guard on the existing single-slot record/tombstone.
        row = data["slots"].get(target)
        if row is None and plan["attempted"]:
            row = occurrence(plan, plan["current"]["due"])
            row.update(status="cancelled", block="repeat_deleted")
            data["slots"][target] = row
        if row is not None:
            row["guard"] = max(row.get("guard", 0), plan["attempted"])
            if plan["last"] and plan["last"]["attempted"]:
                row["repeat_last"] = plan["last"]
        del data["repeats"][target]
    else:
        raise ValueError("invalid_action")
    return plan
