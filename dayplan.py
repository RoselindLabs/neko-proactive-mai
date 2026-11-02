"""Small today-only intentions. No generator, worker, sender or activity facts."""

from datetime import datetime
import hashlib
import json
import re

from .repeat import zone_info, occurrence
from .wake_store import PENDING, number, parse_appointment, text, valid_record

MAX_ITEMS = 6


def day_key(now, zone):
    return datetime.fromtimestamp(now, zone_info(zone)).date().isoformat()


def item_version(row):
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def active_items(data, target):
    plan = data.get("dayplans", {}).get(target)
    return list(plan["items"].values()) if plan else []


def rotate(plan, now, *, restart=False):
    changed = False
    today = day_key(now, plan["timezone"])
    for row in plan["items"].values():
        if row["status"] in PENDING and (today > plan["date"] or (restart and row["due"] <= now)):
            row.update(
                status="expired", block="day_ended" if today > plan["date"] else "restart_missed"
            )
            changed = True
    return changed


def validate_day(plan):
    if (
        not isinstance(plan, dict)
        or not text(plan.get("session"), 128)
        or not text(plan.get("timezone"), 64)
    ):
        return False
    if not isinstance(plan.get("date"), str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", plan["date"]
    ):
        return False
    if (
        not isinstance(plan.get("items"), dict)
        or len(plan["items"]) > MAX_ITEMS
        or not number(plan.get("attempted"))
    ):
        return False
    if not isinstance(plan.get("previous"), list) or len(plan["previous"]) > MAX_ITEMS:
        return False
    if (
        not isinstance(plan.get("retained"), list)
        or len(plan["retained"]) > MAX_ITEMS
        or any(
            not valid_record(row) or row["status"] != "unknown" or row["session"] != plan["session"]
            for row in plan["retained"]
        )
    ):
        return False
    if any(
        not isinstance(row, dict)
        or set(row) != {"status", "at", "attempted"}
        or row["status"]
        not in ("submitted", "rejected", "unknown", "cancelled", "expired", "revoked")
        or not text(row["at"], 64)
        or not number(row["attempted"])
        for row in plan["previous"]
    ):
        return False
    try:
        zone_info(plan["timezone"])
        datetime.fromisoformat(plan["date"])
        for key, row in plan["items"].items():
            if (
                not valid_record(row)
                or key != row["id"]
                or row["session"] != plan["session"]
                or row["timezone"] != plan["timezone"]
                or day_key(row["due"], row["timezone"]) != plan["date"]
            ):
                return False
            if "batch_id" in row and not re.fullmatch("[a-f0-9]{32}", str(row["batch_id"])):
                return False
        records = [*plan["items"].values(), *plan["retained"]]
        if len({row["id"] for row in records}) != len(records):
            return False
        for row in records:
            if row["timezone"] != plan["timezone"]:
                return False
            if row["attempted"]:
                if row["status"] not in ("unknown", "submitted", "rejected") or not re.fullmatch(
                    "[a-f0-9]{32}", str(row.get("batch_id", ""))
                ):
                    return False
                batch = [member for member in records if member.get("batch_id") == row["batch_id"]]
                if not any(member["id"] == row["batch_id"] for member in batch):
                    return False
                if any(
                    (
                        member["status"],
                        member["attempted"],
                        member["task"],
                        member["shown"],
                        member.get("planner_requested_at"),
                    )
                    != (
                        row["status"],
                        row["attempted"],
                        row["task"],
                        row["shown"],
                        row.get("planner_requested_at"),
                    )
                    for member in batch
                ):
                    return False
            elif (
                row.get("batch_id")
                or row["task"]
                or row["shown"]
                or row.get("planner_requested_at")
            ):
                return False
    except (ValueError, OverflowError, OSError):
        return False
    return True


def get_day(data, target, session, now, zone=""):
    plan = data.get("dayplans", {}).get(target)
    if plan and plan["session"] != session:
        return {
            "date": day_key(now, zone or "Asia/Shanghai"),
            "timezone": zone or "Asia/Shanghai",
            "items": [],
            "remaining": 0,
            "session_changed": True,
            "configured": True,
            "next_at": None,
        }
    zone = plan["timezone"] if plan else zone or "Asia/Shanghai"
    today = day_key(now, zone)
    rows = []
    if plan:
        for row in sorted(plan["items"].values(), key=lambda r: r["due"]):
            if row.get("dismissed"):
                continue
            expired = row["status"] in PENDING and plan["date"] < today
            rows.append(
                {
                    "item_id": row["id"],
                    "version": item_version(row),
                    "at": row["at"],
                    "intent": row["intent"],
                    "status": "expired" if expired else row["status"],
                    "block": "day_ended" if expired else row["block"],
                    "can_edit": row["status"] in PENDING
                    and row["due"] > now
                    and plan["date"] == today,
                    "can_delete": row["status"] not in PENDING or expired,
                    "planner_requested_at": row.get("planner_requested_at"),
                    "task": row["task"],
                }
            )
    return {
        "date": today,
        "timezone": zone,
        "items": rows,
        "remaining": MAX_ITEMS - len(plan["items"])
        if plan and plan["date"] == today
        else MAX_ITEMS,
        "previous": plan["previous"] if plan else [],
        "session_changed": False,
        "configured": plan is not None,
        "next_at": next((row["at"] for row in rows if row["status"] in PENDING), None),
    }


def change_day(
    data,
    target,
    session,
    now,
    action,
    *,
    at="",
    timezone="",
    intent="",
    item_id="",
    version="",
    preserve_minute=False,
):
    if any(
        not text(value, limit, empty=True)
        for value, limit in ((at, 64), (timezone, 64), (intent, 240), (item_id, 32), (version, 64))
    ):
        raise ValueError("invalid_day_input")
    plan = data.get("dayplans", {}).get(target)
    if plan and plan["session"] != session:
        raise ValueError("day_session_changed")
    zone = plan["timezone"] if plan else timezone
    zone_info(zone)
    if timezone and timezone != zone:
        raise ValueError("day_timezone_fixed")
    today = day_key(now, zone)
    if plan and today < plan["date"]:
        raise ValueError("day_clock_reversed")
    if action == "day_add":
        due, normalized = parse_appointment(at, zone, intent, now)
        if day_key(due, zone) != today:
            raise ValueError("day_time_outside_today")
        if plan and plan["date"] < today:
            rotate(plan, now)
            # Keep the most recent uncertain occurrence intact, never re-arm it.
            unknown = [
                *plan["retained"],
                *(row for row in plan["items"].values() if row["status"] == "unknown"),
            ]
            if len(unknown) > MAX_ITEMS:
                raise ValueError("day_unknown_retained")
            plan["retained"] = unknown
            plan["previous"] = [
                {k: r[k] for k in ("status", "at", "attempted")} for r in plan["items"].values()
            ]
            plan.update(date=today, items={})
        if plan is None:
            plan = {
                "session": session,
                "timezone": zone,
                "date": today,
                "attempted": 0,
                "items": {},
                "previous": [],
                "retained": [],
            }
        if len(plan["items"]) >= MAX_ITEMS:
            raise ValueError("day_full")
        row = occurrence({"session": session, "timezone": zone, "intent": intent.strip()}, due)
        row["at"] = normalized
        plan["items"][row["id"]] = row
        data["dayplans"][target] = plan
        return row
    if action not in ("day_update", "day_cancel") or not plan or item_id not in plan["items"]:
        raise ValueError("day_item_not_found")
    row = plan["items"][item_id]
    if not text(version, 64) or version != item_version(row):
        raise ValueError("stale_wake_revision")
    if plan["date"] != today or row["status"] not in PENDING or row["due"] <= now:
        raise ValueError("day_future_only")
    if action == "day_cancel":
        row.update(status="cancelled", block="")
        return row
    if preserve_minute and at:
        moment = datetime.fromisoformat(at)
        if moment.utcoffset() is not None and int(moment.timestamp() // 60) == int(
            row["due"] // 60
        ):
            at = ""
    keep_time = not at or at == row["at"]
    at, intent = at or row["at"], intent or row["intent"]
    due, normalized = parse_appointment(at, zone, intent, row["due"] - 60 if keep_time else now)
    if day_key(due, zone) != today:
        raise ValueError("day_time_outside_today")
    row.update(
        due=due, retry=due, at=normalized, intent=intent.strip(), status="scheduled", block=""
    )
    return row
