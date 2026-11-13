"""Neko Proactive Mai — 基于 Maisaka Planner 主动找话题。

不再自行构建 prompt 和调用 LLM，而是通过 maisaka.proactive.trigger()
将群聊冷场或私聊生活分享的独立意图注入 Planner 上下文，由 Maisaka 基于
人格、记忆、上下文和工具自行决定是否回复及如何表达。
"""

from __future__ import annotations

import asyncio
import random
import time
import json
from datetime import datetime, time as datetime_time
from typing import Dict, Optional, Tuple

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config import GROUP_PROACTIVE_INTENT, PRIVATE_PROACTIVE_INTENT, LonelyMaiConfig
from .prompt_visibility import filter_request, pending_offers, inject_offers
from .wake_store import (
    CONTRACT,
    PENDING,
    WakeStore,
    text,
    singles,
    all_singles,
    single_revision,
    selected_single,
    target_identity,
    telegram_key,
)
from .webui import WakeManagement
from .time_input import DEFAULT_ZONE, simple_instant


# ==================== 工具函数 ====================


def parse_time(time_str: str) -> datetime_time:
    hour, minute = map(int, time_str.split(":"))
    return datetime_time(hour, minute)


def is_in_time_range(current: datetime_time, start: datetime_time, end: datetime_time) -> bool:
    if start <= end:
        return start <= current <= end
    else:  # 跨夜
        return current >= start or current <= end


# ==================== 聊天流状态 ====================


class ChatStreamState:
    def __init__(self, stream_id: str, target_id: str, *, is_group: bool = False):
        self.stream_id = stream_id
        self.target_id = target_id
        self.platform, self.account_id, self.user_id = target_identity(target_id)
        self.is_group = is_group
        self.last_proactive_time: float = 0.0


# ==================== 插件主类 ====================


class LonelyMaiPlugin(WakeManagement, MaiBotPlugin):
    config_model = LonelyMaiConfig

    def __init__(self):
        super().__init__()
        self._states: Dict[Tuple[bool, str], ChatStreamState] = {}
        self._scheduler_task: Optional[asyncio.Task] = None
        self._bot_qq: str = ""
        self._lifecycle_lock = asyncio.Lock()
        self._life = None
        self._wake_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._wakes = None
        self._wake_ready = False
        self._next_regular_at = None
        self._next_pass_at = None
        self._scheduler_phase = "stopped"
        self._observations = {}
        self._submission_results = {}
        self._management_operation = "尚无管理操作"
        self._management_epoch = 0
        self._wake_inflight = None

    # ========== 生命周期 ==========

    async def on_load(self) -> None:
        self._get_wakes().recover(time.time())
        # Runner may reactivate this same unloaded instance on a failed reload.
        # Its closed bridges have drained; reopen lazily from the durable ledger.
        self._life = None
        self._bilibili = None
        self._wake_ready = True
        await self._sync_scheduler()

    async def on_unload(self) -> None:
        from .life_bridge import finish_owned

        self._management_epoch = getattr(self, "_management_epoch", 0) + 1
        self._wake_ready = False

        async def cleanup():
            async with self._lifecycle_lock:
                await self._stop_scheduler()
                for bridge in (self._life, getattr(self, "_bilibili", None)):
                    if bridge is not None:
                        await bridge.close()

        await finish_owned(cleanup())
        self.ctx.logger.info("Neko Proactive Mai 已卸载")

    async def on_config_update(self, *args, **kwargs):
        del args, kwargs
        self._management_epoch = getattr(self, "_management_epoch", 0) + 1
        await self._sync_scheduler()

    async def _stop_scheduler(self) -> None:
        task = self._scheduler_task
        self._scheduler_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._next_regular_at = self._next_pass_at = None
        self._scheduler_phase = "stopped"

    async def _sync_scheduler(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_scheduler()
            # Management stays readable when automatic scheduling is disabled.
            self._bot_qq = await self._get_global_str("bot.qq_account", "")
            if not self.config.plugin.enabled or not self.config.scheduler.enabled:
                self.ctx.logger.info("Neko Proactive Mai 调度器已禁用")
                return
            # Keep cooldown state; only the scheduler resolves targets, avoiding
            # concurrent replacement of states during an in-flight trigger.
            self._scheduler_task = asyncio.create_task(self._schedule_loop())
            self._scheduler_phase = "starting"
            self.ctx.logger.info("Neko Proactive Mai 调度器已启动")

    async def _get_global_str(self, key: str, default: str = "") -> str:
        try:
            val = await self.ctx.config.get(key, default)
        except Exception as e:
            self.ctx.logger.warning(f"读取全局配置 {key} 失败: {e}")
            return default
        if isinstance(val, dict):
            val = val.get("value", default)
        return str(val or default)

    async def _init_target_streams(self) -> None:
        previous = {key: state.last_proactive_time for key, state in self._states.items()}
        resolved: Dict[Tuple[bool, str], ChatStreamState] = {}

        for group_qq in self.config.target.allowed_groups:
            target_id = str(group_qq).strip()
            if not target_id:
                continue
            sid = await self._resolve_session_id(target_id, True)
            if sid:
                key = (True, target_id)
                state = ChatStreamState(sid, target_id, is_group=True)
                state.last_proactive_time = previous.get(key, 0.0)
                resolved[key] = state
            else:
                if (True, target_id) in self._states:
                    state = ChatStreamState("", target_id, is_group=True)
                    state.last_proactive_time = previous[(True, target_id)]
                    resolved[(True, target_id)] = state
                self.ctx.logger.warning(f"无法解析群聊: {target_id}")

        for user_qq in self.config.target.allowed_friends:
            target_id = str(user_qq).strip()
            if not target_id:
                continue
            sid = await self._resolve_session_id(target_id, False)
            if sid:
                key = (False, target_id)
                state = ChatStreamState(sid, target_id, is_group=False)
                state.last_proactive_time = previous.get(key, 0.0)
                resolved[key] = state
            else:
                if (False, target_id) in self._states:
                    state = ChatStreamState("", target_id)
                    state.last_proactive_time = previous[(False, target_id)]
                    resolved[(False, target_id)] = state
                self.ctx.logger.warning(f"无法解析私聊: {target_id}")

        for target_id in self._telegram_targets():
            sid = await self._resolve_session_id(target_id, False)
            key = (False, target_id)
            # Plan-only states are in the original map, never the base cycle.
            state = ChatStreamState(sid or "", target_id)
            state.last_proactive_time = previous.get(key, 0.0)
            resolved[key] = state

        changed = set(resolved) != set(self._states)
        self._states = resolved
        if changed or not resolved:
            self.ctx.logger.info(f"共注册 {len(self._states)} 个目标流")

    async def _resolve_session_id(self, qq: str, is_group: bool) -> Optional[str]:
        try:
            # One strict parser for scheduling, tools and management. Missing or
            # untrusted identity is unavailable, never authority to use a cached ID.
            stream = await self._management_stream(qq, is_group)
        except Exception as e:
            self.ctx.logger.error(f"ctx.chat 查询失败 ({qq}): {e}")
            return None
        return stream["session"] if stream else None

    # ========== 调度循环 ==========

    def _get_wakes(self):
        if self._wakes is None:
            self._wakes = WakeStore(self.ctx.paths.data_dir)
        return self._wakes

    def _automatic_enabled(self):
        return self.config.plugin.enabled and self.config.scheduler.enabled

    def _guard_until(self, row):
        return max(row["attempted"], row.get("guard", 0)) + 60

    def _target_guard(self, data, target):
        rows = [
            *singles(data, target),
            data.get("repeats", {}).get(target),
            data.get("dayplans", {}).get(target),
        ]
        return max((self._guard_until(row) for row in rows if row), default=0)

    @Tool(
        "neko_proactive_wake",
        description="单次预约/定时提醒：在当前授权私聊稍后续聊或回顾，可保留多条；每天或每隔N天用neko_proactive_repeat。已有安排先get，再按item_id/version修改或取消，不要每轮新增。到时可沉默，不是可靠闹钟；unknown不可为补发重加。",
        brief_description="单次预约/定时提醒：在当前授权私聊稍后续聊或回顾，可保留多条；每天或每隔N天用neko_proactive_repeat。已有安排先get，再按item_id/version修改或取消，不要每轮新增。到时可沉默，不是可靠闹钟；unknown不可为补发重加。",
        parameters=[
            ToolParameterInfo(
                name="action",
                description="get查看；add新增且不覆盖其他预约，必填at和intent；update仅修改未来未尝试项；cancel仅取消待执行项",
                enum_values=["get", "add", "update", "cancel"],
            ),
            ToolParameterInfo(
                name="at",
                description="北京时间，未来1分钟至7天；可填10分钟后、HH:MM、明天09:00或日期时间。HH:MM只指今天，过时不顺延。add必填，update省略保留原时刻",
                required=False,
            ),
            ToolParameterInfo(
                name="intent",
                description="1～240字话题与依据；add必填，update省略保留",
                required=False,
            ),
            ToolParameterInfo(
                name="item_id",
                description="get返回的完整预约ID，update/cancel必填；add不填",
                required=False,
            ),
            ToolParameterInfo(
                name="version",
                description="get返回的该条预约版本，update/cancel必填；add不填",
                required=False,
            ),
        ],
        platforms=["qq", "telegram"],
        chat_scope="private",
        trusted_context=CONTRACT,
    )
    async def private_wake(
        self, action="get", at="", timezone="", intent="", item_id="", version="", **kwargs
    ):
        from .webui import tool_result

        try:
            async with self._wake_lock:
                if not self._wake_ready or not self._automatic_enabled():
                    raise ValueError("wake_disabled")
                identity = await self._repeat_identity(kwargs)
                if not identity:
                    raise ValueError("trusted_private_context_required")
                target, session = identity
                row = self._change_wake_locked(
                    action,
                    target,
                    session,
                    at,
                    timezone,
                    intent,
                    simple=not timezone,
                    item_id=item_id,
                    version=version,
                )
                from .dayplan import item_version

                rows = sorted(
                    (
                        r
                        for r in singles(self._get_wakes().read(), target)
                        if r["session"] == session
                    ),
                    key=lambda r: r["due"],
                )
                view = [{**r, "item_id": r["id"], "version": item_version(r)} for r in rows]
                return tool_result(
                    "wake",
                    {
                        "success": True,
                        "wake": row,
                        "items": view,
                        "message": {
                            "get": "当前单次预约列表",
                            "set": "单次预约已新增",
                            "add": "单次预约已新增",
                            "update": "这条单次预约已修改",
                            "cancel": "这条单次预约已取消",
                        }[action],
                        "next_at": next((r["at"] for r in rows if r["status"] in PENDING), None),
                    },
                    selected=row["id"] if row and action != "get" else None,
                )
        except (ValueError, OSError) as exc:
            from .webui import ERRORS

            return tool_result(
                "wake",
                {
                    "success": False,
                    "error": str(exc)
                    if isinstance(exc, ValueError)
                    else "wake_storage_unavailable",
                    "message": ERRORS.get(
                        str(exc), "单次预约未完成，请核对当前私聊、北京时间和依据"
                    ),
                },
            )

    @Tool(
        "neko_proactive_repeat",
        description="重复预约/计划提醒：每天、每隔N天或固定间隔续聊/回顾；一次性用neko_proactive_wake。当前授权私聊只保留一份重复计划，不支持指定星期或每月。已有计划先get再修改，不必每轮设置。到时可沉默，不是可靠闹钟；unknown不可为补发重加。",
        brief_description="重复预约/计划提醒：每天、每隔N天或固定间隔续聊/回顾；一次性用neko_proactive_wake。当前授权私聊只保留一份重复计划，不支持指定星期或每月。已有计划先get再修改，不必每轮设置。到时可沉默，不是可靠闹钟；unknown不可为补发重加。",
        parameters=[
            ToolParameterInfo(
                name="action",
                description="get查看；set设置/修改，首次必填intent及interval_minutes或local_time二选一，修改时省略字段保留，编辑暂停计划不自动resume；pause暂停；resume继续；delete删除",
                enum_values=["get", "set", "pause", "resume", "delete"],
            ),
            ToolParameterInfo(
                name="interval_minutes",
                description="间隔1～10080分钟：每5分钟填5，每2小时填120，每隔2天填2880",
                param_type=ToolParamType.INTEGER,
                required=False,
            ),
            ToolParameterInfo(
                name="local_time",
                description="每天北京时间HH:MM，例如21:00；与interval_minutes二选一",
                required=False,
            ),
            ToolParameterInfo(
                name="intent",
                description="1～240字话题/依据；首次set必填，修改省略保留；不是预写正文",
                required=False,
            ),
        ],
        platforms=["qq", "telegram"],
        chat_scope="private",
        trusted_context=CONTRACT,
    )
    async def private_repeat(
        self,
        action="get",
        kind="",
        interval_minutes=0,
        local_time="",
        timezone="",
        intent="",
        at="",
        **kwargs,
    ):
        from .webui import tool_result

        # Same trusted caller path; never accept a model-supplied target/session.
        if action not in ("get", "set", "pause", "resume", "delete"):
            return tool_result(
                "repeat",
                {
                    "success": False,
                    "message": "请选择get、set、pause、resume或delete",
                    "error": "invalid_action",
                },
            )
        async with self._wake_lock:
            probe = await self._repeat_identity(kwargs)
            if not probe:
                return tool_result(
                    "repeat", {"success": False, "message": "当前没有可用的已授权私聊上下文"}
                )
            target, session = probe
            try:
                plan = self._change_wake_locked(
                    "repeat_" + action,
                    target,
                    session,
                    repeat_fields={
                        "kind": kind,
                        "interval_minutes": interval_minutes,
                        "local_time": local_time,
                        "timezone": timezone,
                        "intent": intent,
                        "at": at,
                    },
                    simple=not timezone,
                )
                current = plan["current"] if plan else None
                return tool_result(
                    "repeat",
                    {
                        "success": True,
                        "message": {
                            "get": "当前重复联系节奏",
                            "set": "重复计划已保存",
                            "pause": "重复计划已暂停",
                            "resume": "重复计划从未来继续",
                            "delete": "重复计划已删除，保留已有防重间隔",
                        }[action],
                        "repeat": plan,
                        "next_at": current["at"]
                        if plan and plan["enabled"] and action != "delete"
                        else None,
                    },
                    selected=action,
                )
            except (ValueError, OSError) as exc:
                from .webui import ERRORS

                return tool_result(
                    "repeat",
                    {
                        "success": False,
                        "message": ERRORS.get(
                            str(exc), "重复计划未保存，请检查间隔、北京时间和意图"
                        ),
                        "error": str(exc)
                        if isinstance(exc, ValueError)
                        else "wake_storage_unavailable",
                    },
                )

    async def _repeat_identity(self, kwargs):
        host = kwargs.get("_host_context")
        if (
            not self._wake_ready
            or not self._automatic_enabled()
            or not isinstance(host, dict)
            or host.get("contract") != CONTRACT
            or host.get("platform") not in ("qq", "telegram")
            or host.get("group_id")
        ):
            return None
        target, session = host.get("user_id"), host.get("session_id")
        if (
            not text(target, 128)
            or not text(session, 128)
            or host.get("stream_id") != session
            or target != target.strip()
            or session != session.strip()
        ):
            return None
        account = self._bot_qq
        if host["platform"] == "telegram":
            account = self.config.target.telegram_account_id
            try:
                target = telegram_key(account, target)
            except ValueError:
                return None
        elif target.startswith("telegram:"):
            return None
        if host.get("account_id") and host["account_id"] != account:
            return None
        epoch = self._management_epoch
        try:
            async with asyncio.timeout(7):
                actual = await self._management_stream(target, False, session=session)
        except Exception:
            return None
        state = ChatStreamState(session, target)
        if (
            not self._wake_ready
            or not self._automatic_enabled()
            or not self._is_authorized(state, target)
            or epoch != self._management_epoch
            or not actual
            or actual["session"] != session
        ):
            return None
        return target, session

    @Tool(
        "neko_proactive_dayplan",
        brief_description="今天日程/计划提醒：安排今天稍后整理、关注或继续的话题，不能设置明天或以后。最多6项，可留空；计划不代表已发生经历。已有安排先get，再按item_id/version修改或取消，不要每轮新增。到时可沉默，不是可靠闹钟；unknown不可为补发重加。",
        parameters=[
            ToolParameterInfo(
                name="action",
                description="get查看；add添加，必填at和intent；update修改、cancel取消，均仅限未来未尝试项",
                enum_values=["get", "add", "update", "cancel"],
            ),
            ToolParameterInfo(
                name="at",
                description="今天北京时间HH:MM，如21:30；至少1分钟后，过时不顺延。add必填，update省略保留",
                required=False,
            ),
            ToolParameterInfo(
                name="intent",
                description="1～240字事项及依据；add必填，update省略保留；计划不代表已做过",
                required=False,
            ),
            ToolParameterInfo(
                name="item_id",
                description="get返回的完整事项ID，update/cancel必填；add不填",
                required=False,
            ),
            ToolParameterInfo(
                name="version",
                description="该item_id当前版本，update/cancel必填；变化后重新get；add不填",
                required=False,
            ),
        ],
        platforms=["qq"],
        chat_scope="private",
        trusted_context=CONTRACT,
    )
    async def private_dayplan(
        self, action="get", at="", timezone="", intent="", item_id="", version="", **kwargs
    ):
        from .dayplan import get_day
        from .webui import ERRORS, tool_result

        selected = None
        try:
            async with self._wake_lock:
                epoch = self._management_epoch
                host = kwargs.get("_host_context")
                if not isinstance(host, dict) or host.get("platform") != "qq":
                    raise ValueError("private_chat_not_authorized")
                identity = await self._repeat_identity(kwargs)
                if not identity:
                    raise ValueError("trusted_private_context_required")
                target, session = identity
                actual = await self._management_stream(target, False)
                state = ChatStreamState(session, target)
                if (
                    not actual
                    or actual["session"] != session
                    or epoch != self._management_epoch
                    or not self._wake_ready
                    or not self._automatic_enabled()
                    or not self._is_authorized(state, target)
                ):
                    raise ValueError("trusted_private_context_required")
                if action == "get":
                    result = get_day(
                        self._get_wakes().read(),
                        target,
                        session,
                        time.time(),
                        timezone or DEFAULT_ZONE,
                    )
                elif action in ("add", "update", "cancel"):
                    changed = self._change_wake_locked(
                        "day_" + action,
                        target,
                        session,
                        day_fields={
                            "at": at,
                            "timezone": timezone,
                            "intent": intent,
                            "item_id": item_id,
                            "version": version,
                        },
                        simple=not timezone,
                    )
                    selected = changed["id"]
                    result = get_day(self._get_wakes().read(), target, session, time.time())
                else:
                    raise ValueError("invalid_action")
                return tool_result(
                    "dayplan",
                    {
                        "success": True,
                        "message": {
                            "get": "今天日程，留空也正常",
                            "add": "当天事项已添加",
                            "update": "当天事项已修改",
                            "cancel": "当天事项已取消",
                        }[action],
                        "dayplan": result,
                    },
                    selected=selected,
                )
        except (ValueError, OSError) as exc:
            return tool_result(
                "dayplan",
                {
                    "success": False,
                    "message": ERRORS.get(
                        str(exc), "当天事项未完成，请核对今天的北京时间、依据和版本"
                    ),
                    "error": str(exc)
                    if isinstance(exc, ValueError)
                    else "wake_storage_unavailable",
                },
            )

    def _change_wake_locked(
        self,
        action,
        target,
        session,
        at="",
        zone="",
        intent="",
        *,
        revision=None,
        partial=False,
        repeat_fields=None,
        day_fields=None,
        simple=False,
        item_id="",
        version="",
    ):
        """Both trusted model and authenticated admin use this lock-held mutation."""
        from .webui import wake_revision

        if not self._wake_ready:
            raise ValueError("wake_disabled")
        if self._wake_inflight == target and action not in ("get", "repeat_get", "day_get"):
            raise ValueError("wake_submission_inflight")
        if not self._plan_allowed(target):
            raise ValueError("private_chat_not_authorized")
        if action.startswith("day_") and target_identity(target)[0] != "qq":
            raise ValueError("telegram_dayplan_not_supported")
        store = self._get_wakes()
        data = store.read()
        row = data["slots"].get(target)
        repeat = data.get("repeats", {}).get(target)
        day = data.get("dayplans", {}).get(target)
        now = round(time.time(), 6)
        if revision is not None and revision != wake_revision(
            target, session, single_revision(data, target), repeat, day
        ):
            raise ValueError("stale_wake_revision")
        if action.startswith("day_"):
            from .dayplan import change_day

            simple_day = simple or (
                isinstance((day_fields or {}).get("at"), str) and len(day_fields["at"]) == 5
            )
            if simple_day:
                day_fields = dict(day_fields or {})
                zone = day["timezone"] if day else DEFAULT_ZONE
                day_fields["timezone"] = zone
            if action != "day_cancel" and not self._automatic_enabled():
                raise ValueError("wake_disabled")
            result = change_day(
                data,
                target,
                session,
                now,
                action,
                preserve_minute=partial,
                simple=simple_day,
                **(day_fields or {}),
            )
            store.write(data)
            self._wake_event.set()
            return result
        if action.startswith("repeat_"):
            from .repeat import set_plan, change_plan

            if action in ("repeat_set", "repeat_resume") and not self._automatic_enabled():
                raise ValueError("wake_disabled")
            if repeat and repeat["session"] != session and action != "repeat_set":
                raise ValueError("session_changed")
            if action == "repeat_get":
                return repeat
            if action == "repeat_set":
                fields = dict(repeat_fields or {})
                if simple:
                    if fields.get("interval_minutes") and fields.get("local_time"):
                        raise ValueError("simple_repeat_choice")
                    fields["kind"] = (
                        "daily"
                        if fields.get("local_time")
                        else "interval"
                        if fields.get("interval_minutes")
                        else fields.get("kind", "")
                    )
                    # Intent-only edits preserve old anchors/timezones without migration.
                    fields["timezone"] = (
                        DEFAULT_ZONE
                        if fields.get("interval_minutes") or fields.get("local_time") or not repeat
                        else repeat["timezone"]
                    )
                    if fields.get("at"):
                        fields["at"] = simple_instant(fields["at"], now)
                plan = set_plan(data, target, session, now, **fields)
            else:
                plan = change_plan(data, target, action, time.time())
            store.write(data)
            self._wake_event.set()
            return plan
        from .dayplan import item_version

        if action == "set":
            action = "update" if item_id else "add"
        if action in ("update", "cancel"):
            if not text(item_id, 32) or not text(version, 64):
                raise ValueError("single_identity_required")
            row = next((r for r in singles(data, target) if r["id"] == item_id), None)
            if not row:
                raise ValueError("single_not_found")
            if row["session"] != session:
                raise ValueError("session_changed")
            if version != item_version(row):
                raise ValueError("stale_wake_revision")
            if (
                row["status"] not in PENDING
                and not (action == "cancel" and row["status"] == "cancelled")
            ) or (action == "update" and row["due"] <= now):
                raise ValueError("single_future_only")
        if action in ("add", "update"):
            if action == "add":
                row = None
                if item_id or version:
                    raise ValueError("add_identity_not_allowed")
            partial = action == "update"
            if simple and at:
                at, zone = simple_instant(at, now), DEFAULT_ZONE
            if not self._automatic_enabled():
                raise ValueError("wake_disabled")
            if partial and row and row["status"] in PENDING and at and zone == row["timezone"]:
                moment = datetime.fromisoformat(at)
                if moment.utcoffset() is not None and int(moment.timestamp() // 60) == int(
                    row["due"] // 60
                ):
                    at = ""  # Prefilled minute unchanged: edit purpose without moving a blocked occurrence.
            keep_time = (
                partial
                and row
                and row["status"] in PENDING
                and row["session"] == session
                and not at
            )
            if partial and row and row["status"] in PENDING and row["session"] == session:
                at, zone, intent = at or row["at"], zone or row["timezone"], intent or row["intent"]
            row = store.set(
                data,
                target,
                session,
                at,
                zone,
                intent,
                now,
                keep_time=bool(keep_time),
                wake_id=row["id"] if row else "",
            )
        elif action == "cancel":
            if row and row["status"] in PENDING:
                row.update(status="cancelled", block="")
                store.write(data)
        elif action == "get":
            row = selected_single(data, target, session)
        else:
            raise ValueError("invalid_action")
        if action != "get":
            self._wake_event.set()
        return row

    @API("wake_overview", description="只读主动检查与私聊预约管理状态", public=False)
    async def wake_overview(self):
        return await self.management_snapshot()

    @API("wake_save", description="管理员新增或按ID修改当前授权私聊预约", public=False)
    async def wake_save(
        self,
        target="",
        session="",
        revision="",
        at="",
        timezone="",
        intent="",
        item_id="",
        version="",
    ):
        return await self.management_change(
            "set", target, session, revision, at, timezone, intent, item_id=item_id, version=version
        )

    @API("wake_cancel", description="管理员取消当前授权私聊的待执行预约，不重放", public=False)
    async def wake_cancel(self, target="", session="", revision="", item_id="", version=""):
        return await self.management_change(
            "cancel", target, session, revision, item_id=item_id, version=version
        )

    @API("wake_edit", description="认证管理页编辑单次/重复计划；无执行或重放操作", public=False)
    async def wake_edit(
        self,
        action="",
        target="",
        session="",
        revision="",
        at="",
        when="",
        timezone="",
        intent="",
        kind="",
        interval_minutes=0,
        local_time="",
        item_id="",
        version="",
    ):
        from .webui import wall_instant, ERRORS

        try:
            if action not in (
                "single_save",
                "single_cancel",
                "single_delete",
                "repeat_set",
                "repeat_pause",
                "repeat_resume",
                "repeat_delete",
                "day_add",
                "day_update",
                "day_cancel",
                "day_delete",
            ):
                raise ValueError("invalid_action")
            if action in ("single_save", "day_add", "day_update") and when:
                if at:
                    raise ValueError("invalid_management_request")
                at = (
                    when
                    if action.startswith("day_") and len(when) == 5
                    else wall_instant(when, timezone or DEFAULT_ZONE)
                )
            if action in ("single_delete", "day_delete"):
                result = await self.management_change(
                    action, target, session, revision, item_id=item_id, version=version
                )
            elif action.startswith("day_"):
                result = await self.management_change(
                    action,
                    target,
                    session,
                    revision,
                    day_fields={
                        "at": at,
                        "timezone": timezone,
                        "intent": intent,
                        "item_id": item_id,
                        "version": version,
                    },
                )
            elif action.startswith("repeat_"):
                result = await self.management_change(
                    action,
                    target,
                    session,
                    revision,
                    repeat_fields={
                        "at": at,
                        "timezone": timezone,
                        "intent": intent,
                        "kind": kind,
                        "interval_minutes": interval_minutes,
                        "local_time": local_time,
                    },
                )
            else:
                result = await self.management_change(
                    "set" if action == "single_save" else "cancel",
                    target,
                    session,
                    revision,
                    at,
                    timezone,
                    intent,
                    item_id=item_id,
                    version=version,
                )
            return result
        except (ValueError, OSError) as exc:
            return {
                "success": False,
                "message": ERRORS.get(
                    str(exc), str(exc) if isinstance(exc, ValueError) else "预约存储不可用"
                ),
            }

    async def _run_wakes(self):
        async with self._wake_lock:
            if not self._wake_ready or not self._automatic_enabled() or self._wake_inflight:
                return
            store = self._get_wakes()
            data = store.read()
            from .repeat import advance, expires
            from .dayplan import rotate, active_items

            jobs = [(target, row, None, False) for target, row in all_singles(data)]
            for target, dayplan in data.get("dayplans", {}).items():
                changed = rotate(dayplan, time.time())
                if changed:
                    store.write(data)
                jobs.extend(
                    (target, row, None, True)
                    for row in dayplan["items"].values()
                    if row["status"] in PENDING
                )
            for target, plan in data.get("repeats", {}).items():
                if not plan["enabled"]:
                    continue
                if time.time() >= expires(plan):
                    advance(plan, time.time(), "expired", "repeat_window_missed")
                    store.write(data)
                jobs.append((target, plan["current"], plan, False))
            dispatched = False
            for target, row, plan, is_day in sorted(
                jobs, key=lambda item: (item[1]["due"], not item[3])
            ):
                now = time.time()
                if row["status"] not in PENDING:
                    continue
                state = self._states.get((False, target))
                if not self._is_authorized(ChatStreamState(row["session"], target), target) or (
                    state and state.stream_id and row["session"] != state.stream_id
                ):
                    row.update(status="revoked", block="authorization_or_session_changed")
                    if plan:
                        plan.update(enabled=False, state="revoked")
                    store.write(data)
                    continue
                if row["due"] > now or row["retry"] > now:
                    continue
                if now - row["due"] >= 86400:
                    row.update(status="expired", block="blocked_24h")
                    store.write(data)
                    continue
                if not state or not state.stream_id:
                    row.update(status="blocked", block="session_unavailable", retry=now + 60)
                    store.write(data)
                    continue
                if (plan or is_day) and any(
                    r["status"] in PENDING and r["session"] == state.stream_id
                    for r in singles(data, target)
                ):
                    row.update(status="blocked", block="single_priority", retry=now + 60)
                    store.write(data)
                    continue
                if is_day:
                    nearby = [
                        r
                        for r in active_items(data, target)
                        if r["status"] in PENDING
                        and r["session"] == state.stream_id
                        and r["due"] <= row["due"] + 60
                    ]
                    merge_at = max((r["due"] for r in nearby), default=row["due"])
                    if merge_at > now:
                        row.update(status="blocked", block="day_merge_wait", retry=merge_at)
                        store.write(data)
                        continue
                if plan and any(
                    r["status"] in PENDING
                    and r["session"] == state.stream_id
                    and r["due"] <= now + 60
                    for r in active_items(data, target)
                ):
                    advance(plan, now, "expired", "dayplan_priority")
                    store.write(data)
                    continue
                self._note_check(state, "appointment")
                # Query session fresh; config/lifecycle may change during Host reads.
                resolved = await self._resolve_session_id(target, False)
                if not self._wake_ready or not self._automatic_enabled():
                    return
                if not self._is_authorized(state, target) or (
                    resolved is not None and resolved != row["session"]
                ):
                    row.update(status="revoked", block="authorization_or_session_changed")
                    if plan:
                        plan.update(enabled=False, state="revoked")
                    store.write(data)
                    continue
                if resolved is None:
                    row.update(
                        status="blocked", block="session_unavailable", retry=time.time() + 60
                    )
                    store.write(data)
                    continue
                # All rule reads occur after async Host lookups, using the latest
                # config/time. A hot change must not open a stale admission window.
                now = time.time()
                if is_day and rotate(data["dayplans"][target], now):
                    store.write(data)
                    continue
                if plan and now >= expires(plan):
                    advance(plan, now, "expired", "repeat_window_missed")
                    store.write(data)
                    continue
                if now - row["due"] >= 86400:
                    row.update(status="expired", block="blocked_24h")
                    store.write(data)
                    continue
                block = ""
                if now < self._target_guard(data, target):
                    block = "safety_spacing"
                elif dispatched or now < data["last_dispatch"] + 60:
                    block = "dispatch_spacing"
                if block:
                    self._note_check(state, block)
                    row.update(status="blocked", block=block, retry=now + 60)
                    store.write(data)
                    continue
                # Commit the uncertainty boundary BEFORE any Host submission.
                # Cancellation, process loss, malformed results never replay this ID.
                row.update(
                    status="unknown",
                    last_outcome="unknown",
                    attempted=now,
                    block="",
                    task="",
                    shown=False,
                )
                merged = [row]
                if is_day:
                    merged = [
                        r
                        for r in active_items(data, target)
                        if r["status"] in PENDING
                        and r["due"] <= now
                        and r["session"] == state.stream_id
                    ]
                    if row not in merged:
                        merged.append(row)
                    for member in merged:
                        member.update(
                            status="unknown",
                            last_outcome="unknown",
                            attempted=now,
                            block="",
                            task="",
                            shown=False,
                            batch_id=row["id"],
                        )
                    data["dayplans"][target]["attempted"] = now
                data["last_dispatch"] = now
                if plan:
                    plan["attempted"] = now
                    advance(plan, now)
                store.write(data)
                dispatched = True
                self._record_submission(state, "unknown", "appointment", now)
                wake_id = row["id"]
                intent = self._intent_for(state)
                reason = self.config.intent.reason
                self._wake_inflight = target
                # Host can run our blocking Planner hook before RPC acknowledgement.
                # Never hold its storage lock across that bidirectional boundary.
                self._wake_lock.release()
                result = None
                try:
                    result = await self.ctx.maisaka.proactive.trigger(
                        stream_id=state.stream_id,
                        intent=intent,
                        reason=reason,
                        metadata={
                            "source": "lonelymai",
                            "target_id": state.user_id,
                            "platform": state.platform,
                            "account_id": state.account_id or self._bot_qq,
                            "chat_kind": "private",
                            "neko_wake_id": wake_id,
                        },
                    )
                except Exception:
                    self.ctx.logger.warning("私聊预约提交结果未知；不自动重放")
                finally:
                    await self._wake_lock.acquire()
                    self._wake_inflight = None
                # The hook/another target's admin write can have changed the index.
                # Reread, update only this immutable occurrence, never stale overwrite.
                data = store.read()
                row = self._find_wake(data, target, wake_id)
                if row is None:
                    return
                batch = (
                    [r for r in self._wake_records(data, target) if r.get("batch_id") == wake_id]
                    if is_day
                    else [row]
                )
                for member in batch:
                    if isinstance(result, dict) and result.get("success") is True:
                        member["status"] = "submitted"
                        member["last_outcome"] = "submitted"
                        task = result.get("task_id")
                        if (
                            text(task, 160)
                            and task.startswith("proactive:neko.proactive:")
                            and not member["task"]
                        ):
                            member["task"] = task
                    elif isinstance(result, dict) and result.get("success") is False:
                        member["status"] = "rejected"
                        member["last_outcome"] = "rejected"
                store.write(data)
                self._record_submission(state, row["status"], "appointment", now)
                return  # One admitted Host dispatch per pass; reread next pass.

    @staticmethod
    def _wake_records(data, target):
        slot = data["slots"].get(target)
        plan = data.get("repeats", {}).get(target)
        day = data.get("dayplans", {}).get(target)
        return [
            row
            for row in (
                *singles(data, target),
                slot.get("repeat_last") if slot else None,
                plan.get("last") if plan else None,
                *(day["items"].values() if day else []),
                *(day["retained"] if day else []),
            )
            if row
        ]

    @staticmethod
    def _find_wake(data, target, wake_id):
        for row in LonelyMaiPlugin._wake_records(data, target):
            if row and row["id"] == wake_id:
                return row
        return None

    def _wake_delay(self):
        now = time.time()
        data = self._get_wakes().read()
        rows = [
            *(row for _, row in all_singles(data)),
            *(plan["current"] for plan in data.get("repeats", {}).values() if plan["enabled"]),
            *(row for day in data.get("dayplans", {}).values() for row in day["items"].values()),
        ]
        return min(
            (max(1, max(r["due"], r["retry"]) - now) for r in rows if r["status"] in PENDING),
            default=86400,
        )

    def _next_sleep_seconds(self) -> int:
        base_interval = max(1, self.config.scheduler.check_interval)
        jitter = self.config.scheduler.jitter
        actual_minutes = (
            base_interval + random.randint(-jitter, jitter) if jitter > 0 else base_interval
        )
        return max(60, actual_minutes * 60)

    async def _schedule_loop(self) -> None:
        next_regular = 0
        while True:
            try:
                if not self.config.plugin.enabled or not self.config.scheduler.enabled:
                    await asyncio.sleep(60)
                    continue

                self._scheduler_phase = "resolving"
                self._next_pass_at = None
                self._wake_event.clear()
                await self._init_target_streams()
                if not any(state.stream_id for state in self._states.values()):
                    await self._run_wakes()
                    self._scheduler_phase = "unresolved"
                    self._next_pass_at = time.time() + 15
                    self.ctx.logger.warning("没有可用目标流，15 秒后重新解析")
                    await asyncio.sleep(15)
                    continue

                regular = time.monotonic() >= next_regular
                self._scheduler_phase = "checking"
                for state in list(self._states.values()) if regular else []:
                    if not state.stream_id or state.platform != "qq":
                        continue
                    target_id = state.target_id
                    allowed = (
                        self.config.target.allowed_groups
                        if state.is_group
                        else self.config.target.allowed_friends
                    )
                    if target_id not in allowed:
                        continue

                    try:
                        await self._check_and_trigger(state, target_id)
                    except Exception as e:
                        self.ctx.logger.error(f"[{target_id}] 检查异常: {e}", exc_info=True)

                if regular:
                    next_regular = time.monotonic() + self._next_sleep_seconds()
                    self._next_regular_at = (
                        time.time() + max(0, next_regular - time.monotonic())
                        if any(s.platform == "qq" and s.stream_id for s in self._states.values())
                        else None
                    )
                await self._run_wakes()
                sleep_seconds = min(max(1, next_regular - time.monotonic()), self._wake_delay())
                self._scheduler_phase = "waiting"
                self._next_pass_at = time.time() + sleep_seconds
                if self.config.log.scheduler_log:
                    self.ctx.logger.info(f"[调度器] 下次检查: +{int(sleep_seconds // 60)}min")
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=sleep_seconds)
                except TimeoutError:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._scheduler_phase = "error"
                self._next_pass_at = time.time() + 60
                self.ctx.logger.error(f"调度器异常: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _check_and_trigger(self, state: ChatStreamState, target_id: str) -> None:
        if state.platform != "qq":
            return
        if not state.is_group:
            async with self._wake_lock:
                data = self._get_wakes().read()
                now = time.time()
                if (
                    self._wake_inflight
                    or self._near_plan(data, target_id, state.stream_id, now)
                    or now < max(self._target_guard(data, target_id), data["last_dispatch"] + 60)
                ):
                    self._note_check(state, "plan_spacing")
                    return
            # Never carry the storage mutex into Host RPC / Planner hooks.
        await self._check_default_and_trigger(state, target_id)

    def _near_plan(self, data, target, session, now):
        from .dayplan import active_items

        repeat = data.get("repeats", {}).get(target)
        rows = [
            *singles(data, target),
            repeat["current"] if repeat and repeat["enabled"] else None,
            *active_items(data, target),
        ]
        return any(
            row
            and row["status"] in PENDING
            and row["session"] == session
            and row["due"] <= now + 60
            for row in rows
        )

    async def _check_default_and_trigger(self, state: ChatStreamState, target_id: str) -> None:
        if state.platform != "qq":
            return
        cfg = self.config
        now = datetime.now()
        self._note_check(state, "checking")
        current_time = now.time()
        start = parse_time(cfg.scheduler.start_time)
        end = parse_time(cfg.scheduler.end_time)

        if not is_in_time_range(current_time, start, end):
            self._note_check(state, "activity_hours")
            self._skip(target_id, f"不在 {cfg.scheduler.start_time}-{cfg.scheduler.end_time}")
            return

        min_interval = cfg.scheduler.min_interval_between_chats * 60
        if (
            state.last_proactive_time
            and (now.timestamp() - state.last_proactive_time) < min_interval
        ):
            remain = int(min_interval - (now.timestamp() - state.last_proactive_time))
            self._note_check(state, "cooldown")
            self._skip(target_id, f"距上次主动还有 {remain} 秒")
            return

        isolation_seconds = cfg.scheduler.isolation_time * 60
        if isolation_seconds > 0:
            last_msg_time = await self._get_last_msg_time(state.stream_id)
            self._note_check(state, "checking", last_message=last_msg_time)
            if last_msg_time is not None and (now.timestamp() - last_msg_time) < isolation_seconds:
                self._skip(target_id, "隔离期未到")
                self._note_check(state, "isolation", last_message=last_msg_time)
                return

        if random.random() >= cfg.scheduler.probability:
            self._note_check(state, "probability")
            self._skip(target_id, f"概率未命中 ({cfg.scheduler.probability})")
            return

        self.ctx.logger.info(f"[{target_id}] 触发主动聊天")
        self._note_check(state, "eligible")
        guarded = not state.is_group and self._wake_ready
        if guarded:
            async with self._wake_lock:
                data = self._get_wakes().read()
                stamp = time.time()
                if (
                    self._wake_inflight
                    or not self._automatic_enabled()
                    or not self._is_authorized(state, target_id)
                    or self._near_plan(data, target_id, state.stream_id, stamp)
                    or stamp < max(self._target_guard(data, target_id), data["last_dispatch"] + 60)
                ):
                    self._note_check(state, "plan_spacing")
                    return
                # Same durable global 60s fence as explicit plans. No plan is
                # consumed or moved by a regular check, even on unknown RPCs.
                data["last_dispatch"] = stamp
                self._get_wakes().write(data)
                self._wake_inflight = target_id
        try:
            if await self._do_proactive_chat(state, target_id):
                state.last_proactive_time = now.timestamp()
        finally:
            if guarded:
                async with self._wake_lock:
                    self._wake_inflight = None

    # ========== 消息时间查询 ==========

    async def _get_last_msg_time(self, session_id: str) -> Optional[float]:
        """获取最近一条非 bot 消息的时间戳。"""
        try:
            end_ts = time.time()
            start_ts = end_ts - 3600 * 24 * 7
            raw = await self.ctx.message.get_by_time_in_chat(
                session_id,
                start_time=str(start_ts),
                end_time=str(end_ts),
                limit=1,
                limit_mode="latest",
                filter_mai=True,
            )
            if not isinstance(raw, list):
                return None

            for msg in raw:
                if not isinstance(msg, dict):
                    continue
                sender_id = self._get_msg_user_id(msg)
                if self._bot_qq and sender_id == self._bot_qq:
                    continue
                ts = msg.get("timestamp", 0)
                try:
                    return float(ts)
                except (TypeError, ValueError):
                    pass
            return None
        except Exception:
            return None

    # ========== 主动聊天核心 ==========

    @HookHandler(
        "maisaka.planner.before_request",
        name="lonelymai_latest_planner_task",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def filter_planner_tasks(self, **kwargs):
        modified, removed = filter_request(kwargs)
        items = modified.get("items")
        offers = []
        if (
            isinstance(items, list)
            and self._wake_ready
            and self.config.plugin.enabled
            and self.config.life.enabled
        ):
            try:
                from .life_bridge import LifeBridge

                if self._life is None:
                    self._life = LifeBridge(self.ctx)
                async with asyncio.timeout(2):
                    for record in pending_offers(kwargs):
                        state = next(
                            (s for s in self._states.values() if s.stream_id == record["scope"]),
                            None,
                        )
                        if (
                            state is None
                            or state.platform != "qq"
                            or not self._is_authorized(state, state.target_id)
                        ):
                            continue
                        result = await self._life.call(
                            "prompt",
                            {
                                "session_id": record["scope"],
                                "receipt_id": record["receipt"],
                                "host_task": record["task"],
                            },
                        )
                        candidate = result.get("candidate")
                        if (
                            isinstance(candidate, dict)
                            and self._is_authorized(state, state.target_id)
                            and self.config.life.enabled
                        ):
                            offers.append(
                                (
                                    record["scope"],
                                    record["receipt"],
                                    {**candidate, "_intent": self._intent_for(state)},
                                )
                            )
            except Exception:
                pass  # Missing/expired state never resurrects an old candidate or sends anything.
        if offers:
            modified["items"] = inject_offers(items, offers)
        bili_offers = []
        if (
            isinstance(items, list)
            and self._wake_ready
            and self.config.plugin.enabled
            and self.config.bilibili.enabled
        ):
            try:
                from .bilibili_bridge import BilibiliBridge
                from .prompt_visibility import inject_bilibili_offers

                bridge = getattr(self, "_bilibili", None)
                if bridge is None:
                    bridge = self._bilibili = BilibiliBridge(self.ctx)
                async with asyncio.timeout(3):
                    for record in pending_offers(kwargs, source="bilibili"):
                        state = next(
                            (s for s in self._states.values() if s.stream_id == record["scope"]),
                            None,
                        )
                        if (
                            state is None
                            or state.platform != "qq"
                            or not self._is_authorized(state, state.target_id)
                        ):
                            continue
                        result = await bridge.call(
                            "prompt",
                            {
                                "session_id": record["scope"],
                                "receipt_id": record["receipt"],
                                "host_task": record["task"],
                            },
                        )
                        if (
                            isinstance(result.get("candidate"), dict)
                            and self._is_authorized(state, state.target_id)
                            and self.config.bilibili.enabled
                        ):
                            bili_offers.append(
                                (record["scope"], record["receipt"], result["candidate"])
                            )
                if bili_offers:
                    modified["items"] = inject_bilibili_offers(
                        modified.get("items", items), bili_offers
                    )
            except Exception:
                pass  # Source outage never grants another chat or resurrects material.
        wake_items = await self._wake_prompt(kwargs)
        if wake_items:
            modified["items"] = [*modified.get("items", []), *wake_items]
        if removed or offers or bili_offers or wake_items:
            return {"action": "continue", "modified_kwargs": modified}
        return None

    async def _wake_prompt(self, kwargs):
        from .prompt_visibility import EPHEMERAL, route_scopes, task_record
        from datetime import timezone

        if not self._wake_ready or not self._automatic_enabled():
            return []
        request_items = kwargs.get("items")
        if not isinstance(request_items, list) or not any(
            (record := task_record(item)) and record["wake"] for item in request_items
        ):
            return []  # Ordinary/default requests must not wait on plan storage.
        try:
            async with self._wake_lock:
                store = self._get_wakes()
                data, scopes = store.read(), route_scopes(kwargs)
                items = kwargs.get("items")
                if not isinstance(items, list):
                    return []
                for item in reversed(items):
                    record = task_record(item)
                    if not record or not record["wake"]:
                        continue
                    scope = record["scope"] or kwargs.get("session_id", "")
                    targets = (
                        set(data["slots"])
                        | set(data.get("appointments", {}))
                        | set(data.get("repeats", {}))
                        | set(data.get("dayplans", {}))
                    )
                    records = [
                        (target, row)
                        for target in targets
                        for row in self._wake_records(data, target)
                    ]
                    for target, row in records:
                        state = self._states.get((False, target))
                        if (
                            scope not in scopes
                            or row["session"] != scope
                            or row["id"] != record["wake"]
                            or (row.get("batch_id") and row["batch_id"] != record["wake"])
                            or row["status"] not in ("unknown", "submitted")
                            or (row["task"] and row["task"] != record["task"])
                            or row["shown"]
                            or not 0 <= time.time() - row["attempted"] < 180
                            or state is None
                            or state.stream_id != scope
                            or not self._is_authorized(state, target)
                        ):
                            continue
                        batch = (
                            [
                                member
                                for member in self._wake_records(data, target)
                                if member.get("batch_id") == row["id"]
                            ]
                            if row.get("batch_id") == row["id"]
                            else [row]
                        )
                        if not batch or any(
                            member["shown"]
                            or member["session"] != scope
                            or member["status"] not in ("unknown", "submitted")
                            or (member["task"] and member["task"] != record["task"])
                            for member in batch
                        ):
                            continue
                        requested_at = time.time()
                        for member in batch:
                            member["task"] = record["task"]
                            member["shown"] = True
                            member["planner_requested_at"] = requested_at
                        store.write(data)
                        return [
                            {
                                "item_type": "SystemMessageItem",
                                "meta": {
                                    "item_id": EPHEMERAL + row["id"],
                                    "logical_turn_id": None,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                },
                                "parts": [
                                    {
                                        "type": "text",
                                        "text": "本轮私聊自主预约意图（不是用户的新消息）："
                                        + json.dumps(
                                            {
                                                "session_id": scope,
                                                "at": row["at"],
                                                "timezone": row["timezone"],
                                                "intent": row["intent"]
                                                if len(batch) == 1
                                                else [member["intent"] for member in batch],
                                            },
                                            ensure_ascii=False,
                                        )
                                        + "。结合最新上下文判断意图是否过时；可不发言，不承诺提醒已送达。",
                                    }
                                ],
                            }
                        ]
        except (ValueError, OSError):
            self.ctx.logger.warning("私聊预约临时上下文不可用")
        return []

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="lonelymai_latest_replyer_task",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def filter_replyer_tasks(self, **kwargs):
        return self._filter_proactive_request(kwargs)

    def _filter_proactive_request(self, kwargs):
        modified, removed = filter_request(kwargs)
        if not removed:
            return None
        self.ctx.logger.debug("本次请求过滤 %s 条旧主动任务，仅保留最新提示", removed)
        return {"action": "continue", "modified_kwargs": modified}

    def _intent_for(self, state: ChatStreamState) -> str:
        cfg = self.config.intent
        if state.is_group:
            return cfg.group_text.strip() or GROUP_PROACTIVE_INTENT
        return cfg.private_text.strip() or PRIVATE_PROACTIVE_INTENT

    def _is_authorized(self, state: ChatStreamState, target_id: str) -> bool:
        # Cached states preserve cooldowns, but are never an authorization source.
        if state.platform == "telegram":
            return (
                self.config.plugin.enabled
                and not state.is_group
                and bool(state.stream_id)
                and state.target_id == target_id
                and self._plan_allowed(target_id)
            )
        allowed = (
            self.config.target.allowed_groups
            if state.is_group
            else self.config.target.allowed_friends
        )
        return (
            self.config.plugin.enabled
            and bool(state.stream_id)
            and state.target_id == target_id
            and target_id in {str(value).strip() for value in allowed}
        )

    async def _do_proactive_chat(self, state: ChatStreamState, target_id: str) -> bool:
        """通过 Maisaka 主动触发 API 非显式注入意图到 Planner 上下文。

        群聊和私聊各选自己的意图，空白时使用各自默认。
        """
        if (
            state.platform != "qq"
            or not self._wake_ready
            or not self._is_authorized(state, target_id)
        ):
            self._skip(target_id, "插件已禁用或目标已撤权")
            return False
        candidate = None
        candidate_bridge = None
        reason = self.config.intent.reason
        metadata = {
            "source": "lonelymai",
            "target_id": target_id,
            "chat_kind": "group" if state.is_group else "private",
        }
        if self.config.life.enabled:
            try:
                from .life_bridge import LifeBridge

                if self._life is None:
                    self._life = LifeBridge(self.ctx)
                async with asyncio.timeout(3):
                    await self._life.flush()
                    candidate = await self._life.claim(state.stream_id)
                if candidate:
                    candidate_bridge = self._life
                    reason += '\n<neko_life_offer receipt_id="' + candidate["receipt_id"] + '"/>'
                    metadata["neko_life_receipt_id"] = candidate["receipt_id"]
            except Exception as exc:
                candidate = None
                self.ctx.logger.warning("Life候选获取或结算不可用: %s", type(exc).__name__)
        if self.config.bilibili.enabled:
            try:
                from .bilibili_bridge import BilibiliBridge

                bridge = getattr(self, "_bilibili", None)
                if bridge is None:
                    bridge = self._bilibili = BilibiliBridge(self.ctx)
                if candidate is None:
                    async with asyncio.timeout(3):
                        await bridge.flush()
                        candidate = await bridge.claim(state.stream_id)
                    if candidate:
                        candidate_bridge = bridge
                        reason += (
                            '\n<neko_bilibili_offer receipt_id="' + candidate["receipt_id"] + '"/>'
                        )
                        metadata["neko_bilibili_receipt_id"] = candidate["receipt_id"]
            except Exception as exc:
                self.ctx.logger.warning("B站阅读素材暂不可用: %s", type(exc).__name__)
        try:
            # Life I/O yields to config updates. Recheck immediately before Host I/O;
            # an already dispatched Host request cannot be recalled by this plugin.
            resolved = await self._resolve_session_id(target_id, state.is_group)
            if (
                not self._wake_ready
                or not self._is_authorized(state, target_id)
                or resolved != state.stream_id
            ):
                if candidate:
                    await candidate_bridge.update(candidate["receipt_id"], "released")
                return False
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id=state.stream_id,
                intent=self._intent_for(state),
                reason=reason,
                metadata=metadata,
            )
            if candidate:
                task_id = (
                    result.get("task_id", "")
                    if isinstance(result, dict) and result.get("success") is True
                    else ""
                )
                if not isinstance(task_id, str) or not task_id.startswith(
                    "proactive:neko.proactive:"
                ):
                    task_id = ""
                try:
                    await candidate_bridge.update(
                        candidate["receipt_id"],
                        "submitted"
                        if task_id
                        else "released"
                        if isinstance(result, dict) and result.get("success") is False
                        else "unknown",
                        task_id,
                    )
                except Exception:
                    pass  # Durable lease/outbox prevents a second host trigger.
            if isinstance(result, dict) and result.get("success") is True:
                self._record_submission(state, "submitted", "default")
                self.ctx.logger.info(f"[{target_id}] 已注入 Planner 上下文")
                return True
            outcome = (
                "宿主拒绝"
                if isinstance(result, dict) and result.get("success") is False
                else "宿主结果未知"
            )
            self._record_submission(
                state,
                "rejected"
                if isinstance(result, dict) and result.get("success") is False
                else "unknown",
                "default",
            )
            self.ctx.logger.warning(f"[{target_id}] 主动触发失败: {outcome}")
            return False
        except Exception as e:
            self._record_submission(state, "unknown", "default")
            if candidate:
                try:
                    await candidate_bridge.update(candidate["receipt_id"], "unknown")
                except Exception:
                    pass
            self.ctx.logger.error(f"[{target_id}] 主动触发失败: {e}", exc_info=True)
            return False

    @HookHandler("send_service.after_send", name="lonelymai_life_receipt", mode=HookMode.OBSERVE)
    async def life_after_send(self, **kwargs):
        if not self._wake_ready or not self.config.life.enabled:
            return
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        try:
            if self._life is None:
                from .life_bridge import LifeBridge

                self._life = LifeBridge(self.ctx)
            await self._life.after_send(
                str(message.get("session_id") or ""),
                kwargs.get("reply_message_id"),
                kwargs.get("sent"),
            )
        except Exception:
            pass  # Pending durable receipt can be retried; never resend content.

    def _skip(self, target_id: str, reason: str) -> None:
        if self.config.log.scheduler_log:
            self.ctx.logger.info(f"[{target_id}] 跳过: {reason}")

    @staticmethod
    def _get_msg_user_id(msg: dict) -> str:
        info = msg.get("message_info") or {}
        user = info.get("user_info") or {}
        return str(user.get("user_id", "") or "")

    # ========== 手动命令 ==========

    @Command(
        "lonelymai_trigger",
        description="手动请求主动聊天，不保证发言；检查目标授权但不走自动时段/概率/冷却",
        pattern=r"^/saymai$",
    )
    async def cmd_trigger(self, stream_id: str = "", **kwargs):
        if not stream_id:
            return False, "无法获取当前会话 ID", True

        state = next((st for st in self._states.values() if st.stream_id == stream_id), None)
        if state is None or state.platform != "qq":
            return False, "当前会话未在白名单中", True

        success = await self._do_proactive_chat(state, state.target_id)
        if success:
            return True, "已触发主动发言", True
        else:
            return False, "主动发言失败", True


def create_plugin():
    return LonelyMaiPlugin()
