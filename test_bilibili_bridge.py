import json
import asyncio
import threading
import importlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from maibot_sdk.context import PluginContext, PluginPaths
import test_intent as intents
from prompt_visibility import pending_offers, filter_request, inject_bilibili_offers, EPHEMERAL
import test_prompt_visibility as visibility
import test_wakes as wakes


class BilibiliIntentTests(unittest.IsolatedAsyncioTestCase):
    setUp = intents.LifeIntentTests.setUp

    async def test_bilibili_uses_existing_trigger_without_source_body_in_history(self):
        self.bot.config.bilibili.enabled = True
        bridge = SimpleNamespace(
            flush=AsyncMock(),
            claim=AsyncMock(
                return_value={
                    "receipt_id": "a" * 32,
                    "protocol": 2,
                    "summary": "source must not enter history",
                }
            ),
            update=AsyncMock(),
        )
        self.bot._bilibili = bridge
        self.bot.ctx.maisaka.proactive.trigger.return_value = {
            "success": True,
            "task_id": "proactive:neko.proactive:1792930000000",
        }
        self.assertTrue(await self.bot._do_proactive_chat(self.state, "target"))
        args = self.bot.ctx.maisaka.proactive.trigger.await_args.kwargs
        self.assertIn("neko_bilibili_offer", args["reason"])
        self.assertNotIn("source must not enter history", str(args))
        self.assertEqual(args["intent"], self.bot.config.intent.private_text)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        bridge.update.assert_awaited_once_with(
            "a" * 32, "submitted", "proactive:neko.proactive:1792930000000"
        )

    async def test_source_failure_falls_back_to_normal_chat_and_revoked_target_never_triggers(self):
        self.bot.config.bilibili.enabled = True
        self.bot._bilibili = SimpleNamespace(flush=AsyncMock(side_effect=ValueError("offline")))
        self.assertTrue(await self.bot._do_proactive_chat(self.state, "target"))
        args = self.bot.ctx.maisaka.proactive.trigger.await_args.kwargs
        self.assertNotIn("neko_bilibili_offer", args["reason"])
        self.bot.config.target.allowed_friends = []
        self.bot.ctx.maisaka.proactive.trigger.reset_mock()
        self.assertFalse(await self.bot._do_proactive_chat(self.state, "target"))
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    def test_focus_bilibili_material_is_scoped_request_only_and_removed_next_request(self):
        stamp = 1792930000000
        text = visibility.task(stamp).replace(
            "attention_proactive",
            'attention_proactive\n<neko_bilibili_offer receipt_id="' + "a" * 32 + '"/>',
        )
        source = {"session_id": "chat", "items": [visibility.item(text)]}
        found = pending_offers(source, now=stamp / 1000 + 1, source="bilibili")
        self.assertEqual([(r["scope"], r["receipt"]) for r in found], [("chat", "a" * 32)])
        self.assertEqual(
            pending_offers({**source, "session_id": ""}, now=stamp / 1000 + 1, source="bilibili"),
            [],
        )
        clean, _ = filter_request(source)
        self.assertEqual(clean["items"], [])
        injected = inject_bilibili_offers(
            [],
            [
                (
                    "chat",
                    "a" * 32,
                    {
                        "summary": "actual source",
                        "url": "https://www.bilibili.com/video/BV1234567890",
                    },
                )
            ],
        )
        self.assertTrue(injected[0]["meta"]["item_id"].startswith(EPHEMERAL))
        self.assertNotIn("neko.life.share", str(injected))
        self.assertEqual(filter_request({"session_id": "chat", "items": injected})[0]["items"], [])

    def test_journal_thought_and_share_reason_reach_only_bounded_optional_context(self):
        candidate = {
            "journal": True,
            "topic": "深海信号",
            "opinion": "这是当时的感想" * 90,
            "share_reason": "如果这个聊天谈到海洋，可以接着聊光的用途",
            "summary": "摘要" * 400,
        }
        injected = inject_bilibili_offers([], [("private-session", "b" * 32, candidate)])
        text = injected[0]["parts"][0]["text"]
        self.assertLessEqual(len(text), 1600)
        self.assertIn("reading_thought", text)
        self.assertIn("possible_conversation", text)
        self.assertIn("private-session", text)
        self.assertIn("没有合适关联就不用", text)
        self.assertEqual(
            filter_request({"session_id": "private-session", "items": injected})[0]["items"], []
        )

    async def test_real_sdk_client_signs_for_own_provider_not_life(self):
        from importlib import import_module

        Bridge = import_module(self.mod.__package__ + ".bilibili_bridge").BilibiliBridge
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "bilibili-client.json"
            key.write_text(json.dumps({"neko.proactive": "ab" * 32}), encoding="utf-8")
            key.chmod(0o600)
            calls = []

            async def rpc(method, plugin_id, payload):
                calls.append(payload)
                return {"success": True, "result": {"success": True, "candidate": None}}

            ctx = PluginContext(
                "neko.proactive",
                rpc_call=rpc,
                paths=PluginPaths(Path(directory), Path(directory) / "runtime"),
            )
            bridge = Bridge(ctx)
            self.assertIsNone(await bridge.claim("chat"))
            args = calls[0]["args"]
            self.assertEqual(args["api_name"], "local.bilibili-autopilot.discovery_v1")
            self.assertEqual(args["args"]["packet"]["audience"], "local.bilibili-autopilot")
        self.assertEqual(bridge.path.name, "bilibili-outbox.json")


class BilibiliIOTests(unittest.IsolatedAsyncioTestCase):
    setUp = wakes.WakeTests.setUp

    async def test_sdk_unavailable_stops_polling_and_expires_without_reclaiming_unknown(self):
        module = importlib.import_module("wake_test.bilibili_bridge")
        clock = importlib.import_module("wake_test.life_bridge").time
        directory = Path(self.directory.name)
        key = directory / "bilibili-client.json"
        key.write_text(json.dumps({"neko.proactive": "ab" * 32}), encoding="utf-8")
        key.chmod(0o600)
        calls = []
        candidate_id = "a" * 32
        unknown_status = "unknown"

        async def rpc(method, plugin_id, payload):
            self.assertEqual(payload["args"]["api_name"], "local.bilibili-autopilot.discovery_v1")
            packet = payload["args"]["args"]["packet"]
            operation, body = packet["operation"], packet["body"]
            calls.append((operation, body))
            result = {"success": True}
            if operation == "claim":
                result["candidate"] = {
                    "receipt_id": candidate_id,
                    "summary": "synthetic",
                    "protocol": 2,
                }
            elif operation == "status":
                result["status"] = (
                    "unavailable" if body["receipt_id"] == "a" * 32 else unknown_status
                )
            else:
                self.assertEqual(operation, "receipt")
            return {"success": True, "result": result}

        ctx = PluginContext(
            "neko.proactive",
            rpc_call=rpc,
            paths=PluginPaths(directory, directory / "runtime"),
        )
        bridge = module.BilibiliBridge(ctx)
        with patch.object(clock, "time", return_value=wakes.NOW) as now:
            await bridge.claim("chat")
            await bridge.update(candidate_id, "submitted", "proactive:neko.proactive:1793505600000")
            candidate_id = "b" * 32
            await bridge.claim("chat")
            await bridge.update(candidate_id, "unknown")
            await bridge.flush()
            terminal = bridge._read()["a" * 32]
            uncertain = bridge._read()["b" * 32]
            self.assertEqual(terminal["status"], "unavailable")
            self.assertEqual(uncertain["status"], "unknown")

            bridge = module.BilibiliBridge(ctx)
            calls.clear()
            unknown_status = "future_unrecognized_status"
            await bridge.flush()
            self.assertEqual(calls, [("status", {"session_id": "chat", "receipt_id": "b" * 32})])
            self.assertEqual(bridge._read()["a" * 32], terminal)
            self.assertEqual(bridge._read()["b" * 32], uncertain)
            await bridge.update("a" * 32, "unknown")
            self.assertEqual(bridge._read()["a" * 32], terminal)

            now.return_value = wakes.NOW + 7 * 86400 - 1
            candidate_id = "a" * 32
            before = bridge.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "duplicate_receipt"):
                await bridge.claim("chat")
            self.assertEqual(bridge.path.read_bytes(), before)

            now.return_value += 1
            candidate_id = "c" * 32
            await bridge.claim("chat")
            self.assertEqual(set(bridge._read()), {"b" * 32, "c" * 32})
            self.assertEqual(bridge._read()["b" * 32], uncertain)

    def bridge(self):
        bridge = importlib.import_module("wake_test.bilibili_bridge").BilibiliBridge(self.bot.ctx)
        bridge.call = AsyncMock(
            side_effect=[
                {
                    "success": True,
                    "candidate": {"receipt_id": char * 32, "summary": "synthetic", "protocol": 2},
                }
                for char in ("a", "b")
            ]
        )
        return bridge

    def gated_write(self, bridge, *, error=False):
        entered, completed = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        original = bridge._write
        loop = asyncio.get_running_loop()

        def write(rows):
            loop.call_soon_threadsafe(entered.set)
            try:
                if not release.wait(10):
                    raise TimeoutError("synthetic write gate was not released")
                if error:
                    raise OSError("synthetic worker failure")
                return original(rows)
            finally:
                loop.call_soon_threadsafe(completed.set)

        bridge._write = write
        return entered, release, completed

    async def test_repeated_cancel_keeps_lock_until_thread_finishes_and_next_claim_keeps_both_rows(
        self,
    ):
        bridge = self.bridge()
        entered, release, completed = self.gated_write(bridge)
        first = asyncio.create_task(bridge.claim("group"))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(bridge.claim("group"))
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertTrue(bridge.lock.locked())
            self.assertEqual(bridge.call.await_count, 1)
            self.assertFalse(second.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await second
            self.assertTrue(completed.is_set())
            self.assertEqual(set(bridge._read()), {"a" * 32, "b" * 32})
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (first, second) if task), return_exceptions=True
            )

    async def test_unload_drains_scheduler_and_non_scheduler_writes_even_if_cancelled_and_worker_fails(
        self,
    ):
        for owned_by_scheduler, error in (
            (True, False),
            (False, False),
            (True, True),
            (False, True),
        ):
            with self.subTest(scheduler=owned_by_scheduler, worker_error=error):
                with tempfile.TemporaryDirectory() as directory:
                    self.bot.ctx.paths.data_dir = directory
                    self.bot._wake_ready = True
                    bridge = self.bot._bilibili = self.bridge()
                    entered, release, completed = self.gated_write(bridge, error=error)
                    first = asyncio.create_task(bridge.claim("group"))
                    unloading = None
                    self.bot._scheduler_task = first if owned_by_scheduler else None
                    try:
                        await asyncio.wait_for(entered.wait(), 5)
                        unloading = asyncio.create_task(self.bot.on_unload())
                        await asyncio.sleep(0)
                        # Cancel both the owner and the waiting unloader repeatedly.
                        first.cancel()
                        await asyncio.sleep(0)
                        first.cancel()
                        unloading.cancel()
                        await asyncio.sleep(0)
                        unloading.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(unloading.done())
                        self.assertTrue(bridge.lock.locked())
                        self.assertFalse(completed.is_set())
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await unloading
                        with self.assertRaises(asyncio.CancelledError):
                            await first
                        self.assertTrue(completed.is_set())
                        self.assertTrue(bridge.closed)
                        with self.assertRaisesRegex(ValueError, "bridge_closed"):
                            await bridge.claim("group")
                        new = self.bridge()
                        new.call.side_effect = None
                        new.call.return_value = {
                            "success": True,
                            "candidate": {
                                "receipt_id": "b" * 32,
                                "summary": "synthetic",
                                "protocol": 2,
                            },
                        }
                        await new.claim("group")
                        self.assertEqual(
                            set(new._read()), {"b" * 32} if error else {"a" * 32, "b" * 32}
                        )
                    finally:
                        release.set()
                        await asyncio.gather(
                            *(task for task in (first, unloading) if task), return_exceptions=True
                        )

    async def test_worker_failure_without_cancellation_is_not_swallowed(self):
        bridge = self.bridge()
        with patch.object(bridge, "_write", side_effect=OSError("synthetic")):
            with self.assertRaisesRegex(OSError, "synthetic"):
                await bridge.claim("group")
        self.assertFalse(bridge.lock.locked())
        self.assertFalse(bridge.path.exists())

    async def test_same_instance_reactivation_reopens_drained_bridge_from_ledger(self):
        bridge = self.bot._bilibili = self.bridge()
        await bridge.claim("group")
        before = bridge.path.read_bytes()
        await self.bot.on_unload()
        self.assertTrue(bridge.closed)
        self.bot.config.scheduler.enabled = False
        await self.bot.on_load()
        self.assertIsNone(self.bot._bilibili)
        self.assertTrue(self.bot._wake_ready)
        fresh = self.bot._bilibili = self.bridge()
        self.assertFalse(fresh.closed)
        self.assertEqual(fresh.path.read_bytes(), before)
        await self.bot.on_unload()
