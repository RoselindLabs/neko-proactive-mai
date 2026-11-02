"""Today intentions through real SDK; only raw Host RPC is synthetic."""

import asyncio
import copy
from datetime import datetime, timezone
import importlib
import json
import unittest
from unittest.mock import patch, AsyncMock

import test_chat_sdk as fixtures
from test_wakes import NOW, iso
from test_prompt_visibility import item, task, filter_request

day = importlib.import_module("wake_test.dayplan")


class DayplanTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ChatSDKTests.setUp
    selected = fixtures.ChatSDKTests.selected
    identity = fixtures.ChatSDKTests.identity

    async def asyncSetUp(self):
        await fixtures.ChatSDKTests.asyncSetUp(self)
        await self.bot._init_target_streams()
        self.original_rpc = self.context._rpc_call
        self.calls = []
        self.result = {"success": True, "task_id": "proactive:neko.proactive:1793505900000"}

        async def rpc(method, plugin_id, payload, **kwargs):
            if payload["capability"] == "maisaka.proactive.trigger":
                self.calls.append(copy.deepcopy(payload["args"]))
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result
            return await self.original_rpc(method, plugin_id, payload, **kwargs)

        self.context._rpc_call = rpc
        self.host = {
            "contract": "neko.tools.context.v1",
            "platform": "qq",
            "user_id": "friend",
            "session_id": "session-friend",
            "stream_id": "session-friend",
            "account_id": "bot",
            "group_id": "",
        }

    async def tool(self, action="get", **fields):
        return await self.bot.private_dayplan(action, _host_context=self.host, **fields)

    async def add(self, delay=300, intent="整理已知兴趣的合成想法"):
        response = await self.tool(
            "add", at=iso(self.clock_mock.return_value + delay), timezone="UTC", intent=intent
        )
        self.assertTrue(response["success"], response)
        return response["dayplan"]["items"][-1]

    def data(self):
        return self.bot._get_wakes().read()

    def plan(self):
        return self.data()["dayplans"]["friend"]

    async def test_multiple_items_never_replace_single_repeat_and_reads_do_not_write(self):
        r = await self.selected()
        await self.bot.wake_edit(
            "single_save", **self.identity(r), at=iso(NOW + 1200), timezone="UTC", intent="明确单次"
        )
        r = await self.selected()
        await self.bot.wake_edit(
            "repeat_set",
            **self.identity(r),
            kind="interval",
            interval_minutes=120,
            timezone="UTC",
            intent="重复节奏",
        )
        before = self.data()
        first = await self.add(300)
        second = await self.add(600, "续一个未完话题")
        after = self.data()
        self.assertNotEqual(first["item_id"], second["item_id"])
        self.assertEqual(after["slots"], before["slots"])
        self.assertEqual(after["repeats"], before["repeats"])
        raw = self.bot._get_wakes().path.read_bytes()
        event = self.bot._wake_event.is_set()
        await self.bot.wake_overview()
        await self.tool()
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), raw)
        self.assertEqual(self.bot._wake_event.is_set(), event)
        self.assertEqual(self.calls, [])
        self.assertTrue(all(cap.startswith("chat.get_stream_by_") for cap, _ in self.rpc_calls))

    async def test_delete_queued_batch_item_keeps_prompt_batch_guards_and_used_quota(self):
        await self.add(300, "批次首项")
        await self.add(300, "批次第二项")
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        before = self.data()
        selected = await self.selected()
        first = selected["dayplan"]["items"][0]
        self.assertTrue(first["can_delete"])
        result = await self.bot.wake_edit(
            "day_delete",
            **self.identity(selected),
            item_id=first["item_id"],
            version=first["version"],
        )
        self.assertTrue(result["success"], result)
        expected = copy.deepcopy(before)
        expected["dayplans"]["friend"]["items"][first["item_id"]]["dismissed"] = True
        self.assertEqual(self.data(), expected)
        fresh = (await self.selected())["dayplan"]
        self.assertEqual(len(fresh["items"]), 1)
        self.assertEqual(fresh["remaining"], 4)
        self.bot._get_wakes().recover(NOW + 301)
        raw_task = task(1793505900000).replace(
            '{"source": "lonelymai"}', json.dumps(self.calls[0]["metadata"])
        )
        prompt = await self.bot._wake_prompt(
            {"session_id": "session-friend", "items": [item(raw_task)]}
        )
        self.assertIn("批次首项", json.dumps(prompt, ensure_ascii=False))
        self.assertIn("批次第二项", json.dumps(prompt, ensure_ascii=False))
        self.assertEqual(len(self.calls), 1)

    async def test_delete_revoked_record_without_reauthorizing_or_revealing_old_intent(self):
        first = await self.add(intent="撤权后不可展示的私有意图")
        self.bot.config.target.allowed_friends = []
        await self.bot._run_wakes()
        snapshot = await self.bot.wake_overview()
        self.assertNotIn("撤权后不可展示", json.dumps(snapshot, ensure_ascii=False))
        old = snapshot["cleanup"][0]
        self.assertEqual(old["status"], "revoked")
        self.bot.ctx.chat.get_stream_by_user_id = AsyncMock(side_effect=AssertionError("no lookup"))
        result = await self.bot.wake_edit(
            "day_delete", **self.identity(old), item_id=old["item_id"], version=old["version"]
        )
        self.assertTrue(result["success"], result)
        self.assertEqual((await self.bot.wake_overview())["cleanup"], [])
        self.assertTrue(self.plan()["items"][first["item_id"]]["dismissed"])
        self.assertEqual(self.bot.config.target.allowed_friends, [])
        self.assertEqual(self.calls, [])

    async def test_delete_rejects_pending_stale_cross_session_and_inflight_then_allows_expired(
        self,
    ):
        await self.add()
        selected = await self.selected()
        row = selected["dayplan"]["items"][0]
        fields = {**self.identity(selected), "item_id": row["item_id"], "version": row["version"]}
        raw = self.bot._get_wakes().path.read_bytes()
        for changes in ({}, {"version": "bad"}, {"session": "other"}, {"target": "other"}):
            self.assertFalse(
                (await self.bot.wake_edit("day_delete", **{**fields, **changes}))["success"]
            )
            self.assertEqual(self.bot._get_wakes().path.read_bytes(), raw)
        self.clock_mock.return_value = NOW + 86400
        selected = await self.selected()
        row = selected["dayplan"]["items"][0]
        self.assertEqual(row["status"], "expired")
        self.assertTrue(row["can_delete"])
        fields = {**self.identity(selected), "item_id": row["item_id"], "version": row["version"]}
        self.bot._wake_inflight = "friend"
        self.assertFalse((await self.bot.wake_edit("day_delete", **fields))["success"])
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), raw)
        self.bot._wake_inflight = ""
        self.bot.config.scheduler.enabled = False
        self.assertTrue((await self.bot.wake_edit("day_delete", **fields))["success"])
        self.assertEqual((await self.selected())["dayplan"]["items"], [])
        self.assertFalse((await self.bot.wake_edit("day_delete", **fields))["success"])

    async def test_delete_unknown_keeps_uncertainty_and_no_model_delete_action(self):
        await self.add()
        self.result = None
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        selected = await self.selected()
        row = selected["dayplan"]["items"][0]
        before = self.data()
        result = await self.bot.wake_edit(
            "day_delete", **self.identity(selected), item_id=row["item_id"], version=row["version"]
        )
        self.assertTrue(result["success"], result)
        after = self.data()
        self.assertEqual(after["last_dispatch"], before["last_dispatch"])
        self.assertEqual(self.plan()["items"][row["item_id"]]["status"], "unknown")
        self.assertFalse(
            (await self.tool("delete", item_id=row["item_id"], version=row["version"]))["success"]
        )
        self.bot._get_wakes().recover(NOW + 360)
        await self.bot._run_wakes()
        self.assertEqual(len(self.calls), 1)

    async def test_future_only_update_cancel_cas_and_used_slots_cannot_be_refilled(self):
        first = await self.add()
        response = await self.tool(
            "update", item_id=first["item_id"], version=first["version"], intent="新的已知依据"
        )
        self.assertTrue(response["success"], response)
        self.assertFalse(
            (await self.tool("cancel", item_id=first["item_id"], version=first["version"]))[
                "success"
            ]
        )
        fresh = (await self.tool())["dayplan"]["items"][0]
        self.assertTrue(
            (await self.tool("cancel", item_id=fresh["item_id"], version=fresh["version"]))[
                "success"
            ]
        )
        for n in range(5):
            await self.add(600 + n * 60)
        before = self.bot._get_wakes().path.read_bytes()
        self.assertFalse(
            (await self.tool("add", at=iso(NOW + 1200), timezone="UTC", intent="超额"))["success"]
        )
        self.assertEqual(before, self.bot._get_wakes().path.read_bytes())
        self.assertEqual((await self.tool())["dayplan"]["remaining"], 0)

    async def test_today_offset_zone_limits_and_clock_reverse_rejected_without_rewrite(self):
        await self.add()
        raw = self.bot._get_wakes().path.read_bytes()
        for fields in [
            dict(at=iso(NOW + 59)),
            dict(at=iso(NOW + 86400)),
            dict(at="2026-11-01T20:00:00"),
            dict(timezone="Asia/Shanghai"),
            dict(timezone="bad"),
            dict(intent="x" * 241),
            dict(intent="a\nb"),
        ]:
            args = {"at": iso(NOW + 600), "timezone": "UTC", "intent": "依据", **fields}
            self.assertFalse((await self.tool("add", **args))["success"], args)
            self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())
        self.clock_mock.return_value = NOW - 86400
        self.assertFalse(
            (await self.tool("add", at=iso(NOW - 86000), timezone="UTC", intent="回到过去"))[
                "success"
            ]
        )
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())

    async def test_cross_chat_group_missing_account_revocation_and_session_do_not_grant(self):
        first = await self.add()
        for changes in [
            {"group_id": "g1"},
            {"user_id": "other"},
            {"session_id": "other"},
            {"account_id": "wrong"},
            {"contract": "fake"},
        ]:
            response = await self.bot.private_dayplan(
                "add",
                _host_context={**self.host, **changes},
                at=iso(NOW + 600),
                timezone="UTC",
                intent="不能借身份",
            )
            self.assertFalse(response["success"])
        self.responses[(False, "friend")]["stream"]["account_id"] = "wrong"
        # Legacy trusted tool path resolves a stream, so verify strict Host private
        # identity in the new tool as well, not only Host-injected account claims.
        self.assertFalse(
            (await self.tool("cancel", item_id=first["item_id"], version=first["version"]))[
                "success"
            ]
        )
        self.responses[(False, "friend")]["stream"]["account_id"] = "bot"
        self.bot.config.target.allowed_friends = []
        self.assertFalse((await self.tool())["success"])
        snapshot = await self.bot.wake_overview()
        self.assertNotIn("整理已知兴趣", json.dumps(snapshot, ensure_ascii=False))

    async def test_single_priority_due_merge_and_near_repeat_skip_not_permanent_suppression(self):
        r = await self.selected()
        await self.bot.wake_edit(
            "repeat_set",
            **self.identity(r),
            kind="interval",
            interval_minutes=5,
            timezone="UTC",
            intent="原重复",
        )
        await self.add(330, "事项一")
        await self.add(350, "事项二")
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.data()["repeats"]["friend"]["last"]["block"], "dayplan_priority")
        self.clock_mock.return_value = NOW + 350
        await self.bot._run_wakes()
        self.assertEqual(len(self.calls), 1)
        rows = list(self.plan()["items"].values())
        self.assertTrue(all(r["status"] == "submitted" for r in rows))
        self.assertEqual(len({r["task"] for r in rows}), 1)
        self.assertEqual(len({r["batch_id"] for r in rows}), 1)
        self.clock_mock.return_value = NOW + 600
        await self.bot._run_wakes()
        self.assertEqual(len(self.calls), 2)

    async def test_far_future_day_does_not_take_over_default_or_repeat(self):
        await self.add(7200)
        self.bot._check_default_and_trigger = AsyncMock()
        await self.bot._check_and_trigger(self.bot._states[(False, "friend")], "friend")
        self.bot._check_default_and_trigger.assert_awaited_once()
        self.clock_mock.return_value = NOW + 7140
        await self.bot._check_and_trigger(self.bot._states[(False, "friend")], "friend")
        self.bot._check_default_and_trigger.assert_awaited_once()
        r = await self.selected()
        await self.bot.wake_edit(
            "single_save",
            **self.identity(r),
            at=iso(NOW + 7440),
            timezone="UTC",
            intent="明确约定优先",
        )
        self.clock_mock.return_value = NOW + 7200
        await self.bot._run_wakes()
        self.assertEqual(self.calls, [])
        self.assertEqual(next(iter(self.plan()["items"].values()))["block"], "single_priority")

    async def test_batch_unknown_before_rpc_hook_before_ack_and_prompt_consumed_once(self):
        await self.add(300, "整理想法一")
        await self.add(300, "延续兴趣二")

        async def rpc(method, plugin_id, payload, **kwargs):
            if payload["capability"] != "maisaka.proactive.trigger":
                return await self.original_rpc(method, plugin_id, payload, **kwargs)
            self.calls.append(payload["args"])
            rows = list(self.plan()["items"].values())
            self.assertTrue(all(r["status"] == "unknown" for r in rows))
            text = task(1793505900000).replace(
                '{"source": "lonelymai"}', json.dumps(payload["args"]["metadata"])
            )
            response = await asyncio.wait_for(
                self.bot.filter_planner_tasks(session_id="session-friend", items=[item(text)]),
                timeout=0.5,
            )
            content = json.dumps(response, ensure_ascii=False)
            self.assertIn("整理想法一", content)
            self.assertIn("延续兴趣二", content)
            self.assertLess(len(content), 3000)
            self.assertTrue(all(r["shown"] for r in self.plan()["items"].values()))
            self.assertEqual(
                await self.bot._wake_prompt(
                    {"session_id": "session-friend", "items": [item(text)]}
                ),
                [],
            )
            filtered = filter_request(response["modified_kwargs"])[0]
            self.assertEqual(filtered["items"], [])
            return self.result

        self.context._rpc_call = rpc
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        rows = list(self.plan()["items"].values())
        self.assertTrue(all(r["status"] == "submitted" for r in rows))
        self.assertEqual(len({r["planner_requested_at"] for r in rows}), 1)

    async def test_batch_rejection_unknown_cancel_and_final_write_failure_never_partial_replay(
        self,
    ):
        for result in [{"success": False}, None, asyncio.CancelledError(), "write_fail"]:
            with self.subTest(result=str(result)):
                self.clock_mock.return_value = NOW
                self.bot._get_wakes().path.unlink(missing_ok=True)
                await self.add(300)
                await self.add(300, "第二项")
                self.calls = []
                self.result = result if result != "write_fail" else {"success": True}
                original = self.bot._get_wakes().write
                count = 0

                def writer(data):
                    nonlocal count
                    count += 1
                    if result == "write_fail" and count == 2:
                        raise OSError("synthetic final write")
                    original(data)

                self.clock_mock.return_value = NOW + 300
                try:
                    with patch.object(self.bot._get_wakes(), "write", side_effect=writer):
                        await self.bot._run_wakes()
                except (OSError, asyncio.CancelledError):
                    pass
                statuses = {r["status"] for r in self.plan()["items"].values()}
                self.assertEqual(
                    statuses, {"rejected"} if result == {"success": False} else {"unknown"}
                )
                before = len(self.calls)
                self.bot._get_wakes().recover(NOW + 301)
                await self.bot._run_wakes()
                self.assertEqual(len(self.calls), before)

    async def test_cross_day_restart_expiry_and_monotone_day_budget(self):
        first = await self.add()
        self.bot._get_wakes().recover(NOW + 301)
        self.assertEqual(self.plan()["items"][first["item_id"]]["block"], "restart_missed")
        self.clock_mock.return_value = NOW + 86400
        before = self.bot._get_wakes().path.read_bytes()
        self.assertEqual((await self.tool())["dayplan"]["remaining"], 6)
        self.assertEqual(before, self.bot._get_wakes().path.read_bytes())
        await self.add(300, "新一天自己的计划")
        self.assertEqual(len(self.plan()["items"]), 1)
        self.assertTrue(self.plan()["previous"])
        self.assertNotEqual(self.plan()["date"], day.day_key(NOW, "UTC"))
        self.assertEqual(self.calls, [])

    async def test_no_automatic_day_hint_in_normal_or_group_requests(self):
        raw = {"session_id": "session-friend", "items": [item("合成普通私聊")]}
        self.assertIsNone(await self.bot.filter_planner_tasks(**raw))
        self.bot._wakes = None
        self.assertIsNone(await self.bot.filter_planner_tasks(**raw))
        self.assertIsNone(
            await self.bot.filter_planner_tasks(**{**raw, "session_id": "session-g1"})
        )
        self.assertEqual(self.calls, [])
        self.assertFalse(self.bot._get_wakes().path.exists())
        self.assertEqual(self.data()["version"], 4)
        self.assertFalse(hasattr(self.bot, "_day_hints"))
        self.assertFalse(hasattr(self.bot, "_day_hint"))

    async def test_admin_cas_future_only_and_no_hidden_repeat_or_single_mutation(self):
        selected = await self.selected()
        result = await self.bot.wake_edit(
            "day_add",
            **self.identity(selected),
            at=iso(NOW + 300),
            timezone="UTC",
            intent="管理员合成事项",
        )
        self.assertTrue(result["success"], result)
        self.assertFalse(
            (
                await self.bot.wake_edit(
                    "day_add",
                    **self.identity(selected),
                    at=iso(NOW + 600),
                    timezone="UTC",
                    intent="旧页面",
                )
            )["success"]
        )

        latest = await self.selected()
        row = latest["dayplan"]["items"][0]
        self.assertTrue(
            (
                await self.bot.wake_edit(
                    "day_update",
                    **self.identity(latest),
                    item_id=row["item_id"],
                    version=row["version"],
                    intent="新依据",
                )
            )["success"]
        )
        self.assertEqual(self.data()["slots"], {})
        self.assertEqual(self.data()["repeats"], {})
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        latest = await self.selected()
        row = latest["dayplan"]["items"][0]
        self.assertFalse(
            (
                await self.bot.wake_edit(
                    "day_cancel",
                    **self.identity(latest),
                    item_id=row["item_id"],
                    version=row["version"],
                )
            )["success"]
        )

    async def test_strict_live_session_fields_and_revocation_after_query_are_rechecked(self):
        first = await self.add()
        original = copy.deepcopy(self.responses)
        for field, value in [
            ("platform", None),
            ("platform", "other"),
            ("account_id", None),
            ("account_id", "wrong"),
            ("is_group_session", None),
            ("is_group_session", True),
            ("is_group_session", 0),
            ("user_id", "other"),
            ("session_id", "different"),
        ]:
            with self.subTest(field=field, value=value):
                self.responses = copy.deepcopy(original)
                stream = self.responses[(False, "friend")]["stream"]
                if value is None:
                    stream.pop(field)
                else:
                    stream[field] = value
                before = self.bot._get_wakes().path.read_bytes()
                response = await self.tool(
                    "cancel", item_id=first["item_id"], version=first["version"]
                )
                self.assertFalse(response["success"])
                self.assertEqual(before, self.bot._get_wakes().path.read_bytes())
        self.responses = original
        underlying = self.context._rpc_call
        count = 0

        async def revoke(method, plugin_id, payload, **kwargs):
            nonlocal count
            result = await underlying(method, plugin_id, payload, **kwargs)
            count += 1
            if count == 2:
                self.bot.config.target.allowed_friends = []
            return result

        self.context._rpc_call = revoke
        self.assertFalse((await self.tool())["success"])

    async def test_remaining_future_intent_edit_keeps_subminute_time_but_attempted_rows_stay_final(
        self,
    ):
        first = await self.add(336)
        self.clock_mock.return_value = NOW + 310
        result = await self.tool(
            "update", item_id=first["item_id"], version=first["version"], intent="只改未到点的依据"
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(self.plan()["items"][first["item_id"]]["due"], NOW + 336)
        selected = await self.selected()
        fresh = selected["dayplan"]["items"][0]
        wall = datetime.fromtimestamp(NOW + 336, timezone.utc).strftime("%Y-%m-%dT%H:%M")
        result = await self.bot.wake_edit(
            "day_update",
            **self.identity(selected),
            item_id=fresh["item_id"],
            version=fresh["version"],
            when=wall,
            timezone="UTC",
            intent="页面预填分钟未改",
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(self.plan()["items"][first["item_id"]]["due"], NOW + 336)
        self.clock_mock.return_value = NOW + 336
        await self.bot._run_wakes()
        fresh = (await self.tool())["dayplan"]["items"][0]
        raw = self.bot._get_wakes().path.read_bytes()
        self.assertFalse(
            (
                await self.tool(
                    "update",
                    item_id=fresh["item_id"],
                    version=fresh["version"],
                    at=iso(NOW + 600),
                    intent="不可将已提交重写为未来",
                )
            )["success"]
        )
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())

    async def test_unknown_batches_retained_across_days_and_capacity_refuses_without_discard(self):
        self.result = None
        for n in range(6):
            await self.add(300, "第一日未知" + str(n))
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        first_ids = set(self.plan()["items"])
        self.clock_mock.return_value = NOW + 86400
        for n in range(6):
            await self.add(300, "第二日未知" + str(n))
        self.assertEqual({r["id"] for r in self.plan()["retained"]}, first_ids)
        self.clock_mock.return_value = NOW + 86700
        await self.bot._run_wakes()
        self.clock_mock.return_value = NOW + 172800
        raw = self.bot._get_wakes().path.read_bytes()
        response = await self.tool(
            "add", at=iso(NOW + 173100), timezone="UTC", intent="不能清除未知来腾空间"
        )
        self.assertFalse(response["success"])
        self.assertIn("未知", response["message"])
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())

    async def test_corrupt_batch_capacity_and_oversized_storage_remain_fail_closed(self):
        await self.add()
        await self.add()
        original = self.data()
        for mutate in [
            lambda p: p.update(date="2026-99-99"),
            lambda p: p.update(timezone="bad"),
            lambda p: p.update(items={}),
            lambda p: p["items"][next(iter(p["items"]))].update(
                attempted=NOW, status="scheduled", batch_id="a" * 32
            ),
        ]:
            broken = copy.deepcopy(original)
            mutate(broken["dayplans"]["friend"])
            # Empty item dictionaries are valid; exercise a mismatched session instead.
            if not broken["dayplans"]["friend"]["items"]:
                broken["dayplans"]["friend"]["retained"] = [{}]
            raw = json.dumps(broken).encode()
            self.bot._get_wakes().path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.bot._get_wakes().recover(NOW + 400)
            self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())
        self.bot._get_wakes().path.write_bytes(json.dumps(original).encode())
        raw = self.bot._get_wakes().path.read_bytes()
        huge = copy.deepcopy(original)
        huge["padding"] = "x" * 262145
        with self.assertRaises(ValueError):
            self.bot._get_wakes().write(huge)
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())
        many = copy.deepcopy(original)
        many["dayplans"] = {
            str(n): copy.deepcopy(original["dayplans"]["friend"]) for n in range(129)
        }
        with self.assertRaises(ValueError):
            self.bot._get_wakes().write(many)
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())

    async def test_other_target_write_survives_unlocked_batch_and_concurrent_pass(self):
        await self.add()
        await self.add(intent="批次第二项")
        self.bot.config.target.allowed_friends.append("other")
        self.responses[(False, "other")] = {
            "success": True,
            "stream": fixtures.host_stream("other", False),
        }
        entered, release = asyncio.Event(), asyncio.Event()
        underlying = self.context._rpc_call

        async def pending(method, plugin_id, payload, **kwargs):
            if payload["capability"] != "maisaka.proactive.trigger":
                return await underlying(method, plugin_id, payload, **kwargs)
            self.calls.append(payload["args"])
            entered.set()
            await release.wait()
            return self.result

        self.context._rpc_call = pending
        self.clock_mock.return_value = NOW + 300
        active = asyncio.create_task(self.bot._run_wakes())
        await entered.wait()
        self.host.update(user_id="other", session_id="session-other", stream_id="session-other")
        self.assertTrue(
            (
                await self.tool(
                    "add", at=iso(NOW + 600), timezone="UTC", intent="其他目标自己的事项"
                )
            )["success"]
        )
        await self.bot._run_wakes()
        release.set()
        await active
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.data()["dayplans"]["other"]["items"]), 1)
        self.assertTrue(all(r["status"] == "submitted" for r in self.plan()["items"].values()))

    async def test_dayplan_does_not_inherit_base_rules_and_pause_is_global(self):
        await self.add()
        cfg = self.bot.config.scheduler
        cfg.start_time = cfg.end_time = "00:00"
        cfg.probability = 0
        cfg.min_interval_between_chats = 60
        cfg.isolation_time = 60
        self.bot._states[(False, "friend")].last_proactive_time = NOW + 299
        cfg.enabled = False
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        self.assertEqual(self.calls, [])
        cfg.enabled = True
        await self.bot._run_wakes()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(next(iter(self.plan()["items"].values()))["attempted"], NOW + 300)

    async def test_normal_chat_does_not_inject_day_reminders_or_touch_existing_v2(self):
        selected = await self.selected()
        await self.bot.wake_edit(
            "repeat_set",
            **self.identity(selected),
            kind="interval",
            interval_minutes=120,
            timezone="UTC",
            intent="保留节奏",
        )
        raw = self.bot._get_wakes().path.read_bytes()
        request = {"session_id": "session-friend", "items": [item("合成普通私聊")]}
        self.assertIsNone(await self.bot.filter_planner_tasks(**request))
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())
        self.assertEqual(self.data()["version"], 4)
        self.clock_mock.return_value = NOW + 86400
        self.assertIsNone(await self.bot.filter_planner_tasks(**request))
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())

    async def test_midnight_during_host_lookup_expires_instead_of_late_submission(self):
        date = datetime.fromtimestamp(NOW, timezone.utc).date()
        midnight = (
            datetime(date.year, date.month, date.day, tzinfo=timezone.utc).timestamp() + 86400
        )
        self.clock_mock.return_value = midnight - 180
        await self.add(120)
        underlying = self.context._rpc_call

        async def cross_midnight(method, plugin_id, payload, **kwargs):
            result = await underlying(method, plugin_id, payload, **kwargs)
            if payload["capability"] == "chat.get_stream_by_user_id":
                self.clock_mock.return_value = midnight + 1
            return result

        self.clock_mock.return_value = midnight - 60
        self.context._rpc_call = cross_midnight
        await self.bot._run_wakes()
        self.assertEqual(self.calls, [])
        self.assertEqual(next(iter(self.plan()["items"].values()))["block"], "day_ended")

    async def test_near_future_day_items_coalesce_without_early_submission(self):
        await self.add(300, "先到点事项")
        await self.add(330, "30秒后的事项")
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        self.assertEqual(self.calls, [])
        first = sorted(self.plan()["items"].values(), key=lambda r: r["due"])[0]
        self.assertEqual(first["block"], "day_merge_wait")
        self.assertEqual(first["retry"], NOW + 330)
        self.clock_mock.return_value = NOW + 330
        await self.bot._run_wakes()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(r["status"] == "submitted" for r in self.plan()["items"].values()))
        self.assertEqual(len({r["batch_id"] for r in self.plan()["items"].values()}), 1)

    async def test_only_primary_batch_id_can_consume_combined_purpose(self):
        await self.add()
        await self.add(intent="第二项依据")
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        rows = list(self.plan()["items"].values())
        peer = next(r for r in rows if r["id"] != r["batch_id"])
        forged = task(1793505900000).replace(
            '{"source": "lonelymai"}', json.dumps({"neko_wake_id": peer["id"]})
        )
        raw = self.bot._get_wakes().path.read_bytes()
        self.assertEqual(
            await self.bot._wake_prompt({"session_id": "session-friend", "items": [item(forged)]}),
            [],
        )
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())
        legitimate = task(1793505900000).replace(
            '{"source": "lonelymai"}', json.dumps(self.calls[0]["metadata"])
        )
        self.assertTrue(
            await self.bot._wake_prompt(
                {"session_id": "session-friend", "items": [item(legitimate)]}
            )
        )
        self.assertTrue(all(r["shown"] for r in self.plan()["items"].values()))

    async def test_global_pause_still_allows_admin_cancel_and_corrupt_partial_batch_rejected(self):
        await self.add()
        await self.add(intent="第二项合成事项")
        selected = await self.selected()
        first = selected["dayplan"]["items"][0]
        self.bot.config.scheduler.enabled = False
        result = await self.bot.wake_edit(
            "day_cancel",
            **self.identity(selected),
            item_id=first["item_id"],
            version=first["version"],
        )
        self.assertTrue(result["success"], result)
        selected = await self.selected()
        self.assertFalse(
            (
                await self.bot.wake_edit(
                    "day_add",
                    **self.identity(selected),
                    at=iso(NOW + 600),
                    timezone="UTC",
                    intent="不能新建",
                )
            )["success"]
        )
        self.bot.config.scheduler.enabled = True
        await self.add(intent="第三项合成事项")
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        data = self.data()
        attempted = [r for r in data["dayplans"]["friend"]["items"].values() if r["attempted"]]
        self.assertEqual(len(attempted), 2)
        attempted[0]["status"] = "unknown"
        raw = json.dumps(data).encode()
        self.bot._get_wakes().path.write_bytes(raw)
        with self.assertRaises(ValueError):
            self.bot._get_wakes().recover(NOW + 301)
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())


class DayCalendarTests(unittest.TestCase):
    def test_dst_absolute_offsets_are_explicit_and_missing_wall_time_rejected(self):
        now = datetime(2026, 11, 1, 4, tzinfo=timezone.utc).timestamp()
        data = {
            "version": 4,
            "last_dispatch": 0,
            "slots": {},
            "appointments": {},
            "repeats": {},
            "dayplans": {},
        }
        a = day.change_day(
            data,
            "friend",
            "session",
            now,
            "day_add",
            at="2026-11-01T01:30:00-04:00",
            timezone="America/New_York",
            intent="较早一次",
        )
        b = day.change_day(
            data,
            "friend",
            "session",
            now,
            "day_add",
            at="2026-11-01T01:30:00-05:00",
            timezone="America/New_York",
            intent="明确另一绝对时刻",
        )
        self.assertEqual(b["due"] - a["due"], 3600)
        spring = datetime(2026, 3, 8, 5, tzinfo=timezone.utc).timestamp()
        with self.assertRaises(ValueError):
            day.change_day(
                {"version": 4, "slots": {}, "appointments": {}, "repeats": {}, "dayplans": {}},
                "friend",
                "session",
                spring,
                "day_add",
                at="2026-03-08T02:30:00-05:00",
                timezone="America/New_York",
                intent="不存在的当地时间",
            )


if __name__ == "__main__":
    unittest.main()
