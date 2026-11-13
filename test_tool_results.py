"""Exercise plans using only model-visible results and real SDK chat normalization."""

import json
import unittest

import test_chat_sdk as chats
from test_wakes import NOW


class VisibleResultTests(unittest.IsolatedAsyncioTestCase):
    setUp = chats.ChatSDKTests.setUp
    asyncSetUp = chats.ChatSDKTests.asyncSetUp
    # The opt-in Host proof replaces only this reader with the actual Host parser.
    content = staticmethod(lambda result: result["content"])

    async def call(self, kind, action="get", **args):
        result = await getattr(self.bot, "private_" + kind)(action, _host_context=self.host, **args)
        text = self.content(result)
        self.assertTrue(text)
        self.assertLessEqual(len(text), 14000)
        if result["success"]:
            self.assertNotEqual(text, result["message"])
            view = json.loads(text.split("\n")[1])
            for internal in (
                '"session"',
                '"task"',
                '"retry"',
                '"attempted"',
                '"batch_id"',
                '"guard"',
            ):
                self.assertNotIn(internal, text)
            return result, view
        return result, text

    async def test_single_and_day_cas_from_visible_content(self):
        for kind, at in (("wake", "10分钟后"), ("dayplan", "23:00")):
            with self.subTest(kind=kind):
                _, empty = await self.call(kind)
                self.assertEqual(empty["items"], [])
                result, added = await self.call(kind, "add", at=at, intent="原话题依据")
                self.assertNotIn("unknown", self.content(result))
                self.assertNotIn("submitted=", self.content(result))
                self.assertNotIn("先get", self.content(result))
                selected = added["selected"]
                self.assertEqual(selected["intent"], "原话题依据")
                self.assertTrue(selected["can_update"])
                self.assertEqual(added["next_at"], selected["at"])
                identity = {k: selected[k] for k in ("item_id", "version")}
                _, changed = await self.call(kind, "update", **identity, intent="新进展")
                updated = changed["selected"]
                self.assertEqual(updated["item_id"], selected["item_id"])
                self.assertEqual(updated["at"], selected["at"])
                self.assertNotEqual(updated["version"], selected["version"])
                stale, error = await self.call(kind, "cancel", **identity)
                self.assertFalse(stale["success"])
                self.assertIn("重新get", error)
                self.assertIn("不要改为add", error)
                _, listed = await self.call(kind)
                identity = {k: listed["items"][0][k] for k in ("item_id", "version")}
                result, cancelled = await self.call(kind, "cancel", **identity)
                self.assertEqual(cancelled["selected"]["status"], "cancelled")
                self.assertIsNone(cancelled["next_at"])
                self.assertIn("已取消", result["message"])
                self.assertNotIn("到点再判断", result["message"])

    async def test_repeat_controls_and_history_are_not_delivery(self):
        _, empty = await self.call("repeat")
        self.assertIsNone(empty["repeat"])
        _, created = await self.call("repeat", "set", interval_minutes=120, intent="定期关注")
        self.assertEqual(created["repeat"]["interval_minutes"], 120)
        self.assertTrue(created["repeat"]["item_id"])
        _, paused = await self.call("repeat", "pause")
        self.assertEqual(paused["repeat"]["state"], "paused")
        self.assertIsNone(paused["next_at"])
        _, edited = await self.call("repeat", "set", intent="只改话题")
        self.assertEqual(edited["repeat"]["state"], "paused")
        self.assertEqual(edited["repeat"]["planned_at"], created["next_at"])
        _, resumed = await self.call("repeat", "resume")
        self.assertEqual(resumed["repeat"]["state"], "active")
        self.assertTrue(resumed["next_at"])
        data = self.bot._get_wakes().read()
        plan = data["repeats"]["friend"]
        plan["last"] = {
            **plan["current"],
            "status": "unknown",
            "attempted": NOW,
            "task": "secret-task",
        }
        self.bot._get_wakes().write(data)
        result, history = await self.call("repeat")
        self.assertEqual(history["repeat"]["last"]["status"], "unknown")
        self.assertIn("不能认定未发送或为补发重加", self.content(result))
        _, deleted = await self.call("repeat", "delete")
        self.assertEqual(deleted["repeat"]["state"], "deleted")
        self.assertEqual(deleted["repeat"]["last"]["status"], "unknown")
        self.assertIsNone(deleted["next_at"])
        _, absent = await self.call("repeat")
        self.assertIsNone(absent["repeat"])

    async def test_errors_are_actionable_without_hidden_timezone_parameters(self):
        for kind, args, expected in (
            ("wake", dict(action="add", at="10分钟后"), "intent"),
            ("dayplan", dict(action="add", at="23:00"), "intent"),
            ("repeat", dict(action="set", interval_minutes=5), "intent"),
            ("wake", dict(action="add", at="01:00", intent="过时"), "北京时间"),
            ("dayplan", dict(action="add", at="明天09:00", intent="过界"), "今天"),
            ("wake", dict(action="update", intent="缺ID"), "item_id"),
            ("dayplan", dict(action="cancel"), "item_id"),
            (
                "wake",
                dict(action="add", at="10分钟后", intent="依据", item_id="old"),
                "新增add不填",
            ),
            (
                "dayplan",
                dict(action="add", at="23:00", intent="依据", version="old"),
                "新增add不填",
            ),
            (
                "repeat",
                dict(action="set", interval_minutes=5, local_time="21:00", intent="冲突"),
                "二选一",
            ),
        ):
            with self.subTest(kind=kind, args=args):
                result, text = await self.call(kind, **args)
                self.assertFalse(result["success"])
                self.assertIn(expected, text)
                self.assertNotIn("IANA", text)
                self.assertNotIn("带偏移", text)
                self.assertNotIn("unknown", text)

    async def test_many_items_keep_mutation_target_and_report_truncation(self):
        for minute in range(1, 27):
            _, added = await self.call("wake", "add", at=f"{minute}分钟后", intent="依据" * 120)
        selected = added["selected"]
        self.assertTrue(added["truncated"])
        self.assertEqual(added["total"], 26)
        self.assertLess(added["shown"], added["total"])
        self.assertIn("没有翻页参数", added["continue"])
        self.assertIn("管理页", added["continue"])
        identity = {k: selected[k] for k in ("item_id", "version")}
        _, updated = await self.call("wake", "update", **identity, intent="列表外目标")
        self.assertEqual(updated["selected"]["item_id"], selected["item_id"])
        self.assertEqual(updated["selected"]["intent"], "列表外目标")
        data = self.bot._get_wakes().read()
        data["slots"]["friend"].update(status="submitted", task="secret-task", attempted=NOW)
        self.bot._get_wakes().write(data)
        result, listed = await self.call("wake")
        self.assertEqual(listed["items"][0]["status"], "submitted")
        self.assertIn("仅Host已入队，不是已回复/送达", self.content(result))
        self.assertFalse(listed["items"][0]["can_update"])


if __name__ == "__main__":
    unittest.main()
