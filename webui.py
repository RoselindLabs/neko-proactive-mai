"""Private WebUI projection. No timer, message-history reads or send controls."""

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import time

from .wake_store import (
    PENDING,
    text,
    singles,
    selected_single,
    single_revision,
    target_identity,
    telegram_key,
)

STATUSES = {
    "scheduled": "待唤醒",
    "blocked": "等待规则放行",
    "submitted": "宿主已入队",
    "rejected": "宿主已拒绝",
    "unknown": "提交结果未知",
    "cancelled": "已取消",
    "expired": "已过期",
    "revoked": "授权或会话已失效",
}
REASONS = {
    "": "—",
    "activity_hours": "活动时段外",
    "cooldown": "最小间隔未到",
    "safety_spacing": "等待计划60秒安全间距",
    "isolation": "真人静默期未到",
    "dispatch_spacing": "等待预约提交间隔",
    "restart_missed": "重载时已错过，不补执行",
    "blocked_24h": "已等待24小时，停止执行",
    "authorization_or_session_changed": "名单或真实会话已改变",
    "session_unavailable": "真实会话或账号暂不可核实，稍后重查；未提交或撤销计划",
    "probability": "本轮概率未命中",
    "checking": "正在检查",
    "eligible": "本轮条件通过",
    "appointment": "正在检查计划",
    "plan_spacing": "临近计划或60秒安全间距，稍后检查基础唤醒",
    "simple_wake_time": "按北京时间填写10分钟后、21:30、明天09:00或日期时间，须为未来1分钟至7天",
    "simple_today_time": "日程只能设今天北京时间的未来时刻：填HH:MM，至少1分钟后，不能设明天或已过时间",
    "simple_repeat_choice": "间隔分钟与每天时刻二选一，如interval_minutes=120或local_time=21:00",
    "single_priority": "等待单次预约结束",
    "repeat_window_missed": "本次已跳过，等待下一轮",
    "repeat_deleted": "重复计划已删除",
    "day_ended": "已跨天，旧事项不补执行",
    "dayplan_priority": "临近当天事项，本次重复已跳过",
    "day_merge_wait": "等待相近事项一起判断（最多60秒）",
}
ERRORS = {
    "telegram_dayplan_not_supported": "Telegram私聊仅支持单次与重复计划",
    "invalid_telegram_identity": "请配置Telegram机器人账号与用户的数字ID",
    "simple_wake_time": REASONS["simple_wake_time"],
    "simple_today_time": REASONS["simple_today_time"],
    "simple_repeat_choice": REASONS["simple_repeat_choice"],
    "single_identity_required": "先查看预约列表，选择一条预约及其当前版本后再修改或取消",
    "single_not_found": "没有找到这条单次预约，请刷新列表",
    "single_future_only": "只能修改未来尚未尝试的预约，或取消仍待执行的预约",
    "invalid_day_input": "日程字段无效：时刻/时区、240字以内依据和当前事项版本必须是有界文本",
    "day_session_changed": "日程属于旧真实会话，不能借用其意图或额度",
    "day_timezone_fixed": "日程时区已固定，请沿用当前日程时区",
    "day_clock_reversed": "当前日期早于已保存日程，未重置额度",
    "day_time_outside_today": "事项必须是该时区今天的未来时刻",
    "day_unknown_retained": "保留的未知日程已达上限，未清除或重放旧事项",
    "day_full": "今天最多6项，已取消和已尝试项仍计入额度",
    "day_item_not_found": "没有找到当前私聊的这项日程",
    "day_future_only": "只能修改或删除尚未到点、从未提交的今天事项",
    "record_not_found": "这条记录已改变或已删除，请刷新列表",
    "record_future_only": "待执行事项请先取消，只有已结束的记录可以删除",
    "wake_submission_inflight": "该私聊正在提交Host，稍后刷新核对；不会自动重试",
    "invalid_repeat": "选择固定间隔或每天，并填写联系依据",
    "invalid_repeat_interval": "间隔须为1～10080分钟",
    "invalid_repeat_clock": "每天时刻须为HH:MM，不能同时填写间隔或首次时刻",
    "repeat_not_found": "当前没有重复计划",
    "daily_time_unavailable": "未来当地时刻不可用",
    "wake_disabled": "插件或自动调度已关闭，未保存新预约；仍可取消待执行预约",
    "private_chat_not_authorized": "目标不在当前私聊授权名单中",
    "session_changed": "真实会话已改变，请刷新后重新选择",
    "stale_wake_revision": "预约已被模型、调度器或其他管理员修改，请刷新后重新选择",
    "invalid_time_or_intent": "填写带偏移的绝对时刻、IANA时区及1～240字符意图",
    "timezone_offset_required": "预约时刻必须带UTC偏移，例如+08:00",
    "timezone_offset_mismatch": "时刻的UTC偏移与所选IANA时区不匹配",
    "invalid_timezone": "IANA时区无效，例如Asia/Shanghai",
    "time_must_be_60s_to_7d_in_future": "新时刻必须在未来60秒至7天内",
    "wake_index_full": "预约存储已满，可修改已有目标；未清除未知记录",
    "invalid_management_request": "管理参数无效，请刷新后重新选择目标",
}


def wall_instant(when, zone):
    from .repeat import zone_info

    if not text(when, 32):
        raise ValueError("日期和时间无效")
    naive = datetime.fromisoformat(when)
    if naive.tzinfo is not None:
        raise ValueError("请选择不带偏移的当地日期和时间")
    tz = zone_info(zone)
    candidates = [
        naive.replace(tzinfo=tz, fold=fold).timestamp()
        for fold in (0, 1)
        if datetime.fromtimestamp(naive.replace(tzinfo=tz, fold=fold).timestamp(), tz).replace(
            tzinfo=None
        )
        == naive
    ]
    if not candidates:
        raise ValueError("该当地时刻因夏令时不存在，请选择其他时间")
    return datetime.fromtimestamp(min(candidates), tz).isoformat()


def wake_revision(target, session, row, repeat=None, day=None):
    value = [target, session, row] if repeat is None else [target, session, row, repeat]
    if day is not None:
        value = [*value, day]
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def local_time(stamp):
    return (
        datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds") if stamp else "—"
    )


def next_activity(now, start, end):
    """Next server-local policy opening; projection only, never a timer deadline."""
    current = datetime.fromtimestamp(now)
    beginning = datetime.strptime(start, "%H:%M").time()
    ending = datetime.strptime(end, "%H:%M").time()
    inside = (
        beginning <= current.time() <= ending
        if beginning <= ending
        else current.time() >= beginning or current.time() <= ending
    )
    if inside:
        return now
    candidate = datetime.combine(current.date(), beginning)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


class WakeManagement:
    def _telegram_targets(self):
        targets = []
        for user in self.config.target.allowed_telegram_user_ids:
            try:
                targets.append(telegram_key(self.config.target.telegram_account_id, user))
            except ValueError:
                continue
        return list(dict.fromkeys(targets))

    def _plan_allowed(self, target):
        if target.startswith("telegram:"):
            return target in self._telegram_targets()
        return target in {str(v).strip() for v in self.config.target.allowed_friends}

    def _target_view(self, target):
        platform, account, user = target_identity(target)
        return {"platform": platform, "account_id": account or self._bot_qq, "user_id": user}

    def _delete_record_locked(self, action, target, session, revision, item_id, version):
        """Admin-only list dismissal; keep dispatch, batch identity and daily usage intact."""
        from .dayplan import day_key, item_version

        if not self._wake_ready:
            raise ValueError("wake_disabled")
        if self._wake_inflight == target:
            raise ValueError("wake_submission_inflight")
        data = self._get_wakes().read()
        plan = data.get("dayplans", {}).get(target)
        if revision != wake_revision(
            target,
            session,
            single_revision(data, target),
            data.get("repeats", {}).get(target),
            plan,
        ):
            raise ValueError("stale_wake_revision")
        rows = (
            [*plan["items"].values(), *plan["retained"]]
            if action == "day_delete" and plan
            else singles(data, target)
            if action == "single_delete"
            else []
        )
        row = next((r for r in rows if r["id"] == item_id and r["session"] == session), None)
        if not row or row.get("dismissed"):
            raise ValueError("record_not_found")
        if not text(version, 64) or version != item_version(row):
            raise ValueError("stale_wake_revision")
        expired = action == "day_delete" and plan["date"] < day_key(time.time(), plan["timezone"])
        if row["status"] in PENDING:
            if not expired:
                raise ValueError("record_future_only")
            row.update(status="expired", block="day_ended")
        row["dismissed"] = True
        self._get_wakes().write(data)

    def _note_check(self, state, reason, **extra):
        key = (state.is_group, state.target_id)
        previous = self._observations.get(key, {})
        self._observations[key] = {
            **previous,
            "session": state.stream_id,
            "at": time.time(),
            "reason": reason,
            **extra,
        }
        while len(self._observations) > 256:
            self._observations.pop(next(iter(self._observations)))

    def _record_submission(self, state, outcome, source, stamp=None):
        self._submission_results[(state.is_group, state.target_id)] = {
            "status": outcome,
            "source": source,
            "session": state.stream_id,
            "at": stamp if stamp is not None else time.time(),
        }
        while len(self._submission_results) > 256:
            self._submission_results.pop(next(iter(self._submission_results)))

    async def _management_stream(self, target, group, *, session=""):
        platform, account, user = target_identity(target)
        account = account or self._bot_qq
        if platform == "telegram":
            if group:
                return None
            # The user-only getter may choose another bot's first session. List
            # existing private sessions and require an exact, unique identity.
            async with asyncio.timeout(2):
                result = await self.ctx.chat.get_private_streams(platform="telegram")
            if isinstance(result, dict):
                result = None if result.get("success") is False else result.get("streams")
            if not isinstance(result, list) or len(result) > 512:
                return None
            matches = [
                row
                for row in result
                if isinstance(row, dict)
                and row.get("platform") == platform
                and row.get("account_id") == account
                and row.get("user_id") == user
                and row.get("is_group_session") is False
                and not row.get("group_id")
                and (not session or row.get("session_id") == session)
            ]
            if len(matches) != 1:
                return None
            result = matches[0]
        else:
            getter = (
                self.ctx.chat.get_stream_by_group_id
                if group
                else self.ctx.chat.get_stream_by_user_id
            )
            result = await getter(target)
        if not isinstance(result, dict) or result.get("success") is False:
            return None
        # PluginContext normalizes Host {success, stream} into the stream itself.
        # Keep explicit-envelope compatibility, without treating stream=None as
        # permission to guess a target or accepting a failed Host response.
        stream = result.get("stream", result if "session_id" in result else None)
        if not isinstance(stream, dict):
            return None
        sid = stream.get("session_id")
        if (
            not text(sid, 128)
            or sid != sid.strip()
            or (session and sid != session)
            or ("stream_id" in stream and stream["stream_id"] != sid)
            or stream.get("platform") != platform
            or stream.get("is_group_session") is not group
            or str(stream.get("group_id" if group else "user_id") or "") != user
            or (not group and stream.get("group_id"))
            or not text(account, 128)
            or stream.get("account_id") != account
        ):
            return None
        name = (
            stream.get("group_name")
            if group
            else stream.get("user_cardname") or stream.get("user_nickname")
        )
        return {
            "session": sid,
            "name": name[:80] if text(name, 500) else "未命名聊天",
            "platform": platform,
            "account_id": account,
            "user_id": user,
        }

    async def management_change(
        self,
        action,
        target,
        session,
        revision,
        at="",
        zone="",
        intent="",
        *,
        repeat_fields=None,
        day_fields=None,
        item_id="",
        version="",
    ):
        try:
            if (
                not text(target, 128)
                or not text(session, 128)
                or not text(revision, 64)
                or any(not isinstance(v, str) for v in (at, zone, intent))
            ):
                raise ValueError("invalid_management_request")
            async with asyncio.timeout(7):
                async with self._wake_lock:
                    if action in ("single_delete", "day_delete"):
                        self._delete_record_locked(
                            action, target, session, revision, item_id, version
                        )
                        self._management_operation = "记录已删除；已入队任务不撤回"
                        return {"success": True, "message": self._management_operation}
                    if not self._plan_allowed(target):
                        raise ValueError("private_chat_not_authorized")
                    epoch = self._management_epoch
                    resolved = await self._management_stream(target, False, session=session)
                    if self._management_epoch != epoch:
                        raise ValueError("stale_wake_revision")
                    if not resolved or resolved["session"] != session:
                        raise ValueError("session_changed")
                    row = self._change_wake_locked(
                        action,
                        target,
                        session,
                        at,
                        zone,
                        intent,
                        revision=revision,
                        partial=True,
                        repeat_fields=repeat_fields,
                        day_fields=day_fields,
                        item_id=item_id,
                        version=version,
                    )
                    label = {
                        "repeat_set": "重复计划已保存",
                        "repeat_pause": "重复计划已暂停",
                        "repeat_resume": "重复计划从未来继续",
                        "repeat_delete": "重复计划已删除",
                        "day_add": "今天事项已添加",
                        "day_update": "今天事项已修改",
                        "day_cancel": "未来事项已取消；不代表现实已完成",
                    }.get(action) or (
                        "已保存预约"
                        if action == "set"
                        else "已取消待执行预约"
                        if row and row["status"] == "cancelled"
                        else "没有待取消预约，状态未改变"
                    )
                    self._management_operation = (
                        label + " · " + resolved["name"] + " · " + local_time(time.time())
                    )
                    return {"success": True, "message": self._management_operation}
        except (ValueError, OSError, TimeoutError) as exc:
            code = str(exc)
            detail = ERRORS.get(
                code,
                "预约存储或会话查询不可用，状态未确认"
                if isinstance(exc, (OSError, TimeoutError))
                else "参数或预约索引无效，未覆盖原状态",
            )
            self._management_operation = "操作未完成：" + detail
            self.ctx.logger.warning("主动唤醒管理：%s", detail)
            raise ValueError(detail) from None

    async def management_snapshot(self):
        from .dayplan import get_day, item_version

        async with asyncio.timeout(7):
            async with self._wake_lock:
                data = self._get_wakes().read()
                epoch = self._management_epoch
                now, cfg = time.time(), self.config.scheduler
                running = self._scheduler_task is not None and not self._scheduler_task.done()
                enabled = self._automatic_enabled() and self._wake_ready and running
                opening = next_activity(now, cfg.start_time, cfg.end_time)
                targets = (
                    [(True, str(t).strip()) for t in self.config.target.allowed_groups]
                    + [(False, str(t).strip()) for t in self.config.target.allowed_friends]
                    + [(False, target) for target in self._telegram_targets()]
                )
                targets = list(dict.fromkeys((g, t) for g, t in targets if text(t, 128)))
                truncated = len(targets) > 128
                base, reservations, current_private, current_sessions = [], [], set(), {}
                for group, target in targets[:128]:
                    identity = self._target_view(target)
                    plans_only = identity["platform"] == "telegram"
                    stream = await self._management_stream(target, group)
                    if epoch != self._management_epoch:
                        raise ValueError("配置或生命周期已改变，请刷新")
                    # Configuration can change while the Host query is in flight.
                    allowed = (
                        self.config.target.allowed_groups
                        if group
                        else self.config.target.allowed_friends
                    )
                    if (not group and not self._plan_allowed(target)) or (
                        group and target not in {str(v).strip() for v in allowed}
                    ):
                        continue
                    state = self._states.get((group, target))
                    if state and (not stream or state.stream_id != stream["session"]):
                        state = None
                    row = (
                        None
                        if group
                        else selected_single(data, target, stream["session"] if stream else None)
                    )
                    plan = None if group else data.get("repeats", {}).get(target)
                    day = None if group else data.get("dayplans", {}).get(target)
                    if not group and stream:
                        current_private.add(target)
                        current_sessions[target] = stream["session"]
                    observation = self._observations.get((group, target), {})
                    if not stream or observation.get("session") != stream["session"]:
                        observation = {}
                    cooldown = max(
                        (state.last_proactive_time if state else 0)
                        + cfg.min_interval_between_chats * 60,
                        max(self._target_guard(data, target), data["last_dispatch"] + 60)
                        if not group
                        else 0,
                    )
                    last_message = observation.get("last_message")
                    isolation = (
                        last_message + cfg.isolation_time * 60
                        if last_message is not None and cfg.isolation_time > 0
                        else 0
                    )
                    pending = row is not None and row["status"] in PENDING
                    near_plan = bool(
                        stream
                        and not group
                        and self._near_plan(data, target, stream["session"], now)
                    )
                    reason = (
                        "插件或调度已关闭"
                        if not enabled
                        else "真实会话未就绪"
                        if not stream
                        else "临近计划，短暂避让"
                        if near_plan
                        else "活动时段外"
                        if opening > now
                        else "最小间隔未到"
                        if cooldown > now
                        else "真人静默期未到"
                        if isolation > now
                        else "等待基础周期检查"
                    )
                    if plans_only:
                        reason = "仅计划唤醒" if enabled and stream else reason
                    last = self._submission_results.get((group, target))
                    if not stream or (last and last.get("session") != stream["session"]):
                        last = None
                    last_text = (
                        (
                            local_time(last["at"])
                            + " · "
                            + STATUSES.get(last["status"], last["status"])
                        )
                        if last
                        else "尚无本进程提交记录"
                    )
                    if row and row["attempted"] and (not last or row["attempted"] > last["at"]):
                        old_outcome = (
                            row["status"]
                            if row["status"] in {"submitted", "rejected", "unknown"}
                            else ""
                        )
                        outcome = STATUSES.get(
                            row.get("last_outcome", old_outcome), "此前预约尝试，旧版未保留结果"
                        )
                        last_text = local_time(row["attempted"]) + " · " + outcome
                    execution_rows = [
                        r
                        for r in (
                            *singles(data, target),
                            row.get("repeat_last") if row else None,
                            plan["last"] if plan else None,
                            *(
                                day["items"].values()
                                if day and stream and day["session"] == stream["session"]
                                else []
                            ),
                            *(
                                day["retained"]
                                if day and stream and day["session"] == stream["session"]
                                else []
                            ),
                        )
                        if r and r["attempted"]
                    ]
                    latest_execution = max(
                        execution_rows, key=lambda r: r["attempted"], default=None
                    )
                    planner_state = "尚无计划提交记录"
                    if latest_execution:
                        if (
                            latest_execution["attempted"]
                            and (not row or latest_execution["attempted"] >= row["attempted"])
                            and (not last or latest_execution["attempted"] >= last["at"])
                        ):
                            last_text = (
                                local_time(latest_execution["attempted"])
                                + " · "
                                + STATUSES.get(
                                    latest_execution.get(
                                        "last_outcome", latest_execution["status"]
                                    ),
                                    "结果未知",
                                )
                            )
                        planner_state = (
                            "Planner请求已到达插件Hook · "
                            + local_time(latest_execution["planner_requested_at"])
                            if latest_execution.get("planner_requested_at")
                            else "旧记录显示临时提示已提供，未记录请求时刻"
                            if latest_execution["shown"]
                            else "Host已入队，尚未观测到Planner请求"
                            if latest_execution["status"] == "submitted"
                            else "Host结果未知，不自动重放"
                            if latest_execution["status"] == "unknown"
                            else STATUSES.get(latest_execution["status"], "等待下一轮")
                        )
                    if (
                        plan
                        and plan["last"]
                        and plan["last"]["attempted"]
                        and (not last or plan["last"]["attempted"] >= last["at"])
                        and (not row or plan["last"]["attempted"] >= row["attempted"])
                    ):
                        previous = plan["last"]
                        last_text = (
                            local_time(previous["attempted"]) + " · " + STATUSES[previous["status"]]
                        )
                    retired = row.get("repeat_last") if row else None
                    if (
                        retired
                        and (not last or retired["attempted"] >= last["at"])
                        and (
                            not plan
                            or not plan["last"]
                            or retired["attempted"] >= plan["last"]["attempted"]
                        )
                    ):
                        last_text = (
                            local_time(retired["attempted"]) + " · " + STATUSES[retired["status"]]
                        )
                    base.append(
                        {
                            **identity,
                            "target": target,
                            "chat": (stream["name"] if stream else "会话未就绪")
                            + ("（群聊）" if group else "（私聊）")
                            + " · "
                            + target,
                            "kind": "群聊" if group else "私聊",
                            "state": reason,
                            "next": "—"
                            if plans_only
                            else local_time(self._next_regular_at)
                            if enabled and stream and self._next_regular_at
                            else "正在检查"
                            if enabled and stream
                            else "—",
                            "rules": "仅单次与重复计划"
                            if plans_only
                            else cfg.start_time
                            + "–"
                            + cfg.end_time
                            + f" · {cfg.check_interval}±{cfg.jitter}分钟 · 概率{cfg.probability:g}",
                            "guard": "仅计划安全间距"
                            if plans_only
                            else (
                                "冷却至 " + local_time(cooldown) if cooldown > now else "冷却已满足"
                            )
                            + (
                                "；上次检查静默至 " + local_time(isolation)
                                if isolation > now
                                else "；静默未要求"
                                if cfg.isolation_time == 0
                                else "；静默由下次实际检查核验"
                            ),
                            "last": last_text,
                        }
                    )
                    if group or not stream:
                        continue
                    active_row = row if row and row["session"] == stream["session"] else None
                    active_plan = plan if plan and plan["session"] == stream["session"] else None
                    earliest = (
                        max(
                            row["due"],
                            row["retry"],
                            self._target_guard(data, target),
                            data["last_dispatch"] + 60,
                            now,
                        )
                        if active_row and pending
                        else None
                    )
                    can_save = enabled and self._wake_ready
                    reservations.append(
                        {
                            **identity,
                            "plans_only": plans_only,
                            "chat": stream["name"]
                            + (" · TG " + identity["account_id"] if plans_only else " · QQ")
                            + " · "
                            + identity["user_id"],
                            "target": target,
                            "session": stream["session"],
                            "revision": wake_revision(
                                target, stream["session"], single_revision(data, target), plan, day
                            ),
                            "singles": [
                                {
                                    **r,
                                    "item_id": r["id"],
                                    "version": item_version(r),
                                    "can_edit": r["status"] in PENDING and r["due"] > now,
                                    "can_cancel": r["status"] in PENDING,
                                    "can_delete": r["status"] not in PENDING,
                                }
                                for r in sorted(singles(data, target), key=lambda r: r["due"])
                                if r["session"] == stream["session"] and not r.get("dismissed")
                            ],
                            "at": active_row["at"] if active_row else "—",
                            "zone": active_row["timezone"] if active_row else "—",
                            "intent": active_row["intent"] if active_row else "—",
                            "preview": active_row["intent"][:32]
                            + ("…" if len(active_row["intent"]) > 32 else "")
                            if active_row
                            else "—",
                            "status": STATUSES.get(active_row["status"], "无预约")
                            if active_row
                            else "旧会话预约不再适用"
                            if row
                            else "无预约",
                            "reason": REASONS.get(active_row["block"], "未知阻挡原因")
                            if active_row
                            else "—",
                            "earliest": local_time(max(now, earliest))
                            if earliest and enabled
                            else "调度关闭，保留预约"
                            if earliest
                            else "—",
                            "last": last_text,
                            "planner_state": planner_state,
                            "dayplan": None
                            if plans_only
                            else get_day(data, target, stream["session"], now),
                            "plan_safety_until": local_time(self._target_guard(data, target))
                            if self._target_guard(data, target) > now
                            else "已满足60秒安全间距",
                            "cannot_save": not can_save,
                            "cannot_delete": not self._wake_ready or self._wake_inflight == target,
                            "cannot_cancel": not self._wake_ready
                            or not active_row
                            or active_row["status"] not in PENDING,
                            "single": {
                                key: active_row[key]
                                for key in (
                                    "id",
                                    "at",
                                    "timezone",
                                    "intent",
                                    "status",
                                    "block",
                                    "due",
                                    "retry",
                                )
                            }
                            if active_row
                            else None,
                            "repeat": {
                                "kind": active_plan["kind"],
                                "timezone": active_plan["timezone"],
                                "intent": active_plan["intent"],
                                "interval_minutes": active_plan["interval"] // 60,
                                "local_time": active_plan["clock"],
                                "enabled": active_plan["enabled"],
                                "state": active_plan["state"],
                                "next_at": active_plan["current"]["at"],
                                "next_due": active_plan["current"]["due"],
                                "status": STATUSES[active_plan["current"]["status"]],
                                "reason": REASONS.get(active_plan["current"]["block"], "—"),
                                "retry": active_plan["current"]["retry"],
                                "last_skip": REASONS.get(
                                    active_plan.get("last_skip", {}).get("reason", ""), "—"
                                ),
                            }
                            if active_plan
                            else None,
                        }
                    )
                hidden = [
                    {
                        "chat": "已撤权或失效目标 · " + target,
                        "status": "已停止调度"
                        if row["status"] in PENDING
                        else STATUSES[row["status"]],
                        "reason": "不展示旧意图；保留原记录",
                    }
                    for target in sorted(set(data["slots"]) | set(data.get("appointments", {})))
                    for row in [selected_single(data, target)]
                    if target not in current_private
                ]
                hidden.extend(
                    {
                        "chat": "已撤权或失效重复目标 · " + target,
                        "status": "已停止调度",
                        "reason": "不展示旧意图；保留原记录",
                    }
                    for target in data.get("repeats", {})
                    if target not in current_private and target not in data["slots"]
                )
                hidden.extend(
                    {
                        "chat": "已撤权或失效日程目标 · " + target,
                        "status": "已停止调度",
                        "reason": "不展示旧意图；保留原记录",
                    }
                    for target in data.get("dayplans", {})
                    if target not in current_private
                    and target not in data["slots"]
                    and target not in data.get("repeats", {})
                )
                cleanup = []
                for target in sorted(
                    set(data["slots"]) | set(data["appointments"]) | set(data["dayplans"])
                ):
                    day = data["dayplans"].get(target)
                    records = [("single_delete", r, False) for r in singles(data, target)]
                    if day:
                        from .dayplan import day_key

                        ended = day["date"] < day_key(now, day["timezone"])
                        records.extend(("day_delete", r, ended) for r in day["items"].values())
                        records.extend(("day_delete", r, True) for r in day["retained"])
                    for action, record, ended in records:
                        retained = day and record in day["retained"]
                        if (
                            record.get("dismissed")
                            or (record["status"] in PENDING and not ended)
                            or (current_sessions.get(target) == record["session"] and not retained)
                        ):
                            continue
                        cleanup.append(
                            {
                                **self._target_view(target),
                                "target": target,
                                "session": record["session"],
                                "revision": wake_revision(
                                    target,
                                    record["session"],
                                    single_revision(data, target),
                                    data["repeats"].get(target),
                                    day,
                                ),
                                "action": action,
                                "item_id": record["id"],
                                "version": item_version(record),
                                "at": record["at"],
                                "timezone": record["timezone"],
                                "status": "expired"
                                if record["status"] in PENDING
                                else record["status"],
                                "can_delete": self._wake_ready and self._wake_inflight != target,
                            }
                        )
                return {
                    "base": base,
                    "reservations": reservations,
                    "inactive": hidden,
                    "cleanup": cleanup,
                    "state": "自动调度运行中" if enabled else "自动调度已关闭或未运行",
                    "deadline": local_time(self._next_regular_at) if enabled else "—",
                    "pass": local_time(self._next_pass_at) if enabled else "—",
                    "phase": {
                        "stopped": "已停止",
                        "starting": "等待首次检查",
                        "resolving": "解析真实目标",
                        "checking": "检查中",
                        "waiting": "等待下次检查",
                        "unresolved": "等待会话就绪",
                        "error": "检查异常，等待恢复",
                    }.get(self._scheduler_phase, "未知"),
                    "limits": "已显示前128个授权目标"
                    if truncated
                    else f"{len(base)}个基础目标 · {len(reservations)}个可查看私聊",
                    "operation": self._management_operation,
                    "checked_at": now,
                    "rules": cfg.start_time
                    + "–"
                    + cfg.end_time
                    + f" · 周期{cfg.check_interval}±{cfg.jitter}分钟 · 概率{cfg.probability:g} · 最小间隔{cfg.min_interval_between_chats}分钟 · 静默{cfg.isolation_time}分钟",
                }
