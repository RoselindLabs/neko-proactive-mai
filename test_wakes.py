"""Real SDK declarations + synthetic Host, clocks and restart; no external I/O."""

import asyncio
import copy
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from wake_store import CONTRACT, MAX_BYTES, MAX_TARGETS, parse_appointment
from test_prompt_visibility import item, task

ROOT = Path(__file__).parent
pkg = types.ModuleType("wake_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg
mod = importlib.import_module("wake_test.plugin")
WakeStore = mod.WakeStore
NOW = 1793505600.0


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def host_stream(target, group, *, session=None):
    """Complete pinned Host identity; success fixtures never omit ownership fields."""
    sid = session or "session-" + target
    return {
        "session_id": sid,
        "stream_id": sid,
        "platform": "qq",
        "user_id": "" if group else target,
        "user_nickname": "" if group else "合成好友",
        "user_cardname": "",
        "group_id": target if group else "",
        "group_name": "合成群" + target if group else "",
        "account_id": "bot",
        "scope": "group" if group else "private",
        "is_group_session": group,
        "chat_type": "group" if group else "private",
    }


class WakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = mod.LonelyMaiConfig()
        config.plugin.enabled = True
        config.target.allowed_friends = ["friend", "other"]
        config.target.allowed_groups = ["group"]
        config.scheduler.start_time = "00:00"
        config.scheduler.end_time = "23:59"
        config.scheduler.isolation_time = 0
        config.scheduler.probability = 1
        ctx = types.SimpleNamespace(
            logger=Mock(),
            paths=types.SimpleNamespace(data_dir=self.directory.name),
            config=types.SimpleNamespace(get=AsyncMock(return_value="bot")),
            chat=types.SimpleNamespace(
                get_stream_by_user_id=AsyncMock(
                    side_effect=lambda user: {"stream": host_stream(user, False)}
                ),
                get_stream_by_group_id=AsyncMock(
                    side_effect=lambda group: {"stream": host_stream(group, True)}
                ),
            ),
            maisaka=types.SimpleNamespace(
                proactive=types.SimpleNamespace(
                    trigger=AsyncMock(
                        return_value={
                            "success": True,
                            "task_id": "proactive:neko.proactive:1793505660000",
                        }
                    )
                )
            ),
        )

        class Bot(mod.LonelyMaiPlugin):
            pass

        Bot.config, Bot.ctx = config, ctx
        self.bot = Bot()
        self.bot._wake_ready = True
        self.bot._bot_qq = "bot"
        self.state = mod.ChatStreamState("session-friend", "friend")
        self.bot._states = {
            (False, "friend"): self.state,
            (False, "other"): mod.ChatStreamState("session-other", "other"),
        }
        self.host = {
            "contract": CONTRACT,
            "session_id": "session-friend",
            "stream_id": "session-friend",
            "platform": "qq",
            "group_id": "",
            "user_id": "friend",
            "account_id": "bot",
        }
        self.clock = patch.object(mod.time, "time", return_value=NOW)
        self.clock_mock = self.clock.start()
        self.addCleanup(self.clock.stop)

    async def tool(self, action="get", **kwargs):
        if action in ("cancel", "update") and "item_id" not in kwargs:
            from wake_test.dayplan import item_version

            rows = mod.singles(self.bot._get_wakes().read(), "friend")
            pending = [r for r in rows if r["status"] in mod.PENDING]
            if len(pending) == 1:
                kwargs.update(item_id=pending[0]["id"], version=item_version(pending[0]))
        return await self.bot.private_wake(action, _host_context=self.host, **kwargs)

    async def reserve(self, delay=60, intent="续聊刚才未完的话题", **kwargs):
        result = await self.tool(
            "set", at=iso(NOW + delay), timezone="UTC", intent=intent, **kwargs
        )
        self.assertTrue(result["success"], result)
        return result["wake"]

    def rows(self):
        return self.bot._get_wakes().read()["slots"]

    async def due(self, delay=60):
        self.clock_mock.return_value = NOW + delay
        await self.bot._run_wakes()

    async def test_add_preserves_existing_and_cancel_is_item_scoped(self):
        first = await self.reserve()
        self.assertEqual((await self.tool())["wake"]["id"], first["id"])
        second = await self.reserve(120)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len((await self.tool())["items"]), 2)
        view = (await self.tool())["items"]
        for it in view:
            self.assertEqual(
                (await self.tool("cancel", item_id=it["item_id"], version=it["version"]))["wake"][
                    "status"
                ],
                "cancelled",
            )
        await self.due(180)
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_future_reservation_keeps_default_and_group_running_with_shared_spacing(
        self,
    ):
        await self.reserve(120)
        with patch.object(mod.random, "random", return_value=0):
            await self.bot._check_and_trigger(self.state, "friend")
            self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
            await self.bot._check_and_trigger(self.bot._states[(False, "other")], "other")
            group = mod.ChatStreamState("session-group", "group", is_group=True)
            await self.bot._check_and_trigger(group, "group")
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 2)

    async def test_cancel_restores_original_private_default(self):
        await self.reserve()
        await self.tool("cancel")
        await self.bot._check_and_trigger(self.state, "friend")
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()

    async def test_authority_comes_only_from_host_private_envelope(self):
        for changes in [
            {"group_id": "group"},
            {"platform": "discord"},
            {"contract": "fake"},
            {"session_id": "session-other"},
            {"stream_id": "session-other"},
            {"user_id": "other"},
            {"user_id": "stranger"},
            {"account_id": "wrong"},
        ]:
            with self.subTest(changes=changes):
                result = await self.bot.private_wake(
                    "set",
                    at=iso(NOW + 60),
                    timezone="UTC",
                    intent="依据",
                    _host_context={**self.host, **changes},
                )
                self.assertFalse(result["success"])
        self.assertFalse(
            (await self.bot.private_wake("get", session_id="session-friend", user_id="friend"))[
                "success"
            ]
        )
        self.bot.config.target.allowed_friends = []
        self.assertFalse((await self.tool())["success"])
        self.assertEqual(self.rows(), {})

    async def test_invalid_times_inputs_and_actions_do_not_change_state(self):
        await self.reserve()
        before = self.rows()
        for at, zone, intent in [
            (iso(NOW + 59), "UTC", "依据"),
            (iso(NOW + 7 * 86400 + 1), "UTC", "依据"),
            (iso(NOW - 60), "UTC", "依据"),
            ("2026-11-01T10:00:00", "UTC", "依据"),
            (iso(NOW + 60), "Not/AZone", "依据"),
            (iso(NOW + 60), "Asia/Shanghai", "依据"),
            (iso(NOW + 60), "UTC", "x" * 241),
            (iso(NOW + 60), "UTC", ""),
            ("x" * 65, "UTC", "依据"),
            (iso(NOW + 60), "UTC", "a\nb"),
        ]:
            self.assertFalse(
                (await self.tool("set", at=at, timezone=zone, intent=intent))["success"]
            )
            self.assertEqual(self.rows(), before)
        self.assertFalse((await self.tool("invalid"))["success"])
        self.assertEqual(
            parse_appointment(iso(NOW + 7 * 86400), "UTC", "依据", NOW)[0], NOW + 7 * 86400
        )
        offset = (
            datetime.fromtimestamp(NOW + 60, timezone.utc)
            .astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
            .isoformat()
        )
        self.assertEqual(parse_appointment(offset, "Asia/Shanghai", "依据", NOW)[0], NOW + 60)

    async def test_five_minute_plan_ignores_all_default_cycle_rules(self):
        await self.reserve(300)
        cfg = self.bot.config.scheduler
        cfg.probability = 0
        current = datetime.fromtimestamp(NOW + 300)
        cfg.start_time = cfg.end_time = f"{(current.hour + 1) % 24:02}:00"
        self.state.last_proactive_time = NOW
        cfg.min_interval_between_chats = 60
        cfg.isolation_time = 15
        self.bot._get_last_msg_time = AsyncMock(return_value=NOW + 299)
        await self.due(300)
        self.assertEqual(self.rows()["friend"]["status"], "submitted")
        self.assertEqual(self.rows()["friend"]["attempted"], NOW + 300)
        self.bot._get_last_msg_time.assert_not_awaited()
        self.assertEqual(self.state.last_proactive_time, NOW)

    async def test_blocked_task_expires_after_24h_without_calling_model(self):
        await self.reserve()
        await self.due(86460)
        self.assertEqual(self.rows()["friend"]["status"], "expired")
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_each_host_outcome_consumed_once_and_never_replayed_on_restart(self):
        for result, expected in [
            ({"success": True}, "submitted"),
            ({"success": False}, "rejected"),
            ({}, "unknown"),
            (None, "unknown"),
            ({"success": 1}, "unknown"),
        ]:
            with self.subTest(expected=expected, result=result):
                self.clock_mock.return_value = NOW
                self.bot._get_wakes().path.unlink(missing_ok=True)
                self.state.last_proactive_time = 0
                await self.reserve()
                self.bot.ctx.maisaka.proactive.trigger.reset_mock()
                self.bot.ctx.maisaka.proactive.trigger.return_value = result
                await self.due()
                self.assertEqual(self.rows()["friend"]["status"], expected)
                self.bot._wakes = WakeStore(self.directory.name)
                self.bot._get_wakes().recover(NOW + 61)
                await self.due(61)
                self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()

    async def test_unknown_is_durable_before_io_and_survives_exception_or_cancellation(self):
        for failure in [RuntimeError("synthetic"), asyncio.CancelledError()]:
            self.clock_mock.return_value = NOW
            self.bot._get_wakes().path.unlink(missing_ok=True)
            await self.reserve()

            async def trigger(**kwargs):
                self.assertEqual(
                    WakeStore(self.directory.name).read()["slots"]["friend"]["status"], "unknown"
                )
                raise failure

            self.bot.ctx.maisaka.proactive.trigger.side_effect = trigger
            try:
                await self.due()
            except asyncio.CancelledError:
                pass
            self.assertEqual(self.rows()["friend"]["status"], "unknown")
            self.bot._wakes = WakeStore(self.directory.name)
            self.bot._get_wakes().recover(NOW + 61)
            self.bot.ctx.maisaka.proactive.trigger.reset_mock()
            await self.due(61)
            self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_restart_keeps_future_but_expires_missed_without_catchup(self):
        await self.reserve(120)
        fresh = WakeStore(self.directory.name)
        fresh.recover(NOW + 60)
        self.assertEqual(fresh.read()["slots"]["friend"]["status"], "scheduled")
        fresh.recover(NOW + 121)
        self.bot._wakes = fresh
        await self.due(121)
        self.assertEqual(self.rows()["friend"]["block"], "restart_missed")
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_same_target_concurrency_and_fast_rearm_cannot_bypass_guard(self):
        await asyncio.gather(self.reserve(), self.reserve())
        self.clock_mock.return_value = NOW + 60
        await asyncio.gather(self.bot._run_wakes(), self.bot._run_wakes())
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        result = await self.tool("set", at=iso(NOW + 120), timezone="UTC", intent="重新考虑")
        self.assertTrue(result["success"])
        self.bot._wakes = WakeStore(self.directory.name)
        self.state.last_proactive_time = 0  # Full process reload lost the old memory cooldown.
        await self.due(119)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        await self.tool("cancel")
        await self.bot._check_and_trigger(self.state, "friend")
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()

    async def test_two_due_targets_are_spaced_not_batch_submitted(self):
        await self.reserve()
        self.host = {
            **self.host,
            "user_id": "other",
            "session_id": "session-other",
            "stream_id": "session-other",
        }
        await self.reserve()
        await self.due()
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        await self.due()  # Next pass observes the durable global safety boundary.
        self.assertEqual(self.rows()["other"]["block"], "dispatch_spacing")
        await self.due(119)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        await self.due(120)
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 2)

    async def test_latest_revocation_or_session_invalidation_prevents_dispatch(self):
        for mode in ["whitelist", "session", "disabled", "scheduler", "unloaded"]:
            self.clock_mock.return_value = NOW
            self.bot._get_wakes().path.unlink(missing_ok=True)
            self.bot.config.target.allowed_friends = ["friend"]
            self.bot.config.plugin.enabled = True
            self.bot.config.scheduler.enabled = True
            self.bot._wake_ready = True
            self.bot.ctx.chat.get_stream_by_user_id.side_effect = lambda target: {
                "stream": host_stream(target, False)
            }
            await self.reserve()
            if mode == "whitelist":
                self.bot.config.target.allowed_friends = []
            elif mode == "session":
                self.bot.ctx.chat.get_stream_by_user_id.side_effect = lambda target: {
                    "stream": host_stream(target, False, session="different")
                }
            elif mode == "disabled":
                self.bot.config.plugin.enabled = False
            elif mode == "scheduler":
                self.bot.config.scheduler.enabled = False
            else:
                self.bot._wake_ready = False
            await self.due()
            self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_revocation_during_host_session_query_and_tool_query_fails_closed(self):
        await self.reserve()

        async def revoke(_):
            self.bot.config.target.allowed_friends = []
            return {"stream": host_stream("friend", False)}

        self.bot.ctx.chat.get_stream_by_user_id.side_effect = revoke
        await self.due()
        self.assertEqual(self.rows()["friend"]["status"], "revoked")
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.bot.config.target.allowed_friends = ["friend"]
        self.assertFalse((await self.tool())["success"])

    async def test_context_is_once_task_and_chat_bound_ephemeral_and_not_delivery_evidence(self):
        row = await self.reserve(intent="来自真实前文的继续聊聊意图")
        await self.due()
        submitted = self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs
        self.assertNotIn(row["intent"], json.dumps(submitted, ensure_ascii=False))
        text = task(1793505660000).replace(
            '{"source": "lonelymai"}', json.dumps(submitted["metadata"])
        )
        request = {"session_id": "session-friend", "items": [item(text)]}
        self.assertEqual(
            await self.bot._wake_prompt({**request, "session_id": "session-other"}), []
        )
        self.assertEqual(await self.bot._wake_prompt({**request, "session_id": ""}), [])
        modified = (await self.bot.filter_planner_tasks(**request))["modified_kwargs"]
        self.assertEqual(len(modified["items"]), 1)
        self.assertIn(row["intent"], modified["items"][0]["parts"][0]["text"])
        self.assertEqual(await self.bot._wake_prompt(request), [])
        self.assertEqual(
            self.bot._filter_proactive_request(modified)["modified_kwargs"]["items"], []
        )
        self.assertEqual(self.rows()["friend"]["status"], "submitted")
        self.assertFalse(self.bot.config.life.enabled)
        self.assertFalse(self.bot.config.bilibili.enabled)

    async def test_wrong_task_expired_task_and_revoked_prompt_never_expose_intent(self):
        await self.reserve()
        await self.due()
        metadata = self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs["metadata"]
        text = task(1793505660000).replace('{"source": "lonelymai"}', json.dumps(metadata))
        source = {"session_id": "session-friend", "items": [item(text)]}
        wrong = {**source, "items": [item(text.replace("1793505660000", "1793505660001"))]}
        self.assertEqual(await self.bot._wake_prompt(wrong), [])
        self.bot.config.target.allowed_friends = []
        self.assertEqual(await self.bot._wake_prompt(source), [])
        self.bot.config.target.allowed_friends = ["friend"]
        self.clock_mock.return_value = NOW + 241
        self.assertEqual(await self.bot._wake_prompt(source), [])

    async def test_real_sdk_schema_contains_no_target_or_trust_argument(self):
        component = next(c for c in self.bot.get_components() if c["name"] == "neko_proactive_wake")
        self.assertEqual(component["metadata"]["trusted_context"], CONTRACT)
        self.assertEqual(component["metadata"]["chat_scope"], "private")
        self.assertEqual(component["metadata"]["platforms"], ["qq", "telegram"])
        self.assertEqual(
            {p["name"] for p in component["metadata"]["parameters"]},
            {"action", "at", "intent", "item_id", "version"},
        )

    async def test_config_reschedule_preserves_pending_and_cooldown_unload_cancels_inflight(self):
        await self.reserve()
        entered = asyncio.Event()

        async def loop():
            entered.set()
            await asyncio.Event().wait()

        self.bot._schedule_loop = loop
        self.state.last_proactive_time = NOW - 30
        await self.bot.on_config_update()
        first = self.bot._scheduler_task
        await entered.wait()
        await self.bot.on_config_update()
        self.assertTrue(first.done())
        self.assertEqual(self.rows()["friend"]["status"], "scheduled")
        self.assertEqual(self.state.last_proactive_time, NOW - 30)
        await self.bot.on_unload()
        self.assertIsNone(self.bot._scheduler_task)
        self.assertFalse((await self.tool())["success"])

    async def test_single_scheduler_event_does_not_advance_group_default_tick(self):
        await self.bot._init_target_streams()
        self.bot._check_and_trigger = AsyncMock()
        self.bot._run_wakes = AsyncMock()
        self.bot._next_sleep_seconds = lambda: 3600
        self.bot._wake_delay = lambda: 3600
        running = asyncio.create_task(self.bot._schedule_loop())
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            initial = self.bot._check_and_trigger.await_count
            self.assertEqual(initial, 3)
            await self.reserve()
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(self.bot._check_and_trigger.await_count, initial)
            self.assertGreaterEqual(self.bot._run_wakes.await_count, 2)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_real_sdk_appointment_uses_single_existing_capability(self):
        from maibot_sdk.capabilities.maisaka import MaisakaCapability

        context = types.SimpleNamespace(
            call_capability=AsyncMock(
                return_value={"success": True, "task_id": "proactive:neko.proactive:1793505660000"}
            )
        )
        self.bot.ctx.maisaka = MaisakaCapability(context)
        await self.reserve()
        await self.due()
        context.call_capability.assert_awaited_once()
        self.assertEqual(context.call_capability.call_args.args, ("maisaka.proactive.trigger",))
        self.assertEqual(self.rows()["friend"]["status"], "submitted")

    async def test_storage_failure_before_io_never_dispatches_and_after_io_never_replays(self):
        await self.reserve()
        self.clock_mock.return_value = NOW + 60
        store = self.bot._get_wakes()
        with patch.object(store, "write", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                await self.bot._run_wakes()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        original = store.write
        calls = 0

        def fail_second(data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("after acceptance, before final status write")
            original(data)

        with patch.object(store, "write", side_effect=fail_second):
            with self.assertRaises(OSError):
                await self.bot._run_wakes()
        self.assertEqual(self.rows()["friend"]["status"], "unknown")
        self.bot._wakes = WakeStore(self.directory.name)
        await self.due(61)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()

    async def test_default_rule_hot_change_does_not_block_explicit_plan(self):
        await self.reserve()
        self.state.last_proactive_time = NOW - 3600

        async def tighten(_):
            self.bot.config.scheduler.min_interval_between_chats = 120
            return {"stream": host_stream("friend", False)}

        self.bot.ctx.chat.get_stream_by_user_id.side_effect = tighten
        await self.due()
        self.assertEqual(self.rows()["friend"]["status"], "submitted")
        self.state.last_proactive_time = 0

        async def add_isolation(_):
            self.bot.config.scheduler.isolation_time = 15
            return {"stream": host_stream("friend", False)}

        self.bot.ctx.chat.get_stream_by_user_id.side_effect = add_isolation
        await self.due(120)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()

    async def test_unload_during_real_scheduler_dispatch_leaves_durable_unknown(self):
        await self.reserve()
        entered = asyncio.Event()

        async def pending(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.bot.ctx.maisaka.proactive.trigger.side_effect = pending
        self.clock_mock.return_value = NOW + 60
        self.bot._scheduler_task = asyncio.create_task(self.bot._run_wakes())
        await entered.wait()
        await self.bot.on_unload()
        self.assertEqual(self.rows()["friend"]["status"], "unknown")
        self.assertFalse(self.bot._wake_ready)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = WakeStore(self.directory.name)

    def test_damaged_oversized_unknown_or_symlink_index_preserved_not_replaced(self):
        for raw in [
            b"broken",
            b"null",
            b"[]",
            b'{"version":2}',
            b"x" * (MAX_BYTES + 1),
            b'{"version":1,"last_dispatch":0,"slots":{"x":null}}',
        ]:
            self.store.path.write_bytes(raw)
            with self.assertRaises((ValueError, UnicodeError)):
                self.store.write({"version": 1, "last_dispatch": 0, "slots": {}})
            self.assertEqual(self.store.path.read_bytes(), raw)
        self.store.path.unlink()
        target = Path(self.directory.name) / "real"
        target.write_bytes(b"do not touch")
        try:
            self.store.path.symlink_to(target)
        except OSError:
            # Windows without symlink privilege: test the branch without a skip.
            with patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaises(ValueError):
                    self.store.read()
        else:
            with self.assertRaises(ValueError):
                self.store.read()
            self.assertEqual(target.read_bytes(), b"do not touch")

    def test_capacity_input_and_file_size_bounded_and_replacement_keeps_guard(self):
        data = self.store.read()
        for n in range(MAX_TARGETS):
            self.store.set(data, str(n), "session", iso(NOW + 60), "UTC", "purpose", NOW)
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.set(data, "overflow", "session", iso(NOW + 60), "UTC", "purpose", NOW)
        self.assertEqual(self.store.path.read_bytes(), before)
        data["slots"]["0"]["attempted"] = NOW - 1
        self.store.write(data)
        self.store.set(
            data,
            "0",
            "session",
            iso(NOW + 120),
            "UTC",
            "updated",
            NOW,
            wake_id=data["slots"]["0"]["id"],
        )
        self.assertEqual(self.store.read()["slots"]["0"]["attempted"], NOW - 1)
        self.assertLess(self.store.path.stat().st_size, MAX_BYTES)
        self.assertEqual(list(Path(self.directory.name).glob(".private-wakes-*")), [])

    def test_all_record_fields_validated_and_corruption_blocks_recovery(self):
        data = self.store.read()
        self.store.set(data, "friend", "session", iso(NOW + 60), "UTC", "purpose", NOW)
        for name, value in [
            ("status", []),
            ("intent", "x" * 241),
            ("session", ""),
            ("due", float("nan")),
            ("retry", -1),
            ("shown", "yes"),
            ("id", "bad"),
            ("attempted", True),
        ]:
            broken = copy.deepcopy(data)
            broken["slots"]["friend"][name] = value
            raw = json.dumps(broken).encode()
            self.store.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.store.recover(NOW + 120)
            self.assertEqual(self.store.path.read_bytes(), raw)

    def test_dst_fold_needs_explicit_valid_offset(self):
        base = datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()
        first = parse_appointment("2026-11-01T01:30:00-04:00", "America/New_York", "purpose", base)
        second = parse_appointment("2026-11-01T01:30:00-05:00", "America/New_York", "purpose", base)
        self.assertEqual(second[0] - first[0], 3600)
        with self.assertRaises(ValueError):
            parse_appointment("2026-11-01T01:30:00-06:00", "America/New_York", "purpose", base)


if __name__ == "__main__":
    unittest.main()
