import asyncio
import copy
from datetime import datetime, timezone
import importlib
import json
import unittest
from unittest.mock import AsyncMock

import test_wakes as fixtures
from test_wakes import NOW, iso

repeat = importlib.import_module("wake_test.repeat")


class RepeatTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.WakeTests.setUp
    reserve = fixtures.WakeTests.reserve
    tool = fixtures.WakeTests.tool
    rows = fixtures.WakeTests.rows
    due = fixtures.WakeTests.due

    async def rep(self, action="get", **kwargs):
        return await self.bot.private_repeat(action, _host_context=self.host, **kwargs)

    async def create(self, minutes=120, **kwargs):
        result = await self.rep(
            "set",
            kind="interval",
            interval_minutes=minutes,
            timezone="UTC",
            intent="有依据地续聊",
            **kwargs,
        )
        self.assertTrue(result["success"], result)
        return result["repeat"]

    def plan(self):
        return self.bot._get_wakes().read()["repeats"]["friend"]

    async def test_interval_anchor_stable_through_attempt_duration_refresh_and_intent_edit(self):
        self.bot.config.scheduler.min_interval_between_chats = 0
        plan = await self.create(2)
        anchor = plan["anchor"]
        await self.due(125)
        self.assertEqual(self.plan()["current"]["due"], anchor + 120)
        self.clock_mock.return_value = NOW + 126
        await self.rep("set", intent="只修改依据")
        self.assertEqual(self.plan()["anchor"], anchor)
        self.assertEqual(self.plan()["current"]["due"], anchor + 120)
        self.assertEqual(self.plan()["last"]["status"], "submitted")
        await self.due(245)
        self.assertEqual(self.plan()["current"]["due"], anchor + 240)
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 2)

    async def test_explicit_priority_does_not_disable_parallel_default(self):
        await self.create(2)
        self.bot.config.scheduler.min_interval_between_chats = 0
        await self.reserve(180)
        await self.bot._check_and_trigger(self.state, "friend")
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        await self.due(120)
        self.assertEqual(self.plan()["current"]["block"], "single_priority")
        await self.due(180)
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 2)
        await self.tool("cancel")
        await self.bot._check_and_trigger(self.state, "friend")
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 2)
        await self.rep("pause")
        self.clock_mock.return_value = NOW + 241
        self.state.last_proactive_time = 0  # Default path uses original wall datetime.
        await self.bot._check_and_trigger(self.state, "friend")
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 3)

    async def test_each_outcome_consumes_occurrence_and_unknown_committed_before_rpc(self):
        self.bot.config.scheduler.min_interval_between_chats = 0
        await self.create(2)
        for stamp, result, outcome in [
            (120, {"success": True}, "submitted"),
            (240, {"success": False}, "rejected"),
            (360, {}, "unknown"),
        ]:

            async def host(**kwargs):
                plan = self.plan()
                self.assertEqual(plan["last"]["status"], "unknown")
                self.assertGreater(plan["current"]["due"], NOW + stamp)
                return result

            self.bot.ctx.maisaka.proactive.trigger.side_effect = host
            await self.due(stamp)
            self.assertEqual(self.plan()["last"]["status"], outcome)
            calls = self.bot.ctx.maisaka.proactive.trigger.await_count
            await self.due(stamp + 1)
            self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, calls)

    async def test_crash_after_boundary_does_not_replay_and_future_survives_reload(self):
        await self.create(2)
        self.bot.ctx.maisaka.proactive.trigger.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.due(120)
        plan = self.plan()
        self.assertEqual(plan["last"]["status"], "unknown")
        self.assertEqual(plan["current"]["due"], NOW + 240)
        self.bot._get_wakes().recover(NOW + 121)
        self.assertEqual(self.plan()["current"]["due"], NOW + 240)
        self.bot._get_wakes().recover(NOW + 10000)
        self.assertGreater(self.plan()["current"]["due"], NOW + 10000)
        self.assertEqual(self.plan()["last"]["status"], "unknown")
        self.assertEqual(self.plan()["last_skip"]["reason"], "restart_missed")

    async def test_blocks_skip_by_next_nominal_boundary_without_backlog(self):
        await self.create(2)
        data = self.bot._get_wakes().read()
        data["last_dispatch"] = NOW + 119
        self.bot._get_wakes().write(data)
        await self.due(120)
        self.assertEqual(self.plan()["current"]["block"], "dispatch_spacing")
        await self.due(245)
        self.assertEqual(self.plan()["last"]["status"], "expired")
        self.assertEqual(self.plan()["last"]["block"], "repeat_window_missed")
        self.assertEqual(self.plan()["current"]["due"], NOW + 360)
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_pause_resume_delete_preserve_shared_guard_and_do_not_replay(self):
        await self.create(2)
        await self.due(120)
        attempted = self.plan()["attempted"]
        await self.rep("pause")
        self.clock_mock.return_value = NOW + 125
        await self.rep("resume")
        self.assertEqual(self.plan()["attempted"], attempted)
        self.assertEqual(self.plan()["current"]["due"], NOW + 240)
        await self.rep("delete")
        self.assertNotIn("friend", self.bot._get_wakes().read()["repeats"])
        self.assertEqual(self.rows()["friend"]["guard"], attempted)
        self.state.last_proactive_time = 0
        await self.bot._check_and_trigger(self.state, "friend")
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        await self.tool("set", at=iso(NOW + 190), timezone="UTC", intent="新的单次")
        await self.due(190)
        self.assertTrue(
            any(
                r["status"] == "submitted"
                for r in fixtures.mod.singles(self.bot._get_wakes().read(), "friend")
            )
        )

    async def test_edit_paused_definition_does_not_implicitly_resume(self):
        await self.create(120)
        await self.rep("pause")
        result = await self.rep(
            "set", kind="daily", local_time="21:00", timezone="Asia/Shanghai", intent="新依据"
        )
        self.assertTrue(result["success"])
        self.assertFalse(self.plan()["enabled"])
        self.assertEqual(self.plan()["state"], "paused")

    async def test_delete_preserves_unknown_attribution_and_new_session_never_borrows_old_purpose(
        self,
    ):
        await self.create(2)
        self.bot.ctx.maisaka.proactive.trigger.return_value = {}
        await self.due(120)
        unknown = self.plan()["last"]
        await self.rep("delete")
        self.assertEqual(self.rows()["friend"]["repeat_last"]["id"], unknown["id"])
        self.assertEqual(self.rows()["friend"]["repeat_last"]["status"], "unknown")
        data = self.bot._get_wakes().read()
        repeat.set_plan(
            data,
            "friend",
            "new-session",
            NOW + 121,
            kind="interval",
            interval_minutes=2,
            timezone="UTC",
            intent="新会话自己的依据",
        )
        self.bot._get_wakes().write(data)
        self.assertNotEqual(self.plan()["intent"], unknown["intent"])
        self.assertIsNone(self.plan()["last"])

    async def test_disabled_revoked_session_and_cross_chat_cannot_create_or_submit(self):
        for host in [
            None,
            {**self.host, "group_id": "group"},
            {**self.host, "session_id": "other"},
            {**self.host, "user_id": "other"},
        ]:
            result = await self.bot.private_repeat(
                "set",
                kind="interval",
                interval_minutes=120,
                timezone="UTC",
                intent="依据",
                _host_context=host,
            )
            self.assertFalse(result["success"])
        await self.create(2)
        self.bot.config.scheduler.enabled = False
        await self.due(120)
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.bot.config.scheduler.enabled = True
        self.bot.config.target.allowed_friends = []
        await self.due(120)
        self.assertEqual(self.plan()["state"], "revoked")
        self.assertFalse(self.plan()["enabled"])

    async def test_latest_repeat_purpose_uses_existing_one_time_task_context(self):
        from test_prompt_visibility import item, task

        plan = await self.create(2)
        await self.due(120)
        submission = self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs
        self.assertNotIn(plan["intent"], json.dumps(submission, ensure_ascii=False))
        # Mock task ID is the known synthetic accepted Host task.
        text = task(1793505660000).replace(
            '{"source": "lonelymai"}', json.dumps(submission["metadata"])
        )
        source = {"session_id": "session-friend", "items": [item(text)]}
        first = await self.bot._wake_prompt(source)
        self.assertEqual(len(first), 1)
        self.assertIn(plan["intent"], first[0]["parts"][0]["text"])
        self.assertEqual(await self.bot._wake_prompt(source), [])

    async def test_legacy_single_preserved_new_format_fails_closed_in_old_reader(self):
        await self.reserve(180)
        before = copy.deepcopy(self.rows())
        await self.create(2)
        self.assertEqual(self.rows(), before)
        data = self.bot._get_wakes().read()
        self.assertEqual(data["version"], 4)
        # Pinned v2.5.1's admission contract is version == 1. A separate delivery
        # probe executes that actual historical reader; CI needs no git history.
        self.assertNotEqual(data["version"], 1)
        raw = self.bot._get_wakes().path.read_bytes()
        broken = {**data, "version": 99}
        self.bot._get_wakes().path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.bot._get_wakes().recover(NOW)
        self.assertNotEqual(self.bot._get_wakes().path.read_bytes(), raw)

    async def test_daily_plan_and_invalid_inputs(self):
        result = await self.rep(
            "set",
            kind="daily",
            local_time="21:00",
            timezone="Asia/Shanghai",
            intent="有依据的晚间联系",
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(
            datetime.fromtimestamp(
                self.plan()["current"]["due"], repeat.zone_info("Asia/Shanghai")
            ).hour,
            21,
        )
        for fields in [
            {"kind": "cron"},
            {"kind": "interval", "interval_minutes": -1},
            {"kind": "interval", "interval_minutes": True},
            {"kind": "daily", "local_time": "25:00"},
            {"timezone": "no-such-zone"},
            {"intent": "x" * 241},
        ]:
            before = self.bot._get_wakes().path.read_bytes()
            self.assertFalse((await self.rep("set", **fields))["success"])
            self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)

    async def test_repeat_ignores_default_hours_cooldown_and_isolation(self):
        await self.create(10)
        self.bot.config.scheduler.min_interval_between_chats = 0
        now = datetime.fromtimestamp(NOW + 600)
        self.bot.config.scheduler.start_time = self.bot.config.scheduler.end_time = (
            f"{(now.hour + 1) % 24:02}:00"
        )
        self.state.last_proactive_time = NOW + 599
        self.bot.config.scheduler.min_interval_between_chats = 60
        self.bot.config.scheduler.isolation_time = 5
        self.bot._get_last_msg_time = AsyncMock(return_value=NOW + 659)
        await self.due(600)
        self.assertEqual(self.plan()["last"]["status"], "submitted")
        self.bot._get_last_msg_time.assert_not_awaited()
        self.assertEqual(self.state.last_proactive_time, NOW + 599)
        # The safety gate remains independent from all default policy fields.
        data = self.bot._get_wakes().read()
        data["last_dispatch"] = NOW + 1199
        self.bot._get_wakes().write(data)
        await self.due(1200)
        self.assertEqual(self.plan()["current"]["block"], "dispatch_spacing")
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()

    async def test_capacity_and_corrupt_repeat_index_preserved(self):
        await self.create()
        data = self.bot._get_wakes().read()
        for field, bad in [
            ("enabled", "true"),
            ("kind", "cron"),
            ("interval", 0),
            ("attempted", -1),
            ("timezone", "../bad"),
            ("current", {}),
        ]:
            broken = copy.deepcopy(data)
            broken["repeats"]["friend"][field] = bad
            raw = json.dumps(broken).encode()
            self.bot._get_wakes().path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.bot._get_wakes().recover(NOW)
            self.assertEqual(self.bot._get_wakes().path.read_bytes(), raw)
        self.bot._get_wakes().path.write_bytes(json.dumps(data).encode())
        for n in range(127):
            data["repeats"][str(n)] = copy.deepcopy(data["repeats"]["friend"])
        self.bot._get_wakes().write(data)
        raw = self.bot._get_wakes().path.read_bytes()
        data["repeats"]["overflow"] = copy.deepcopy(data["repeats"]["friend"])
        with self.assertRaises(ValueError):
            self.bot._get_wakes().write(data)
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), raw)

    async def test_concurrent_due_and_model_changes_cannot_duplicate_occurrence(self):
        await self.create(2)
        self.clock_mock.return_value = NOW + 120
        await asyncio.gather(self.bot._run_wakes(), self.bot._run_wakes())
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        attempted = self.plan()["attempted"]
        await asyncio.gather(self.rep("pause"), self.rep("resume"))
        self.assertEqual(self.plan()["attempted"], attempted)

    async def test_tool_descriptions_are_in_primary_metadata_not_only_detailed(self):
        components = {c["name"]: c for c in self.bot.get_components()}
        for name in ("neko_proactive_wake", "neko_proactive_repeat"):
            metadata = components[name]["metadata"]
            self.assertIn("预约", metadata["description"])
            # SDK 2.22 normalizes the alias away; older supported SDKs retain it.
            self.assertEqual(
                metadata["description"], metadata.get("brief_description", metadata["description"])
            )
            self.assertIn("get", metadata["description"])
            self.assertNotIn("target", {p["name"] for p in metadata["parameters"]})
        self.assertIn("每隔N天", components["neko_proactive_repeat"]["metadata"]["description"])

    async def test_repeat_hot_config_scheduler_replacement_keeps_plan_unload_cancels_unknown(self):
        await self.create(2)

        async def loop():
            await asyncio.Event().wait()

        self.bot._schedule_loop = loop
        await self.bot.on_config_update()
        first = self.bot._scheduler_task
        raw = self.bot._get_wakes().path.read_bytes()
        await self.bot.on_config_update()
        self.assertTrue(first.done())
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), raw)
        await self.bot._stop_scheduler()
        entered = asyncio.Event()

        async def pending(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.bot.ctx.maisaka.proactive.trigger.side_effect = pending
        self.clock_mock.return_value = NOW + 120
        self.bot._scheduler_task = asyncio.create_task(self.bot._run_wakes())
        await entered.wait()
        await self.bot.on_unload()
        self.assertEqual(self.plan()["last"]["status"], "unknown")
        self.assertGreater(self.plan()["current"]["due"], NOW + 120)


class CalendarTests(unittest.TestCase):
    def test_dst_gap_skips_day_and_fold_uses_earlier_only(self):
        plan = {"kind": "daily", "timezone": "America/New_York", "clock": "02:30"}
        after = datetime(2026, 3, 7, 15, tzinfo=timezone.utc).timestamp()
        due = repeat.next_due(plan, after)
        self.assertEqual(datetime.fromtimestamp(due, repeat.zone_info(plan["timezone"])).day, 9)
        plan["clock"] = "01:30"
        before = datetime(2026, 11, 1, 4, tzinfo=timezone.utc).timestamp()
        first = repeat.next_due(plan, before)
        self.assertEqual(first, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc).timestamp())
        second = repeat.next_due(plan, first + 1)
        self.assertEqual(datetime.fromtimestamp(second, repeat.zone_info(plan["timezone"])).day, 2)
