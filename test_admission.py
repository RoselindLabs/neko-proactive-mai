"""Real SDK admission regressions; synthetic Host, clock and temporary storage only."""

import copy
from datetime import datetime
from unittest.mock import patch
import unittest

import test_dayplan as dayplans
from test_wakes import NOW, iso, host_stream, mod


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    setUp = dayplans.DayplanTests.setUp
    asyncSetUp = dayplans.DayplanTests.asyncSetUp

    async def plans(self):
        single = await self.bot.private_wake(
            "add", at=iso(NOW + 3600), timezone="UTC", intent="single", _host_context=self.host
        )
        repeat = await self.bot.private_repeat(
            "set", interval_minutes=120, intent="repeat", _host_context=self.host
        )
        day = await self.bot.private_dayplan(
            "add", at=iso(NOW + 1800), timezone="UTC", intent="day", _host_context=self.host
        )
        for result in (single, repeat, day):
            self.assertTrue(result["success"], result)

    def statuses(self):
        data = self.bot._get_wakes().read()
        return (
            data["slots"]["friend"]["status"],
            data["repeats"]["friend"]["state"],
            next(iter(data["dayplans"]["friend"]["items"].values()))["status"],
        )

    async def test_transient_lookup_preserves_all_future_plans_and_cooldown_without_cached_send(
        self,
    ):
        await self.plans()
        store = self.bot._get_wakes()
        before = store.path.read_bytes()
        original = self.context._rpc_call
        state = self.bot._states[(False, "friend")]
        state.last_proactive_time = NOW - 30
        for response in (
            TimeoutError("synthetic"),
            {"success": False},
            {"success": True, "stream": None},
        ):
            with self.subTest(response=repr(response)):

                async def unavailable(method, plugin_id, payload, **kwargs):
                    if payload["capability"].startswith("chat.get_stream"):
                        if isinstance(response, Exception):
                            raise response
                        return response
                    return await original(method, plugin_id, payload, **kwargs)

                self.context._rpc_call = unavailable
                await self.bot._init_target_streams()
                self.assertEqual(self.bot._states[(False, "friend")].stream_id, "")
                await self.bot._run_wakes()
                self.assertFalse(await self.bot._do_proactive_chat(state, "friend"))
                self.assertEqual(store.path.read_bytes(), before)
                self.assertEqual(self.calls, [])
                self.context._rpc_call = original
                await self.bot._init_target_streams()
                await self.bot._run_wakes()
                self.assertEqual(self.statuses(), ("scheduled", "active", "scheduled"))
                self.assertEqual(self.bot._states[(False, "friend")].last_proactive_time, NOW - 30)

    async def test_due_lookup_failure_blocks_without_attempt_then_recovers_once(self):
        result = await self.bot.private_wake(
            "add", at=iso(NOW + 60), timezone="UTC", intent="single", _host_context=self.host
        )
        self.assertTrue(result["success"])
        # Fail the final fresh lookup, while an earlier resolved state still exists.
        self.responses[(False, "friend")] = {"success": True, "stream": None}
        self.clock_mock.return_value = NOW + 60
        await self.bot._run_wakes()
        data = self.bot._get_wakes().read()
        self.assertEqual(data["slots"]["friend"]["block"], "session_unavailable")
        self.assertEqual(data["slots"]["friend"]["attempted"], 0)
        self.assertEqual(data["last_dispatch"], 0)
        self.assertEqual(self.calls, [])
        self.responses[(False, "friend")] = {
            "success": True,
            "stream": host_stream("friend", False),
        }
        self.clock_mock.return_value = NOW + 120
        await self.bot._init_target_streams()
        await self.bot._run_wakes()
        await self.bot._run_wakes()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.bot._get_wakes().read()["slots"]["friend"]["status"], "submitted")

    async def test_explicit_revocation_and_confirmed_session_change_stay_terminal(self):
        await self.plans()
        original = self.bot._get_wakes().path.read_bytes()
        for mode in ("whitelist", "session"):
            with self.subTest(mode=mode):
                # Reset only this test's synthetic fixture, never runtime data.
                self.bot._get_wakes().path.write_bytes(original)
                if mode == "whitelist":
                    self.bot.config.target.allowed_friends = []
                else:
                    self.responses[(False, "friend")]["stream"] = host_stream(
                        "friend", False, session="new-valid-session"
                    )
                await self.bot._init_target_streams()
                await self.bot._run_wakes()
                self.assertEqual(self.statuses(), ("revoked", "revoked", "revoked"))
                self.bot.config.target.allowed_friends = ["friend"]
                self.responses[(False, "friend")]["stream"] = host_stream("friend", False)
                await self.bot._init_target_streams()
                await self.bot._run_wakes()
                self.assertEqual(self.statuses(), ("revoked", "revoked", "revoked"))
                self.assertEqual(self.calls, [])

    async def test_strict_identity_is_shared_by_schedule_manual_tools_and_management(self):
        await self.plans()
        before = self.bot._get_wakes().path.read_bytes()
        host = {**self.host, "account_id": ""}  # Real Host may lack an anchor account.
        for group, target in ((False, "friend"), (True, "g1")):
            state = mod.ChatStreamState("session-" + target, target, is_group=group)
            original = host_stream(target, group)
            cases = [
                {key: value for key, value in original.items() if key != "account_id"},
                {**original, "account_id": "other-bot"},
                {**original, "platform": "other-platform"},
                {**original, "is_group_session": not group},
                {**original, "group_id" if group else "user_id": "other-target"},
                {"session_id": original["session_id"]},
            ]
            for stream in cases:
                with self.subTest(group=group, stream=stream):
                    self.responses[(group, target)] = {"success": True, "stream": stream}
                    await self.bot._init_target_streams()
                    self.assertFalse(
                        self.bot._states.get((group, target))
                        and self.bot._states[(group, target)].stream_id
                    )
                    self.assertIsNone(await self.bot._management_stream(target, group))
                    self.assertFalse(await self.bot._do_proactive_chat(state, target))
                    if not group:
                        for tool in (
                            self.bot.private_wake,
                            self.bot.private_repeat,
                            self.bot.private_dayplan,
                        ):
                            self.assertFalse((await tool("get", _host_context=host))["success"])
                        await self.bot._run_wakes()
                        self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)
                    self.assertEqual(self.calls, [])
            self.responses[(group, target)] = {"success": True, "stream": original}
        # Positive side: missing anchor account is safe only with strict fresh ownership.
        self.assertTrue((await self.bot.private_wake("get", _host_context=host))["success"])

    async def test_latest_human_query_prevents_false_idle_and_filters_bot_before_limit(self):
        original_rpc = self.context._rpc_call
        stamps = [(NOW - 1200, "human"), (NOW - 10, "human")]
        stamps.extend((NOW - offset, "bot") for offset in range(9, 3, -1))
        queries = []

        async def rpc(method, plugin_id, payload, **kwargs):
            if payload["capability"] == "message.get_by_time_in_chat":
                args = copy.deepcopy(payload["args"])
                queries.append(args)
                rows = [
                    (stamp, user)
                    for stamp, user in stamps
                    if not args.get("filter_mai") or user != "bot"
                ]
                rows = rows[-args["limit"] :]  # Host latest still returns chronological order.
                return {
                    "success": True,
                    "messages": [
                        {"timestamp": str(stamp), "message_info": {"user_info": {"user_id": user}}}
                        for stamp, user in rows
                    ],
                }
            return await original_rpc(method, plugin_id, payload, **kwargs)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(NOW, tz)

        self.context._rpc_call = rpc
        self.bot.config.scheduler.isolation_time = 15
        state = self.bot._states[(True, "g1")]
        with patch.object(mod, "datetime", Clock):
            await self.bot._check_default_and_trigger(state, "g1")
            self.assertEqual(self.calls, [])
            self.assertEqual(self.bot._observations[(True, "g1")]["last_message"], NOW - 10)
            self.assertTrue(queries[-1]["filter_mai"])
            self.assertEqual(queries[-1]["limit"], 1)
            stamps[:] = [(NOW - 1200, "human"), (NOW - 1, "bot")]
            await self.bot._check_default_and_trigger(state, "g1")
        self.assertEqual(len(self.calls), 1)
