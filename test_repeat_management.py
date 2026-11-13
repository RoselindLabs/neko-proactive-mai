"""New admin actions over real PluginContext / raw Host RPC test fixture."""

from datetime import datetime, timezone
from pathlib import Path
import unittest

import test_chat_sdk as fixtures
from test_wakes import NOW, iso


class RepeatManagementTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ChatSDKTests.setUp
    asyncSetUp = fixtures.ChatSDKTests.asyncSetUp
    identity = fixtures.ChatSDKTests.identity
    selected = fixtures.ChatSDKTests.selected

    def test_management_copy_explains_pause_recovery_without_changing_schedule(self):
        document = Path(__file__).with_name("webui.html").read_text(encoding="utf-8")
        for wording in (
            "单次预约不会暂停基础唤醒",
            "重复计划与基础周期并行",
            "只有临近计划时短暂避让",
            "日程只能安排今天",
            "当前唤醒方式",
        ):
            self.assertIn(wording, document)
        self.assertNotIn("由预约接管", document)

    async def edit(self, action, row=None, **fields):
        row = await self.selected() if row is None else row
        return await self.bot.wake_edit(action, **self.identity(row), **fields)

    async def test_private_repeat_lifecycle_cas_and_current_priority_over_real_sdk(self):
        await self.bot._init_target_streams()
        self.assertTrue(
            (
                await self.edit(
                    "repeat_set",
                    kind="daily",
                    local_time="21:00",
                    timezone="Asia/Shanghai",
                    intent="真实合成依据",
                )
            )["success"]
        )
        row = await self.selected()
        self.assertEqual(row["repeat"]["local_time"], "21:00")
        overview = await self.bot.wake_overview()
        self.assertEqual(
            next(r for r in overview["base"] if r["kind"] == "私聊")["state"],
            "等待基础周期检查",
        )
        self.assertTrue((await self.edit("repeat_pause", row))["success"])
        self.assertFalse((await self.edit("repeat_resume", row))["success"])
        self.assertTrue((await self.edit("repeat_resume"))["success"])
        self.assertTrue(
            (await self.edit("single_save", at=iso(NOW + 120), timezone="UTC", intent="单次优先"))[
                "success"
            ]
        )
        self.assertEqual(
            next(r for r in (await self.bot.wake_overview())["base"] if r["kind"] == "私聊")[
                "state"
            ],
            "等待基础周期检查",
        )
        before = self.bot._get_wakes().path.read_bytes()
        await self.bot.wake_overview()
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)
        self.assertTrue((await self.edit("repeat_delete"))["success"])
        self.assertEqual((await self.selected())["repeat"], None)
        self.assertTrue(all(cap.startswith("chat.get_stream_by_") for cap, _ in self.rpc_calls))

    async def test_custom_time_input_and_bad_local_dst_are_readable_errors(self):
        future = datetime.fromtimestamp(NOW + 120, timezone.utc).strftime("%Y-%m-%dT%H:%M")
        result = await self.edit("single_save", when=future, timezone="UTC", intent="合成依据")
        self.assertTrue(result["success"], result)
        result = await self.edit(
            "single_save", when="2026-03-08T02:30", timezone="America/New_York", intent="合成依据"
        )
        self.assertFalse(result["success"])
        self.assertIn("不存在", result["message"])

    async def test_disabled_pause_delete_allowed_but_resume_and_set_denied(self):
        self.assertTrue(
            (
                await self.edit(
                    "repeat_set",
                    kind="interval",
                    interval_minutes=120,
                    timezone="UTC",
                    intent="依据",
                )
            )["success"]
        )
        self.bot.config.scheduler.enabled = False
        self.assertTrue((await self.edit("repeat_pause"))["success"])
        self.assertFalse((await self.edit("repeat_resume"))["success"])
        self.assertFalse((await self.edit("repeat_set", intent="新的依据"))["success"])
        self.assertTrue((await self.edit("repeat_delete"))["success"])

    async def test_revoked_or_changed_session_read_redacts_old_repeat_purpose(self):
        await self.edit(
            "repeat_set",
            kind="interval",
            interval_minutes=120,
            timezone="UTC",
            intent="旧会话私有意图",
        )
        before = self.bot._get_wakes().path.read_bytes()
        self.responses[(False, "friend")]["stream"]["session_id"] = "new-session"
        self.responses[(False, "friend")]["stream"]["stream_id"] = "new-session"
        snapshot = await self.bot.wake_overview()
        self.assertIsNone(snapshot["reservations"][0]["repeat"])
        self.assertNotIn("旧会话私有意图", str(snapshot))
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)

    async def test_reauthorized_revoked_plan_rejects_pause_resume_without_corrupting_index(self):
        await self.bot._init_target_streams()
        self.assertTrue(
            (
                await self.edit(
                    "repeat_set",
                    interval_minutes=120,
                    kind="interval",
                    timezone="UTC",
                    intent="依据",
                )
            )["success"]
        )
        self.bot.config.target.allowed_friends = []
        await self.bot._run_wakes()
        store = self.bot._get_wakes()
        revoked = store.read()["repeats"]["friend"]
        self.assertEqual(revoked["state"], "revoked")
        self.assertEqual(revoked["current"]["status"], "revoked")
        self.bot.config.target.allowed_friends = ["friend"]
        raw = store.path.read_bytes()
        self.bot._wake_event.clear()
        for action in ("pause", "resume"):
            with self.subTest(action=action):
                result = await self.edit("repeat_" + action)
                self.assertFalse(result["success"])
                self.assertIn("已撤权失效", result["message"])
                result = await self.bot.private_repeat(action, _host_context=self.host)
                self.assertEqual(result["error"], "repeat_revoked")
                self.assertEqual(store.path.read_bytes(), raw)
                self.assertFalse(self.bot._wake_event.is_set())
                self.assertEqual(store.read()["repeats"]["friend"], revoked)
                store.recover(NOW + 86400)
                self.assertEqual(store.path.read_bytes(), raw)
                self.assertTrue(
                    (await self.bot.private_repeat("get", _host_context=self.host))["success"]
                )
