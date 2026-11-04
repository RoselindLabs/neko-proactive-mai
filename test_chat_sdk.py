"""Real PluginContext normalization, with only the bottom Host RPC mocked."""

import copy
from pathlib import Path
import unittest
from unittest.mock import Mock

from maibot_sdk import PluginContext
from maibot_sdk.context import PluginPaths

import test_wakes as fixtures
from test_wakes import NOW, iso, host_stream


class ChatSDKTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.WakeTests.setUp
    rows = fixtures.WakeTests.rows

    async def asyncSetUp(self):
        self.responses = {
            (True, "g1"): {"success": True, "stream": host_stream("g1", True)},
            (True, "g2"): {"success": True, "stream": host_stream("g2", True)},
            (False, "friend"): {"success": True, "stream": host_stream("friend", False)},
        }
        self.rpc_calls = []

        async def rpc_call(method, plugin_id, payload, **kwargs):
            self.assertEqual(method, "cap.call")
            self.assertEqual(plugin_id, "neko.proactive")
            capability, args = payload["capability"], payload["args"]
            self.rpc_calls.append((capability, copy.deepcopy(args)))
            self.assertIn(capability, {"chat.get_stream_by_group_id", "chat.get_stream_by_user_id"})
            self.assertEqual(args["platform"], "qq")
            group = capability == "chat.get_stream_by_group_id"
            target = args["group_id" if group else "user_id"]
            # This is the actual Host capability's raw business envelope, as
            # supplied by Runner RPC. No ChatCapability/normalizer is mocked.
            return copy.deepcopy(
                self.responses.get((group, target), {"success": True, "stream": None})
            )

        paths = PluginPaths(
            data_dir=Path(self.directory.name), runtime_dir=Path(self.directory.name) / "runtime"
        )
        self.context = PluginContext("neko.proactive", rpc_call=rpc_call, paths=paths)
        self.context._logger = Mock()
        self.bot.ctx = self.context
        self.bot.config.target.allowed_groups = ["g1", "g2"]
        self.bot.config.target.allowed_friends = ["friend"]
        self.bot._scheduler_task = Mock(done=Mock(return_value=False))
        self.bot._scheduler_phase = "waiting"
        self.bot._next_regular_at = self.bot._next_pass_at = NOW + 1800
        self.bot._states = {}

    async def selected(self):
        return (await self.bot.wake_overview())["reservations"][0]

    def identity(self, row):
        return {key: row[key] for key in ("target", "session", "revision")}

    async def test_real_context_unwraps_raw_host_success_stream_and_not_found_none(self):
        raw = self.responses[(False, "friend")]
        result = await self.context.chat.get_stream_by_user_id("friend")
        self.assertEqual(result, raw["stream"])
        self.assertNotIn("stream", result)
        self.assertEqual(
            await self.context.chat.get_stream_by_group_id("g1"),
            self.responses[(True, "g1")]["stream"],
        )
        self.assertIsNone(await self.context.chat.get_stream_by_user_id("not-found"))

    async def test_snapshot_matches_real_scheduler_two_groups_one_private_without_side_effects(
        self,
    ):
        await self.bot._init_target_streams()
        self.assertEqual(len(self.bot._states), 3)
        before = copy.deepcopy({key: value.__dict__ for key, value in self.bot._states.items()})
        event = self.bot._wake_event.is_set()
        snapshot = await self.bot.wake_overview()
        self.assertEqual(len(snapshot["base"]), 3)
        self.assertEqual(len(snapshot["reservations"]), 1)
        self.assertEqual({r["kind"] for r in snapshot["base"]}, {"群聊", "私聊"})
        self.assertTrue(all(row["state"] != "真实会话未就绪" for row in snapshot["base"]))
        self.assertTrue(any("合成群g1" in row["chat"] for row in snapshot["base"]))
        self.assertIn("合成好友", snapshot["reservations"][0]["chat"])
        self.assertFalse(self.bot._get_wakes().path.exists())
        self.assertEqual(before, {key: value.__dict__ for key, value in self.bot._states.items()})
        self.assertEqual(event, self.bot._wake_event.is_set())
        self.assertTrue(
            all(capability.startswith("chat.get_stream_by_") for capability, _ in self.rpc_calls)
        )

    async def test_save_modify_cancel_and_read_existing_index_through_real_context(self):
        await self.bot._init_target_streams()
        selected = await self.selected()
        await self.bot.wake_save(
            **self.identity(selected), at=iso(NOW + 120), timezone="UTC", intent="合成前文依据"
        )
        row = await self.selected()
        self.assertEqual(row["status"], "待唤醒")
        self.assertTrue(self.bot._wake_event.is_set())
        self.assertEqual(self.bot._next_regular_at, NOW + 1800)
        it = row["singles"][0]
        await self.bot.wake_save(
            **self.identity(row), item_id=it["item_id"], version=it["version"], intent="更新依据"
        )
        self.assertEqual(self.rows()["friend"]["intent"], "更新依据")
        before = self.bot._get_wakes().path.read_bytes()
        self.bot._wake_event.clear()
        row = await self.selected()
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)
        self.assertFalse(self.bot._wake_event.is_set())
        it = row["singles"][0]
        await self.bot.wake_cancel(
            **self.identity(row), item_id=it["item_id"], version=it["version"]
        )
        self.assertEqual(self.rows()["friend"]["status"], "cancelled")
        self.assertTrue(
            all(capability.startswith("chat.get_stream_by_") for capability, _ in self.rpc_calls)
        )

    async def test_missing_wrong_platform_account_type_target_or_session_fail_closed(self):
        original = copy.deepcopy(self.responses)
        for group, target in [(True, "g1"), (False, "friend")]:
            identity_key = "group_id" if group else "user_id"
            for field, bad_value in [
                ("platform", None),
                ("platform", "other"),
                ("account_id", None),
                ("account_id", "wrong"),
                ("is_group_session", None),
                ("is_group_session", not group),
                (identity_key, None),
                (identity_key, "wrong"),
                ("session_id", None),
            ]:
                with self.subTest(group=group, field=field, value=bad_value):
                    self.responses = copy.deepcopy(original)
                    if bad_value is None:
                        self.responses[(group, target)]["stream"].pop(field)
                    else:
                        self.responses[(group, target)]["stream"][field] = bad_value
                    self.assertIsNone(await self.bot._management_stream(target, group))
        self.responses = original
        self.bot._bot_qq = ""
        self.assertIsNone(await self.bot._management_stream("friend", False))

    async def test_not_found_or_failed_response_never_invents_management_target(self):
        for result in [
            {"success": True, "stream": None},
            {"success": False, "error": "not found"},
            None,
            [],
        ]:
            self.responses = {key: copy.deepcopy(result) for key in self.responses}
            snapshot = await self.bot.wake_overview()
            self.assertEqual(len(snapshot["base"]), 3)
            self.assertEqual(snapshot["reservations"], [])
            self.assertTrue(all(row["state"] == "真实会话未就绪" for row in snapshot["base"]))
            self.assertFalse(self.bot._get_wakes().path.exists())

    async def test_real_context_write_rechecks_account_session_and_whitelist_and_keeps_cas(self):
        await self.bot._init_target_streams()
        row = await self.selected()
        original = copy.deepcopy(self.responses)
        for field, value in [
            ("account_id", "wrong"),
            ("session_id", "different-private"),
            ("is_group_session", True),
        ]:
            self.responses = copy.deepcopy(original)
            self.responses[(False, "friend")]["stream"][field] = value
            with self.assertRaises(ValueError):
                await self.bot.wake_save(
                    **self.identity(row), at=iso(NOW + 120), timezone="UTC", intent="依据"
                )
        self.responses = original
        self.bot.config.target.allowed_friends = []
        with self.assertRaises(ValueError):
            await self.bot.wake_save(
                **self.identity(row), at=iso(NOW + 120), timezone="UTC", intent="依据"
            )
        self.bot.config.target.allowed_friends = ["friend"]
        await self.bot.wake_save(
            **self.identity(row), at=iso(NOW + 120), timezone="UTC", intent="依据"
        )
        with self.assertRaises(ValueError):
            await self.bot.wake_cancel(**self.identity(row))
        self.assertEqual(self.rows()["friend"]["status"], "scheduled")


if __name__ == "__main__":
    unittest.main()
