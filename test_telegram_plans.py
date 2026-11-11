"""Real SDK → synthetic Host RPC → shared store/scheduler, without external IO."""

import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import unittest

from maibot_sdk import PluginContext
from maibot_sdk.context import PluginPaths

import test_wakes as fixtures
from test_wakes import NOW, host_stream


class TelegramPlanTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.WakeTests.setUp
    KEY = "telegram:901:123"

    async def asyncSetUp(self):
        self.bot.config.target.allowed_groups = []
        self.bot.config.target.allowed_friends = ["123"]
        self.bot.config.target.telegram_account_id = "901"
        self.bot.config.target.allowed_telegram_user_ids = ["123"]
        self.qq = host_stream("123", False, session="qq-123")
        self.tg = {
            **self.qq,
            "session_id": "tg-123",
            "stream_id": "tg-123",
            "platform": "telegram",
            "account_id": "901",
        }
        self.streams = [
            {**self.tg, "account_id": "902", "session_id": "other-bot", "stream_id": "other-bot"},
            self.tg,
        ]
        self.calls, self.sent = [], []
        self.outcome = {"success": True, "task_id": "proactive:neko.proactive:offline"}
        self.after_lookup = None

        async def rpc(method, plugin_id, payload, **kwargs):
            self.assertEqual((method, plugin_id), ("cap.call", "neko.proactive"))
            cap, args = payload["capability"], payload["args"]
            self.calls.append((cap, copy.deepcopy(args)))
            if cap == "chat.get_private_streams":
                self.assertEqual(args, {"platform": "telegram"})
                result = {"success": True, "streams": copy.deepcopy(self.streams)}
            elif cap == "chat.get_stream_by_user_id":
                self.assertEqual(args["platform"], "qq")
                result = {"success": True, "stream": copy.deepcopy(self.qq)}
            elif cap == "maisaka.proactive.trigger":
                self.sent.append(copy.deepcopy(args))
                return self.outcome
            else:
                raise AssertionError("Unexpected capability: " + cap)
            if self.after_lookup:
                self.after_lookup()
            return result

        self.rpc = rpc
        self.bot.ctx = PluginContext(
            "neko.proactive",
            rpc_call=rpc,
            paths=PluginPaths(
                data_dir=Path(self.directory.name),
                runtime_dir=Path(self.directory.name) / "runtime",
            ),
        )
        self.bot.ctx._logger = Mock()
        self.bot._scheduler_task = Mock(done=Mock(return_value=False))
        await self.bot._init_target_streams()

    def envelope(self, platform="telegram", **changes):
        session = "tg-123" if platform == "telegram" else "qq-123"
        return {
            "contract": fixtures.CONTRACT,
            "session_id": session,
            "stream_id": session,
            "platform": platform,
            "account_id": "",
            "user_id": "123",
            "group_id": "",
            **changes,
        }

    async def add(self, host_platform="telegram", **fields):
        return await self.bot.private_wake(
            "add",
            at="10分钟后",
            intent="继续已知话题",
            _host_context=self.envelope(host_platform),
            **fields,
        )

    def data(self):
        return self.bot._get_wakes().read()

    async def view(self, key=None):
        return next(
            r
            for r in (await self.bot.wake_overview())["reservations"]
            if r["target"] == (key or self.KEY)
        )

    @staticmethod
    def identity(row):
        return {key: row[key] for key in ("target", "session", "revision")}

    async def test_default_deny_empty_account_resolves_exact_session_and_same_number_isolation(
        self,
    ):
        self.bot.config.target.allowed_telegram_user_ids = []
        self.assertFalse((await self.add())["success"])
        self.assertFalse(self.bot._get_wakes().path.exists())
        self.assertTrue((await self.add("qq"))["success"])
        self.assertEqual(self.data()["version"], 4)
        qq = copy.deepcopy(self.data())
        self.bot.config.target.allowed_telegram_user_ids = ["123"]
        self.assertTrue(
            (
                await self.add(
                    stream_id="forged",
                    session_id="qq-123",
                    account_id="902",
                    user_id="999",
                    platform="fake",
                )
            )["success"]
        )
        data = self.data()
        self.assertEqual(data["version"], 5)
        self.assertEqual(data["slots"]["123"], qq["slots"]["123"])
        self.assertEqual(data["last_dispatch"], qq["last_dispatch"])
        self.assertEqual(data["slots"][self.KEY]["session"], "tg-123")
        self.assertEqual((await self.view())["account_id"], "901")
        # Different bot account gets a different plan, never inherits 901's plan.
        self.bot.config.target.telegram_account_id = "902"
        result = await self.bot.private_repeat(
            "get", _host_context=self.envelope(session_id="other-bot", stream_id="other-bot")
        )
        self.assertTrue(result["success"])
        self.assertIsNone(result["repeat"])
        self.assertFalse((await self.add())["success"])

    async def test_rejects_wrong_session_account_platform_group_ambiguity_and_missing_identity(
        self,
    ):
        for changes in (
            {"account_id": "902"},
            {"session_id": "other-bot", "stream_id": "other-bot"},
            {"group_id": "-1001"},
            {"user_id": "@name"},
            {"stream_id": "qq-123"},
            {"platform": "other"},
        ):
            with self.subTest(changes=changes):
                result = await self.bot.private_repeat(
                    "set", interval_minutes=5, intent="依据", _host_context=self.envelope(**changes)
                )
                self.assertFalse(result["success"])
        original = copy.deepcopy(self.streams)
        for field, value in (
            ("account_id", "902"),
            ("account_id", ""),
            ("platform", "qq"),
            ("is_group_session", True),
            ("is_group_session", None),
            ("group_id", "-1001"),
            ("user_id", "456"),
            ("stream_id", "wrong"),
        ):
            self.streams = [{**self.tg, field: value}]
            self.assertFalse((await self.add())["success"], (field, value))
        self.streams = [self.tg, copy.deepcopy(self.tg)]
        self.assertFalse((await self.add())["success"])
        self.streams = [self.tg] * 513
        self.assertFalse((await self.add())["success"])
        self.streams = original
        self.assertTrue((await self.add())["success"])

    async def test_plan_dispatch_unknown_no_replay_and_no_tg_base_dayplan_or_manual(self):
        self.assertTrue((await self.add())["success"])
        state = self.bot._states[(False, self.KEY)]
        self.bot.config.life.enabled = self.bot.config.bilibili.enabled = True
        await self.bot._check_and_trigger(state, self.KEY)
        await self.bot._check_default_and_trigger(state, self.KEY)
        self.assertFalse(await self.bot._do_proactive_chat(state, self.KEY))
        self.assertFalse((await self.bot.cmd_trigger(stream_id="tg-123"))[0])
        self.assertFalse(
            (
                await self.bot.private_dayplan(
                    "add", at="21:00", intent="依据", _host_context=self.envelope()
                )
            )["success"]
        )
        row = await self.view()
        self.assertTrue(row["plans_only"])
        self.assertIsNone(row["dayplan"])
        self.assertFalse(
            (
                await self.bot.wake_edit(
                    "day_add", **self.identity(row), when="21:00", intent="依据"
                )
            )["success"]
        )
        self.assertFalse(self.sent)
        self.clock_mock.return_value = NOW + 600
        self.outcome = None
        await self.bot._run_wakes()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["stream_id"], "tg-123")
        self.assertEqual(self.sent[0]["metadata"]["target_id"], "123")
        self.assertEqual(self.sent[0]["metadata"]["account_id"], "901")
        before = self.data()
        self.assertEqual(before["slots"][self.KEY]["status"], "unknown")
        self.bot._get_wakes().recover(NOW + 1000)
        await self.bot._run_wakes()
        self.assertEqual(self.data(), before)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(any(cap == "api.call" for cap, _ in self.calls))

    async def test_repeat_restart_keeps_future_anchor_unknown_and_skips_missed_occurrence(self):
        self.assertTrue(
            (
                await self.bot.private_repeat(
                    "set", interval_minutes=5, intent="依据", _host_context=self.envelope()
                )
            )["success"]
        )
        initial = self.data()["repeats"][self.KEY]
        self.bot._get_wakes().recover(NOW + 120)
        self.assertEqual(self.data()["repeats"][self.KEY], initial)
        self.clock_mock.return_value = NOW + 300
        self.outcome = None
        await self.bot._run_wakes()
        plan = self.data()["repeats"][self.KEY]
        self.assertEqual(plan["last"]["status"], "unknown")
        self.assertEqual(plan["current"]["due"], NOW + 600)
        self.bot._get_wakes().recover(NOW + 601)
        self.clock_mock.return_value = NOW + 601
        await self.bot._run_wakes()
        after = self.data()["repeats"][self.KEY]
        self.assertEqual(after["anchor"], initial["anchor"])
        self.assertEqual(after["last"], plan["last"])
        self.assertEqual(after["current"]["due"], NOW + 900)
        self.assertEqual(len(self.sent), 1)

    async def test_revoke_during_lookup_cas_old_account_history_and_never_restore_revoked(self):
        stale = await self.view()
        self.assertTrue((await self.add())["success"])
        self.assertFalse(
            (
                await self.bot.wake_edit(
                    "single_save", **self.identity(stale), when="2026-11-01T12:00", intent="stale"
                )
            )["success"]
        )
        current = await self.view()
        self.after_lookup = lambda: setattr(self.bot.config.target, "allowed_telegram_user_ids", [])
        self.assertFalse((await self.add())["success"])
        self.after_lookup = None
        await self.bot._run_wakes()
        self.assertEqual(self.data()["slots"][self.KEY]["status"], "revoked")
        self.bot.config.target.allowed_telegram_user_ids = ["123"]
        await self.bot._init_target_streams()
        await self.bot._run_wakes()
        self.assertEqual(self.data()["slots"][self.KEY]["status"], "revoked")
        self.bot.config.target.telegram_account_id = "902"
        snapshot = await self.bot.wake_overview()
        self.assertNotIn("继续已知话题", str(snapshot))
        history = snapshot["cleanup"][0]
        self.assertEqual((history["platform"], history["account_id"]), ("telegram", "901"))
        self.assertFalse(
            (
                await self.bot.wake_edit(
                    "single_save", **self.identity(current), when="2026-11-01T12:00", intent="stale"
                )
            )["success"]
        )
        self.assertTrue(
            (
                await self.bot.wake_edit(
                    "single_delete",
                    **self.identity(history),
                    item_id=history["item_id"],
                    version=history["version"],
                )
            )["success"]
        )
        self.assertEqual(self.data()["slots"][self.KEY]["status"], "revoked")
        self.assertFalse(self.sent)

    async def test_atomic_v5_transition_preserves_v4_on_failure_and_does_not_downgrade(self):
        self.assertTrue((await self.add("qq"))["success"])
        self.clock_mock.return_value = NOW + 600
        self.outcome = None
        await self.bot._run_wakes()
        self.assertEqual(self.data()["slots"]["123"]["status"], "unknown")
        self.assertTrue(
            (
                await self.bot.private_repeat(
                    "set", interval_minutes=5, intent="QQ原节奏", _host_context=self.envelope("qq")
                )
            )["success"]
        )
        self.assertTrue((await self.add("qq"))["success"])
        store = self.bot._get_wakes()
        original = store.path.read_bytes()
        with patch(
            "wake_test.wake_store.os.replace", side_effect=OSError("synthetic replace failure")
        ):
            self.assertFalse((await self.add())["success"])
        self.assertEqual(store.path.read_bytes(), original)
        self.assertEqual(list(store.directory.glob(".private-wakes-*")), [])
        self.assertTrue((await self.add())["success"])
        v5 = self.data()
        qq_only = copy.deepcopy(v5)
        qq_only["slots"].pop(self.KEY)
        qq_only["version"] = 4
        self.assertEqual(qq_only, json.loads(original))
        v5["version"] = 4
        store.write(v5)
        self.assertEqual(self.data()["version"], 5)
        # Ambiguous old namespace is corruption, not permission to reinterpret.
        v5["version"] = 4
        store.path.write_text(json.dumps(v5), encoding="utf-8")
        damaged = store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "ambiguous_legacy_target"):
            store.recover(NOW)
        self.assertFalse((await self.add())["success"])
        self.assertEqual(store.path.read_bytes(), damaged)

    async def test_actual_shared_loop_only_runs_qq_base_without_any_tg_plan(self):
        with patch.object(
            self.bot._wake_event, "wait", new=AsyncMock(side_effect=asyncio.CancelledError)
        ):
            await self.bot._schedule_loop()
        self.assertEqual([args["stream_id"] for args in self.sent], ["qq-123"])
        self.assertEqual(self.data()["version"], 4)
        self.assertEqual(self.data()["slots"], {})

    async def test_session_lookup_outage_preserves_future_and_blocks_due_then_rechecks(self):
        await self.add()
        initial = self.data()
        self.streams = []
        await self.bot._init_target_streams()
        await self.bot._run_wakes()
        self.assertEqual(self.data(), initial)
        self.clock_mock.return_value = NOW + 600
        await self.bot._run_wakes()
        self.assertEqual(self.data()["slots"][self.KEY]["block"], "session_unavailable")
        self.assertFalse(self.sent)
        self.streams = [self.tg]
        await self.bot._init_target_streams()
        self.clock_mock.return_value = NOW + 660
        await self.bot._run_wakes()
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
