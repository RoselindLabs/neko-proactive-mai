"""Simple model inputs and parallel cadence, synthetic clocks/Host only."""

import asyncio
import copy
from datetime import datetime
import importlib
import unittest
from unittest.mock import Mock

import test_wakes as wakes
import test_chat_sdk as chats
from test_wakes import NOW

times = importlib.import_module("wake_test.time_input")


class SimpleTimeTests(unittest.TestCase):
    def test_beijing_relative_clock_tomorrow_and_dated_inputs(self):
        for raw, seconds in (("1分钟后", 60), ("10分钟后", 600), ("2小时后", 7200)):
            value = datetime.fromisoformat(times.simple_instant(raw, NOW))
            self.assertEqual(value.timestamp(), NOW + seconds)
            self.assertEqual(value.utcoffset().total_seconds(), 28800)
        now = datetime.fromisoformat("2026-10-30T10:00:00+08:00").timestamp()
        for raw, expected in (
            ("21:30", "2026-10-30T21:30:00+08:00"),
            ("明天09:00", "2026-10-31T09:00:00+08:00"),
            ("2026-11-01 09:00", "2026-11-01T09:00:00+08:00"),
        ):
            self.assertEqual(times.simple_instant(raw, now), expected)

    def test_today_is_not_tomorrow_or_automatic_rollover(self):
        now = datetime.fromisoformat("2026-10-30T23:59:30+08:00").timestamp()
        for value in (
            "00:01",
            "23:59",
            "明天09:00",
            "2026-10-31 09:00",
            "10分钟后",
            "25:00",
            True,
            "x" * 65,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                times.simple_instant(value, now, today_only=True)
        with self.assertRaises(ValueError):
            times.simple_instant("00:01", now)

    def test_today_hhmm_boundary_and_bad_inputs(self):
        now = datetime.fromisoformat("2026-10-30T10:00:00+08:00").timestamp()
        self.assertEqual(
            times.simple_instant("10:01", now, today_only=True), "2026-10-30T10:01:00+08:00"
        )
        for raw in ("0分钟后", "99999小时后", "下周五", "2026-02-30 12:00", "2026-11-30 10:00"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                times.simple_instant(raw, now)


class SimpleToolTests(unittest.IsolatedAsyncioTestCase):
    setUp = chats.ChatSDKTests.setUp
    asyncSetUp = chats.ChatSDKTests.asyncSetUp
    identity = chats.ChatSDKTests.identity
    selected = chats.ChatSDKTests.selected

    async def test_real_sdk_exposes_small_parameter_sets_and_today_in_primary_description(self):
        components = {row["name"]: row["metadata"] for row in self.bot.get_components()}
        expected = {
            "neko_proactive_wake": {"action", "at", "intent", "item_id", "version"},
            "neko_proactive_repeat": {"action", "interval_minutes", "local_time", "intent"},
            "neko_proactive_dayplan": {"action", "at", "intent", "item_id", "version"},
        }
        for name, names in expected.items():
            self.assertEqual({p["name"] for p in components[name]["parameters"]}, names)
            time_parameter = "local_time" if name == "neko_proactive_repeat" else "at"
            parameters = {p["name"]: p for p in components[name]["parameters"]}
            self.assertIn("北京时间", parameters[time_parameter]["description"])
        self.assertIn("不能设置明天", components["neko_proactive_dayplan"]["description"])

    async def test_model_simple_inputs_create_without_timezone_or_kind(self):
        # Real PluginContext handles raw chat RPC in this fixture.
        await self.bot._init_target_streams()
        host = {
            "contract": "neko.tools.context.v1",
            "platform": "qq",
            "account_id": "bot",
            "user_id": "friend",
            "group_id": "",
            "session_id": "session-friend",
            "stream_id": "session-friend",
        }
        result = await self.bot.private_wake(
            "set", at="10分钟后", intent="依据", _host_context=host
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["wake"]["due"], NOW + 600)
        result = await self.bot.private_repeat(
            "set", interval_minutes=120, intent="依据", _host_context=host
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["repeat"]["kind"], "interval")
        result = await self.bot.private_repeat(
            "set", local_time="21:00", intent="依据", _host_context=host
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["repeat"]["timezone"], "Asia/Shanghai")
        result = await self.bot.private_dayplan(
            "add", at="23:00", intent="今天想整理", _host_context=host
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["dayplan"]["timezone"], "Asia/Shanghai")
        before = self.bot._get_wakes().path.read_bytes()
        result = await self.bot.private_dayplan(
            "add", at="明天23:00", intent="拒绝", _host_context=host
        )
        self.assertFalse(result["success"])
        self.assertIn("今天", result["message"])
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)
        result = await self.bot.private_repeat(
            "set", interval_minutes=10, local_time="21:00", intent="拒绝", _host_context=host
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)

    async def test_admin_today_time_uses_same_business_cas(self):
        result = await self.bot.wake_edit(
            "day_add",
            **self.identity(await self.selected()),
            when="23:00",
            timezone="Asia/Shanghai",
            intent="管理员当天事项",
        )
        self.assertTrue(result["success"], result)
        row = await self.selected()
        self.assertEqual(row["dayplan"]["items"][0]["at"], "2026-11-01T23:00:00+08:00")

    async def test_old_timezones_and_unknown_survive_reads_and_simple_edits(self):
        await self.bot._init_target_streams()
        host = {
            "contract": "neko.tools.context.v1",
            "platform": "qq",
            "account_id": "bot",
            "user_id": "friend",
            "group_id": "",
            "session_id": "session-friend",
            "stream_id": "session-friend",
        }
        old = await self.bot.private_repeat(
            "set",
            kind="interval",
            interval_minutes=120,
            timezone="UTC",
            intent="旧依据",
            _host_context=host,
        )
        before = copy.deepcopy(old["repeat"])
        result = await self.bot.private_repeat("set", intent="仅改依据", _host_context=host)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["repeat"]["timezone"], "UTC")
        self.assertEqual(result["repeat"]["anchor"], before["anchor"])
        self.assertEqual(result["repeat"]["current"]["due"], before["current"]["due"])
        from test_wakes import iso

        result = await self.bot.private_dayplan(
            "add", at=iso(NOW + 300), timezone="UTC", intent="旧日程", _host_context=host
        )
        self.assertTrue(result["success"], result)
        original = copy.deepcopy(result["dayplan"]["items"])
        result = await self.bot.private_dayplan(
            "add", at="23:00", intent="新的北京时间事项", _host_context=host
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["dayplan"]["timezone"], "UTC")
        self.assertEqual(result["dayplan"]["items"][:1], original)
        self.assertEqual(result["dayplan"]["items"][1]["at"], "2026-11-01T15:00:00+00:00")


class ParallelCadenceTests(unittest.IsolatedAsyncioTestCase):
    setUp = wakes.WakeTests.setUp
    reserve = wakes.WakeTests.reserve
    tool = wakes.WakeTests.tool
    rows = wakes.WakeTests.rows
    due = wakes.WakeTests.due

    async def test_far_single_and_daily_repeat_do_not_suppress_or_mutate_plans(self):
        await self.reserve(7200)
        result = await self.bot.private_repeat(
            "set", local_time="23:30", intent="晚上续聊", _host_context=self.host
        )
        self.assertTrue(result["success"], result)
        before = copy.deepcopy(self.bot._get_wakes().read())
        await self.bot._check_and_trigger(self.state, "friend")
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        after = self.bot._get_wakes().read()
        self.assertEqual(after["slots"], before["slots"])
        self.assertEqual(after["repeats"], before["repeats"])
        self.assertEqual(after["last_dispatch"], NOW)
        # A later normal cycle still runs even though both plans remain enabled.
        self.clock_mock.return_value = NOW + 61
        self.state.last_proactive_time = 0
        await self.bot._check_and_trigger(self.state, "friend")
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 2)

    async def test_near_single_and_near_repeat_still_yield_without_consuming(self):
        await self.reserve(60)
        before = self.bot._get_wakes().path.read_bytes()
        await self.bot._check_and_trigger(self.state, "friend")
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)
        await self.tool("cancel")
        await self.bot.private_repeat(
            "set", interval_minutes=1, intent="依据", _host_context=self.host
        )
        before = self.bot._get_wakes().path.read_bytes()
        await self.bot._check_and_trigger(self.state, "friend")
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)

    async def test_default_rpc_releases_mutex_serializes_and_persists_unknown_spacing(self):
        await self.reserve(600)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def trigger(**kwargs):
            self.assertFalse(self.bot._wake_lock.locked())
            self.assertEqual(self.bot._get_wakes().read()["last_dispatch"], NOW)
            # Simulates a planner tool before Host ACK; it fails busy, not deadlock.
            result = await self.tool("set", at="5分钟后", intent="新计划")
            self.assertFalse(result["success"])
            entered.set()
            await finish.wait()
            return {}  # Unknown cannot immediately bypass durable spacing.

        self.bot.ctx.maisaka.proactive.trigger.side_effect = trigger
        task = asyncio.create_task(self.bot._check_and_trigger(self.state, "friend"))
        await asyncio.wait_for(entered.wait(), 2)
        await self.bot._run_wakes()
        await self.bot._check_and_trigger(self.state, "friend")
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 1)
        finish.set()
        await asyncio.wait_for(task, 2)
        self.assertIsNone(self.bot._wake_inflight)
        await self.bot._check_and_trigger(self.state, "friend")
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 1)
        self.assertEqual(self.rows()["friend"]["status"], "scheduled")

    async def test_default_storage_failure_and_cancel_never_rearm_explicit_plan(self):
        await self.reserve(600)
        before = self.rows()
        write = self.bot._get_wakes().write
        self.bot._get_wakes().write = Mock(side_effect=OSError("disk"))
        with self.assertRaises(OSError):
            await self.bot._check_and_trigger(self.state, "friend")
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.bot._get_wakes().write = write
        self.bot.ctx.maisaka.proactive.trigger.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.bot._check_and_trigger(self.state, "friend")
        self.assertIsNone(self.bot._wake_inflight)
        self.assertEqual(self.rows(), before)
