"""Native management API tests. Real SDK, synthetic identities/deadlines and disk."""

import asyncio
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from test_wakes import NOW, iso, mod
import test_wakes as fixtures


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.WakeTests.setUp
    tool = fixtures.WakeTests.tool
    reserve = fixtures.WakeTests.reserve
    rows = fixtures.WakeTests.rows
    due = fixtures.WakeTests.due

    async def asyncSetUp(self):
        async def private(user):
            return {
                "success": True,
                "stream": {
                    "session_id": "session-" + user,
                    "platform": "qq",
                    "user_id": user,
                    "user_nickname": "好友" + user,
                    "user_cardname": "",
                    "account_id": "bot",
                    "is_group_session": False,
                },
            }

        async def group(target):
            return {
                "success": True,
                "stream": {
                    "session_id": "session-group",
                    "platform": "qq",
                    "group_id": target,
                    "group_name": "测试群",
                    "account_id": "bot",
                    "is_group_session": True,
                },
            }

        self.bot.ctx.chat.get_stream_by_user_id.side_effect = private
        self.bot.ctx.chat.get_stream_by_group_id.side_effect = group
        self.bot._scheduler_task = Mock(done=Mock(return_value=False))
        self.bot._next_regular_at = NOW + 1800
        self.bot._next_pass_at = NOW + 1800
        self.bot._scheduler_phase = "waiting"
        self.bot._states[(True, "group")] = mod.ChatStreamState(
            "session-group", "group", is_group=True
        )

    async def selected(self, target="friend"):
        return next(
            r for r in (await self.bot.wake_overview())["reservations"] if r["target"] == target
        )

    def identity(self, row):
        return {key: row[key] for key in ("target", "session", "revision")}

    async def save(self, row=None, **kwargs):
        row = await self.selected() if row is None else row
        active = next((it for it in row["singles"] if it["can_edit"]), None)
        if active:
            kwargs = {"item_id": active["item_id"], "version": active["version"], **kwargs}
        return await self.bot.wake_save(**self.identity(row), **kwargs)

    async def test_native_apis_are_private_and_page_has_only_management_actions(self):
        components = {c["name"]: c for c in self.bot.get_components()}
        for name in ("wake_overview", "wake_save", "wake_cancel", "wake_edit"):
            self.assertIs(components[name]["metadata"]["public"], False)
        page = json.loads((Path(__file__).parent / "webui.json").read_text(encoding="utf-8"))[
            "pages"
        ][0]
        self.assertEqual(set(page["actions"]), {"save", "cancel"})
        self.assertEqual(page["mode"], "custom")
        self.assertIn("confirmation", page["actions"]["cancel"])
        self.assertEqual(set(page["queries"]), {"overview"})

    async def test_read_only_missing_or_existing_index_has_no_side_effects_or_history_reads(self):
        self.bot.ctx.message = Mock()
        path = self.bot._get_wakes().path
        first = await self.bot.wake_overview()
        self.assertFalse(path.exists())
        self.assertEqual(len(first["base"]), 3)
        self.assertEqual(len(first["reservations"]), 2)
        self.assertIn("测试群", first["base"][0]["chat"])
        await self.reserve()
        before, state, event = (
            path.read_bytes(),
            copy.deepcopy(self.state.__dict__),
            self.bot._wake_event.is_set(),
        )
        self.bot._wake_event.clear()
        second = await self.bot.wake_overview()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.state.__dict__, state)
        self.assertFalse(self.bot._wake_event.is_set())
        self.assertTrue(event)
        self.assertEqual(
            second["deadline"],
            mod.datetime.fromtimestamp(NOW + 1800).astimezone().isoformat(timespec="seconds"),
        )
        self.bot.ctx.message.get_by_time_in_chat.assert_not_called()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_admin_create_intent_only_reschedule_cancel_share_tool_business_and_deadline(
        self,
    ):
        result = await self.save(at=iso(NOW + 120), timezone="UTC", intent="有依据的新话头")
        self.assertTrue(result["success"])
        self.assertTrue(self.bot._wake_event.is_set())
        self.assertEqual(self.bot._next_regular_at, NOW + 1800)
        self.assertEqual((await self.tool())["wake"]["intent"], "有依据的新话头")
        await self.save(intent="只改意图")
        self.assertEqual(self.rows()["friend"]["due"], NOW + 120)
        self.assertEqual(self.rows()["friend"]["intent"], "只改意图")
        await self.save(at=iso(NOW + 180), timezone="UTC")
        self.assertEqual(self.rows()["friend"]["due"], NOW + 180)
        row = await self.selected()
        it = row["singles"][0]
        await self.bot.wake_cancel(
            **self.identity(row), item_id=it["item_id"], version=it["version"]
        )
        self.assertEqual((await self.tool())["wake"]["status"], "cancelled")
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_model_and_admin_mutations_use_same_lock_cas_prevents_late_or_duplicate_save(
        self,
    ):
        row = await self.selected()
        await self.reserve(120)
        with self.assertRaisesRegex(ValueError, "刷新"):
            await self.save(row, at=iso(NOW + 180), timezone="UTC", intent="旧页面")
        row = await self.selected()
        results = await asyncio.gather(
            self.save(row, intent="第一个"), self.save(row, intent="第二个"), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.assertIn(self.rows()["friend"]["intent"], {"第一个", "第二个"})
        before = self.rows()
        with self.assertRaises(ValueError):
            await self.save(row, intent="重复请求")
        self.assertEqual(self.rows(), before)

    async def test_unlisted_group_cross_session_and_forged_revision_rejected_before_write(self):
        row = await self.selected()
        for identity in [
            {**self.identity(row), "target": "stranger"},
            {**self.identity(row), "target": "group", "session": "session-group"},
            {**self.identity(row), "session": "session-other"},
            {**self.identity(row), "target": "other"},
            {**self.identity(row), "revision": "fake"},
        ]:
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    await self.bot.wake_save(
                        **identity, at=iso(NOW + 60), timezone="UTC", intent="依据"
                    )
        self.assertEqual(self.rows(), {})
        with self.assertRaises(TypeError):
            await self.bot.wake_cancel(**self.identity(row), user_id="stranger")

    async def test_disabled_configuration_visible_cancel_allowed_new_and_replace_refused(self):
        await self.reserve()
        for section in (self.bot.config.plugin, self.bot.config.scheduler):
            section.enabled = False
            overview = await self.bot.wake_overview()
            row = next(r for r in overview["reservations"] if r["target"] == "friend")
            self.assertTrue(row["cannot_save"])
            self.assertFalse(row["cannot_cancel"])
            with self.assertRaisesRegex(ValueError, "关闭"):
                await self.save(row, at=iso(NOW + 120), timezone="UTC", intent="依据")
            it = row["singles"][0]
            await self.bot.wake_cancel(
                **self.identity(row), item_id=it["item_id"], version=it["version"]
            )
            self.assertEqual(self.rows()["friend"]["status"], "cancelled")
            section.enabled = True
            await self.reserve()

    async def test_hot_revocation_and_real_host_chat_type_account_session_rechecked(self):
        row = await self.selected()
        original = self.bot.ctx.chat.get_stream_by_user_id.side_effect
        for changes in [
            {"is_group_session": True},
            {"platform": "other"},
            {"user_id": "other"},
            {"account_id": "wrong"},
            {"session_id": "new-session"},
        ]:

            async def changed(target):
                result = await original(target)
                result["stream"].update(changes)
                return result

            self.bot.ctx.chat.get_stream_by_user_id.side_effect = changed
            with self.assertRaises(ValueError):
                await self.save(row, at=iso(NOW + 60), timezone="UTC", intent="依据")

        async def revoke(target):
            self.bot.config.target.allowed_friends = []
            return await original(target)

        self.bot.ctx.chat.get_stream_by_user_id.side_effect = revoke
        with self.assertRaises(ValueError):
            await self.save(row, at=iso(NOW + 60), timezone="UTC", intent="依据")
        self.assertEqual(self.rows(), {})

    async def test_invalid_forms_leave_state_and_report_failure_not_a_false_success(self):
        await self.reserve(120)
        original = self.rows()
        for values in [
            {"at": "bad"},
            {"at": iso(NOW + 59)},
            {"at": iso(NOW + 7 * 86400 + 1)},
            {"timezone": "Asia/Shanghai"},
            {"timezone": "bad"},
            {"intent": "x" * 241},
            {"intent": "a\nb"},
            {"intent": 123},
        ]:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    await self.save(**values)
                self.assertEqual(self.rows(), original)
                self.assertIn("操作未完成", self.bot._management_operation)

    async def test_pending_plan_keeps_real_default_deadline_and_only_near_plan_defers(
        self,
    ):
        await self.reserve()
        overview = await self.bot.wake_overview()
        base = next(r for r in overview["base"] if r["kind"] == "私聊" and "friend" in r["chat"])
        self.assertEqual(base["state"], "临近计划，短暂避让")
        self.assertNotEqual(base["next"], "单次预约待执行，基础唤醒暂停")
        row = await self.selected()
        it = row["singles"][0]
        await self.bot.wake_cancel(
            **self.identity(row), item_id=it["item_id"], version=it["version"]
        )
        self.state.last_proactive_time = NOW
        self.bot.config.scheduler.isolation_time = 15
        self.bot._note_check(self.state, "isolation", last_message=NOW)
        base = next(r for r in (await self.bot.wake_overview())["base"] if "friend" in r["chat"])
        self.assertNotEqual(base["next"], "单次预约待执行，基础唤醒暂停")
        self.assertEqual(base["state"], "最小间隔未到")
        self.assertIn("冷却至", base["guard"])
        self.assertIn("静默至", base["guard"])

    async def test_unknown_has_no_replay_explicit_new_preserves_guard_and_does_not_claim_delivery(
        self,
    ):
        await self.reserve()
        self.bot.ctx.maisaka.proactive.trigger.return_value = {}
        await self.due()
        row = await self.selected()
        self.assertEqual(row["status"], "提交结果未知")
        self.assertTrue(row["cannot_cancel"])
        with self.assertRaises(ValueError):
            await self.save(row, intent="不能重试旧时刻")
        attempted = self.rows()["friend"]["attempted"]
        await self.save(at=iso(NOW + 120), timezone="UTC", intent="明确新预约")
        self.assertEqual(self.rows()["friend"]["attempted"], attempted)
        self.assertIn("未知", (await self.selected())["last"])
        await self.due(119)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        await self.due(120)
        self.assertEqual(self.rows()["friend"]["status"], "unknown")
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 2)

    async def test_due_blocked_edit_refused_without_changing_original(self):
        await self.reserve()
        data = self.bot._get_wakes().read()
        data["last_dispatch"] = NOW + 59
        self.bot._get_wakes().write(data)
        await self.due()
        before = self.rows()["friend"]
        selected = await self.selected()
        it = selected["singles"][0]
        with self.assertRaises(ValueError):
            await self.bot.wake_save(
                **self.identity(selected),
                item_id=it["item_id"],
                version=it["version"],
                intent="新的依据",
            )
        after = self.rows()["friend"]
        for name in ("due", "retry", "status", "attempted", "block"):
            self.assertEqual(after[name], before[name])
        self.assertEqual(after["intent"], before["intent"])

    async def test_inactive_records_redact_old_intent_and_read_does_not_mark_them_revoked(self):
        await self.reserve(intent="私有旧意图")
        before = self.bot._get_wakes().path.read_bytes()
        self.bot.config.target.allowed_friends = ["other"]
        snapshot = await self.bot.wake_overview()
        self.assertNotIn("私有旧意图", json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual(len(snapshot["inactive"]), 1)
        self.assertEqual(self.bot._get_wakes().path.read_bytes(), before)

    async def test_delete_terminal_single_keeps_other_rows_and_dispatch_record(self):
        await self.reserve()
        await self.due()
        row = await self.selected()
        it = row["singles"][0]
        before = self.bot._get_wakes().read()
        self.assertTrue(it["can_delete"])
        result = await self.bot.wake_edit(
            "single_delete", **self.identity(row), item_id=it["item_id"], version=it["version"]
        )
        self.assertTrue(result["success"], result)
        expected = copy.deepcopy(before)
        expected["slots"]["friend"]["dismissed"] = True
        self.assertEqual(self.bot._get_wakes().read(), expected)
        self.assertEqual((await self.selected())["singles"], [])
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()

    async def test_corrupt_store_is_error_and_preserved_not_an_empty_success(self):
        path = self.bot._get_wakes().path
        path.write_bytes(b"broken")
        with self.assertRaises(ValueError):
            await self.bot.wake_overview()
        with self.assertRaises(ValueError):
            await self.bot.wake_save(
                target="friend",
                session="session-friend",
                revision="x",
                at=iso(NOW + 60),
                timezone="UTC",
                intent="依据",
            )
        self.assertEqual(path.read_bytes(), b"broken")

    async def test_actual_single_scheduler_deadlines_exposed_not_read_time_plus_interval(self):
        self.bot._scheduler_task = None
        self.bot._next_sleep_seconds = lambda: 3600
        loop = asyncio.create_task(self.bot._schedule_loop())
        self.bot._scheduler_task = loop
        try:
            for _ in range(30):
                await asyncio.sleep(0)
            deadline = self.bot._next_regular_at
            self.assertIsNotNone(deadline)
            self.clock_mock.return_value = NOW + 20
            snapshot = await self.bot.wake_overview()
            self.assertEqual(
                snapshot["deadline"],
                mod.datetime.fromtimestamp(deadline).astimezone().isoformat(timespec="seconds"),
            )
            self.assertNotIn(iso(NOW + 3620), snapshot["deadline"])
            before = deadline
            await self.save(at=iso(NOW + 120), timezone="UTC", intent="依据")
            for _ in range(30):
                await asyncio.sleep(0)
            self.assertEqual(self.bot._next_regular_at, before)
            self.assertLess(self.bot._next_pass_at, before)
        finally:
            await self.bot.on_unload()

    async def test_unload_denies_new_writes_and_config_epoch_prevents_stale_read(self):
        row = await self.selected()
        self.bot._wake_ready = False
        with self.assertRaises(ValueError):
            await self.save(row, at=iso(NOW + 60), timezone="UTC", intent="依据")
        self.assertTrue((await self.selected())["cannot_cancel"])
        original = self.bot.ctx.chat.get_stream_by_user_id.side_effect

        async def change(target):
            self.bot._management_epoch += 1
            return await original(target)

        self.bot.ctx.chat.get_stream_by_user_id.side_effect = change
        with self.assertRaises(ValueError):
            await self.bot.wake_overview()

    async def test_projection_and_telemetry_bounded_without_keys_messages_or_thinking(self):
        for n in range(300):
            state = mod.ChatStreamState("session" + str(n), str(n))
            self.bot._note_check(state, "eligible")
            self.bot._record_submission(state, "submitted", "default")
        self.assertEqual(len(self.bot._observations), 256)
        self.assertEqual(len(self.bot._submission_results), 256)
        self.bot.config.target.allowed_friends = [str(n) for n in range(140)]
        snapshot = await self.bot.wake_overview()
        self.assertEqual(len(snapshot["base"]), 128)
        self.assertIn("128", snapshot["limits"])
        self.assertNotIn("_host_context", json.dumps(snapshot))

    async def test_last_appointment_outcome_survives_explicit_rearm_and_store_reload(self):
        await self.reserve()
        self.bot.ctx.maisaka.proactive.trigger.return_value = {"success": False}
        await self.due()
        self.assertEqual(self.rows()["friend"]["last_outcome"], "rejected")
        await self.save(at=iso(NOW + 180), timezone="UTC", intent="明确另建")
        self.bot._submission_results = {}
        self.bot._wakes = None
        fresh = await self.selected()
        self.assertEqual(fresh["status"], "待唤醒")
        self.assertIn("宿主已拒绝", fresh["last"])
        self.assertEqual(self.rows()["friend"]["attempted"], NOW + 60)

    async def test_plan_admission_outside_default_activity_hours_keeps_actual_due(
        self,
    ):
        self.bot.config.scheduler.start_time = "08:00"
        self.bot.config.scheduler.end_time = "23:00"
        # Exact local date shape is synthetic; choose tomorrow at 02:00.
        moment = mod.datetime.fromtimestamp(NOW).replace(hour=2, minute=0, second=0)
        due = moment.timestamp() + 86400
        await self.save(at=iso(due), timezone="UTC", intent="明日再考虑")
        selected = await self.selected()
        projected = mod.datetime.fromisoformat(selected["earliest"])
        self.assertEqual(projected.hour, 2)
        self.assertEqual(projected.timestamp(), due)

    async def test_inflight_submission_rejects_edit_without_blocking_hook_storage_lock(self):
        await self.reserve()
        selected = await self.selected()
        entered, release = asyncio.Event(), asyncio.Event()

        async def pending(**kwargs):
            entered.set()
            await release.wait()
            return {"success": True}

        self.bot.ctx.maisaka.proactive.trigger.side_effect = pending
        self.clock_mock.return_value = NOW + 60
        trigger = asyncio.create_task(self.bot._run_wakes())
        await entered.wait()
        change = asyncio.create_task(
            self.save(selected, at=iso(NOW + 180), timezone="UTC", intent="旧页面")
        )
        with self.assertRaises(ValueError):
            await asyncio.wait_for(change, timeout=1)
        release.set()
        await trigger
        self.assertEqual(self.rows()["friend"]["status"], "submitted")
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()

    async def test_disabled_initial_load_can_read_real_account_sessions_without_starting_scheduler(
        self,
    ):
        self.bot._scheduler_task = None
        self.bot._bot_qq = ""
        self.bot.config.plugin.enabled = False
        await self.bot.on_load()
        self.assertIsNone(self.bot._scheduler_task)
        self.assertEqual(self.bot._bot_qq, "bot")
        snapshot = await self.bot.wake_overview()
        self.assertEqual(len(snapshot["reservations"]), 2)
        self.assertTrue(all(row["cannot_save"] for row in snapshot["reservations"]))


if __name__ == "__main__":
    unittest.main()
