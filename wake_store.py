"""Bounded private wake reservations. No timer, sender or model client."""

import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONTRACT = "neko.tools.context.v1"
MAX_BYTES = 262144
MAX_TARGETS = 128
PENDING = {"scheduled", "blocked"}
STATUSES = PENDING | {"submitted", "rejected", "unknown", "expired", "cancelled", "revoked"}


def telegram_key(account, user):
    """Canonical private-only namespace; never reinterpret a bare QQ identifier."""
    if any(
        not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", v) for v in (account, user)
    ):
        raise ValueError("invalid_telegram_identity")
    return f"telegram:{account}:{user}"


def target_identity(target):
    if target.startswith("telegram:"):
        parts = target.split(":")
        if len(parts) != 3 or telegram_key(parts[1], parts[2]) != target:
            raise ValueError("invalid_telegram_identity")
        return "telegram", parts[1], parts[2]
    return "qq", "", target


def target_keys(data):
    return set().union(
        *(data.get(name, {}) for name in ("slots", "appointments", "repeats", "dayplans"))
    )


def singles(data, target):
    """Primary occurrence and additional independently addressed v4 rows."""
    row = data["slots"].get(target)
    return ([row] if row else []) + list(data.get("appointments", {}).get(target, {}).values())


def all_singles(data):
    return [
        (target, row)
        for target in sorted(set(data["slots"]) | set(data.get("appointments", {})))
        for row in singles(data, target)
    ]


def selected_single(data, target, session=None):
    rows = [r for r in singles(data, target) if session is None or r["session"] == session]
    pending = [r for r in rows if r["status"] in PENDING]
    return (
        min(pending, key=lambda r: r["due"])
        if pending
        else max(rows, key=lambda r: r["attempted"], default=None)
    )


def single_revision(data, target):
    return (
        singles(data, target)
        if data.get("appointments", {}).get(target)
        else data["slots"].get(target)
    )


def text(value, limit, *, empty=False):
    return (
        isinstance(value, str)
        and (empty or bool(value.strip()))
        and len(value) <= limit
        and not any(ord(c) < 32 for c in value)
    )


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12


def valid_record(row):
    return isinstance(row, dict) and (
        text(row.get("session"), 128)
        and re.fullmatch("[a-f0-9]{32}", str(row.get("id", ""))) is not None
        and isinstance(row.get("status"), str)
        and row["status"] in STATUSES
        and text(row.get("intent"), 240)
        and text(row.get("timezone"), 64)
        and text(row.get("at"), 64)
        and text(row.get("block"), 64, empty=True)
        and text(row.get("task"), 160, empty=True)
        and type(row.get("shown")) is bool
        and (
            "dismissed" not in row
            or (
                type(row["dismissed"]) is bool
                and (not row["dismissed"] or row["status"] not in PENDING)
            )
        )
        and (
            "last_outcome" not in row or row["last_outcome"] in ("submitted", "rejected", "unknown")
        )
        and all(number(row.get(k)) for k in ("due", "retry", "attempted"))
        and ("guard" not in row or number(row["guard"]))
        and ("planner_requested_at" not in row or number(row["planner_requested_at"]))
        and (
            "repeat_last" not in row
            or (
                isinstance(row["repeat_last"], dict)
                and "repeat_last" not in row["repeat_last"]
                and valid_record(row["repeat_last"])
                and row["repeat_last"]["status"] not in PENDING
            )
        )
    )


def parse_appointment(at, zone, intent, now):
    if not text(at, 64) or not text(zone, 64) or not text(intent, 240):
        raise ValueError("invalid_time_or_intent")
    moment = datetime.fromisoformat(at)
    if moment.utcoffset() is None:
        raise ValueError("timezone_offset_required")
    try:
        local = moment.astimezone(ZoneInfo(zone))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid_timezone") from exc
    if local.utcoffset() != moment.utcoffset():
        raise ValueError("timezone_offset_mismatch")
    due = moment.timestamp()
    if not 60 <= due - now <= 7 * 86400:
        raise ValueError("time_must_be_60s_to_7d_in_future")
    return due, local.isoformat()


class WakeStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "private-wakes.json"

    def read(self):
        if self.path.is_symlink():
            raise ValueError("unsafe_wake_index")
        if not self.path.exists():
            return {
                "version": 4,
                "last_dispatch": 0,
                "slots": {},
                "appointments": {},
                "repeats": {},
                "dayplans": {},
            }
        if self.path.stat().st_size > MAX_BYTES:
            raise ValueError("wake_index_too_large")
        with self.path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("wake_index_too_large")
        data = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data.get("version") not in (4, 5)
            or not number(data.get("last_dispatch"))
            or not isinstance(data.get("slots"), dict)
            or len(data["slots"]) > MAX_TARGETS
        ):
            raise ValueError("invalid_wake_index")
        for target, row in data["slots"].items():
            if not text(target, 128) or not isinstance(row, dict):
                raise ValueError("invalid_wake_slot")
            if not valid_record(row):
                raise ValueError("invalid_wake_record")
        from .repeat import valid_plan
        from .dayplan import validate_day

        repeats = data.get("repeats")
        if not isinstance(repeats, dict):
            raise ValueError("invalid_repeat_index")
        if any(not text(target, 128) or not valid_plan(plan) for target, plan in repeats.items()):
            raise ValueError("invalid_repeat_record")
        plans = data.get("dayplans")
        if not isinstance(plans, dict):
            raise ValueError("invalid_day_index")
        if any(not text(target, 128) or not validate_day(plan) for target, plan in plans.items()):
            raise ValueError("invalid_day_record")
        extra = data.get("appointments")
        if (
            not isinstance(extra, dict)
            or len(set(data["slots"]) | set(repeats) | set(plans) | set(extra)) > MAX_TARGETS
        ):
            raise ValueError("invalid_wake_index")
        for target, rows in extra.items():
            if (
                not text(target, 128)
                or not isinstance(rows, dict)
                or not rows
                or any(not valid_record(row) or key != row["id"] for key, row in rows.items())
            ):
                raise ValueError("invalid_wake_record")
            records = singles(data, target)
            if len({r["id"] for r in records}) != len(records):
                raise ValueError("invalid_wake_record")
        # v4 never owned a Telegram namespace. Ambiguous legacy keys fail closed,
        # not silently relabelled as a new platform when authorization is enabled.
        for target in target_keys(data):
            if target.startswith("telegram:"):
                if data["version"] != 5:
                    raise ValueError("ambiguous_legacy_target")
                target_identity(target)
                if target in plans:
                    raise ValueError("telegram_dayplan_not_supported")
        return data

    def write(self, data):
        previous = self.read()  # Corrupt/unknown disk state is never treated as an empty index.
        # First Telegram write is the sole format transition, in the same bounded
        # atomic replacement as the plan. No QQ row or dispatch timestamp changes.
        if previous["version"] == 5 or any(t.startswith("telegram:") for t in target_keys(data)):
            data["version"] = 5
        for target in target_keys(data):
            target_identity(target)
            if target.startswith("telegram:") and target in data.get("dayplans", {}):
                raise ValueError("telegram_dayplan_not_supported")
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if (
            len(raw) > MAX_BYTES
            or len(
                set(data["slots"])
                | set(data.get("repeats", {}))
                | set(data.get("dayplans", {}))
                | set(data.get("appointments", {}))
            )
            > MAX_TARGETS
        ):
            raise ValueError("wake_index_full")
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".private-wakes-", dir=self.directory)
        try:
            os.chmod(name, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            if os.name == "posix":
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def recover(self, now):
        data = self.read()
        changed = False
        for _, row in all_singles(data):
            if row["status"] in PENDING and row["due"] <= now:
                row.update(status="expired", block="restart_missed")
                changed = True
        if data.get("repeats"):
            from .repeat import advance
        for plan in data.get("repeats", {}).values():
            if plan["enabled"] and plan["current"]["due"] <= now:
                advance(plan, now, "expired", "restart_missed")
                changed = True
        if data.get("dayplans"):
            from .dayplan import rotate

            for plan in data["dayplans"].values():
                changed = rotate(plan, now, restart=True) or changed
        if changed:
            self.write(data)

    def set(self, data, target, session, at, zone, intent, now, *, keep_time=False, wake_id=""):
        old = next((r for r in singles(data, target) if r["id"] == wake_id), {}) if wake_id else {}
        previous = dict(old)
        if keep_time and (
            old.get("status") not in PENDING or old.get("session") != session or at != old["at"]
        ):
            raise ValueError("stale_wake_revision")
        due, normalized = parse_appointment(at, zone, intent, old["due"] - 60 if keep_time else now)
        if target not in data["slots"] and len(data["slots"]) >= MAX_TARGETS:
            raise ValueError("wake_index_full")
        row = {
            "id": old["id"] if old else uuid.uuid4().hex,
            "session": session,
            "due": due,
            "at": normalized,
            "timezone": zone,
            "intent": intent.strip(),
            "status": "scheduled",
            "retry": due,
            "attempted": old.get("attempted", 0),
            "block": "",
            "task": "",
            "shown": False,
        }
        if "last_outcome" in old:
            row["last_outcome"] = old["last_outcome"]
        if "guard" in old:
            row["guard"] = old["guard"]
        if "repeat_last" in old and old.get("session") == session:
            row["repeat_last"] = old["repeat_last"]
        if old:
            old.clear()
            old.update(row)
            row = old
        elif target not in data["slots"]:
            data["slots"][target] = row
        else:
            data["appointments"].setdefault(target, {})[row["id"]] = row
        if keep_time:
            row.update(status=previous["status"], retry=previous["retry"], block=previous["block"])
        self.write(data)
        return row
