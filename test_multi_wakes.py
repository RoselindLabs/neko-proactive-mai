"""Independent one-shot reservations; synthetic Host and clocks, no live I/O."""

import asyncio
import copy
import importlib
import json
import unittest
from unittest.mock import patch

import test_wakes as fixtures
import test_chat_sdk as chats
from test_wakes import NOW, iso, mod

store_module = importlib.import_module("wake_test.wake_store")
day = importlib.import_module("wake_test.dayplan")


class MultiWakeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.WakeTests.setUp
    tool = fixtures.WakeTests.tool
    due = fixtures.WakeTests.due

    async def add(self, delay=300, intent="稍后继续话题"):
        result = await self.bot.private_wake(
            "add", at=iso(NOW + delay), timezone="UTC", intent=intent, _host_context=self.host
        )
        self.assertTrue(result["success"], result)
        return next(it for it in result["items"] if it["id"] == result["wake"]["id"])

    def data(self):
        return self.bot._get_wakes().read()

    async def test_pre_v4_and_unknown_formats_are_never_auto_migrated(self):
        store = self.bot._get_wakes()
        self.assertEqual(store.read()["version"], 4)
        self.assertFalse(store.path.exists())
        for version in (1, 2, 3, 6):
            data = {
                "version": version,
                "last_dispatch": 0,
                "slots": {},
                "appointments": {},
                "repeats": {},
                "dayplans": {},
            }
            store.path.write_text(json.dumps(data), encoding="utf-8")
            before = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.recover(NOW)
            with self.assertRaises(ValueError):
                store.write({**data, "version": 4})
            self.assertEqual(store.path.read_bytes(), before)

    async def change(self, action, it, **fields):
        return await self.bot.private_wake(
            action, item_id=it["item_id"], version=it["version"], _host_context=self.host, **fields
        )

    async def test_many_adds_keep_ids_and_old_record_bytes_untouched(self):
        first = await self.add(360)
        original = copy.deepcopy(self.data()["slots"]["friend"])
        for n in range(12):
            await self.add(600 + n * 60, "话题" + str(n))
        data = self.data()
        self.assertEqual(data["version"], 4)
        self.assertEqual(data["slots"]["friend"], original)
        items = (await self.tool())["items"]
        self.assertEqual(len(items), 13)
        self.assertEqual(len({it["item_id"] for it in items}), 13)
        self.assertEqual(items[0]["item_id"], first["item_id"])
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_update_and_cancel_only_selected_item_and_stale_versions_refused(self):
        first, second = await self.add(), await self.add(600)
        before = copy.deepcopy(self.data()["slots"]["friend"])
        changed = await self.change("update", second, at="20分钟后", intent="换个话题")
        self.assertTrue(changed["success"], changed)
        self.assertEqual(changed["wake"]["id"], second["id"])
        self.assertEqual(changed["wake"]["due"], NOW + 1200)
        self.assertEqual(self.data()["slots"]["friend"], before)
        raw = self.bot._get_wakes().path.read_bytes()
        self.assertFalse((await self.change("cancel", second))["success"])
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())
        self.assertTrue((await self.change("cancel", first))["success"])
        rows = store_module.singles(self.data(), "friend")
        self.assertEqual([r["status"] for r in rows], ["cancelled", "scheduled"])

    async def test_missing_id_never_cancels_all_or_picks_a_random_item(self):
        await self.add()
        await self.add(600)
        raw = self.bot._get_wakes().path.read_bytes()
        for action in ("cancel", "update"):
            for fields in (
                {},
                {"item_id": "0" * 32, "version": "a" * 64},
                {"item_id": [], "version": {}},
            ):
                result = await self.bot.private_wake(action, _host_context=self.host, **fields)
                self.assertFalse(result["success"], result)
                self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())

    async def test_current_session_only_get_and_cross_session_edits_refused(self):
        old = await self.add()
        data = self.data()
        data["slots"]["friend"]["session"] = "old-session"
        self.bot._get_wakes().write(data)
        own = await self.add(600)
        items = (await self.tool())["items"]
        self.assertEqual([it["item_id"] for it in items], [own["item_id"]])
        old["version"] = day.item_version(self.data()["slots"]["friend"])
        self.assertFalse((await self.change("cancel", old))["success"])

    async def test_due_items_dispatch_independently_with_persistent_spacing(self):
        first, second = await self.add(120), await self.add(120)
        await self.due(120)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        await self.due(120)
        rows = store_module.singles(self.data(), "friend")
        self.assertEqual(rows[0]["status"], "submitted")
        self.assertEqual(rows[1]["status"], "blocked")
        self.bot._wakes = mod.WakeStore(self.directory.name)
        await self.due(179)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        await self.due(180)
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 2)
        self.assertEqual(
            {r["id"] for r in store_module.singles(self.data(), "friend")},
            {first["id"], second["id"]},
        )

    async def test_far_multi_items_do_not_suppress_default(self):
        await self.add(3600)
        await self.add(7200)
        before = copy.deepcopy(store_module.singles(self.data(), "friend"))
        await self.bot._check_and_trigger(self.bot._states[(False, "friend")], "friend")
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        self.assertEqual(store_module.singles(self.data(), "friend"), before)

    async def test_restart_expires_every_missed_item_but_keeps_future_unknown_and_guards(self):
        await self.add(120)
        second = await self.add(180)
        await self.add(900)
        data = self.data()
        row = data["appointments"]["friend"][second["id"]]
        row.update(status="unknown", attempted=NOW + 100, last_outcome="unknown")
        data["last_dispatch"] = NOW + 100
        self.bot._get_wakes().write(data)
        self.bot._get_wakes().recover(NOW + 300)
        rows = store_module.singles(self.data(), "friend")
        self.assertEqual([r["status"] for r in rows], ["expired", "unknown", "scheduled"])
        self.assertEqual(self.data()["last_dispatch"], NOW + 100)

    async def test_extra_occurrence_hook_before_ack_finds_and_updates_only_its_id(self):
        from test_prompt_visibility import item, task

        await self.add(600)
        second = await self.add(120, "第二条意图")

        async def trigger(**kwargs):
            self.assertFalse(self.bot._wake_lock.locked())
            data = self.data()
            row = data["appointments"]["friend"][second["id"]]
            self.assertEqual(row["status"], "unknown")
            stamp = int((NOW + 120) * 1000)
            task_text = task(stamp).replace(
                '{"source": "lonelymai"}', json.dumps(kwargs["metadata"])
            )
            request = {"session_id": "session-friend", "items": [item(task_text)]}
            result = await self.bot._wake_prompt(request)
            self.assertIn("第二条意图", json.dumps(result, ensure_ascii=False))
            self.assertEqual(await self.bot._wake_prompt(request), [])
            return {"success": True}

        self.bot.ctx.maisaka.proactive.trigger.side_effect = trigger
        await self.due(120)
        rows = store_module.singles(self.data(), "friend")
        self.assertEqual(rows[0]["status"], "scheduled")
        self.assertEqual(rows[1]["status"], "submitted")
        self.assertTrue(rows[1]["shown"])

    async def test_extra_unknown_survives_exception_and_cancellation_without_replay(self):
        for error in (RuntimeError("unknown"), asyncio.CancelledError()):
            self.bot._get_wakes().path.unlink(missing_ok=True)
            self.clock_mock.return_value = NOW
            await self.add(900)
            extra = await self.add(120)
            self.bot.ctx.maisaka.proactive.trigger.reset_mock()
            self.bot.ctx.maisaka.proactive.trigger.side_effect = error
            try:
                await self.due(120)
            except asyncio.CancelledError:
                pass
            self.assertIsNone(self.bot._wake_inflight)
            self.assertEqual(
                self.data()["appointments"]["friend"][extra["id"]]["status"], "unknown"
            )
            await self.due(240)
            self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()

    async def test_dayplan_add_does_not_downgrade_v4(self):
        await self.add()
        await self.add(600)
        data = self.data()
        day.change_day(
            data,
            "friend",
            "session-friend",
            NOW,
            "day_add",
            at=iso(NOW + 900),
            timezone="UTC",
            intent="今天的话题",
        )
        self.bot._get_wakes().write(data)
        self.assertEqual(self.data()["version"], 4)
        self.assertEqual(len(store_module.singles(self.data(), "friend")), 2)

    async def test_v4_corruption_and_capacity_fail_without_overwriting_disk(self):
        await self.add()
        second = await self.add(600)
        path = self.bot._get_wakes().path
        original = path.read_bytes()
        for mutate in (
            lambda d: d["appointments"]["friend"][second["id"]].update(status="invalid"),
            lambda d: d.update(appointments=[]),
            lambda d: d["appointments"]["friend"].update(
                {"bad": d["appointments"]["friend"].pop(second["id"])}
            ),
        ):
            data = json.loads(original)
            mutate(data)
            path.write_text(json.dumps(data), encoding="utf-8")
            broken = path.read_bytes()
            with self.assertRaises(ValueError):
                self.bot._get_wakes().recover(NOW)
            self.assertEqual(path.read_bytes(), broken)
        path.write_bytes(original)
        with patch.object(store_module, "MAX_BYTES", len(original) + 20):
            result = await self.bot.private_wake(
                "add", at="10分钟后", intent="x" * 240, _host_context=self.host
            )
        self.assertFalse(result["success"])
        self.assertEqual(path.read_bytes(), original)

    async def test_v4_version_contract_is_not_accepted_by_old_reader(self):
        await self.add()
        await self.add(600)
        self.assertEqual(self.data()["version"], 4)
        # Pinned 2.8.0 accepts versions 1/2/3 only. A separate local probe runs
        # that actual historical source; CI does not depend on checkout history.
        self.assertNotIn(self.data()["version"], (1, 2, 3))


class MultiManagementTests(unittest.IsolatedAsyncioTestCase):
    setUp = chats.ChatSDKTests.setUp
    asyncSetUp = chats.ChatSDKTests.asyncSetUp
    selected = chats.ChatSDKTests.selected
    identity = chats.ChatSDKTests.identity

    async def test_real_sdk_admin_list_add_update_cancel_cas_and_no_hidden_mutation(self):
        for n in range(3):
            r = await self.selected()
            result = await self.bot.wake_edit(
                "single_save",
                **self.identity(r),
                at=iso(NOW + 300 + n * 60),
                timezone="UTC",
                intent="话题" + str(n),
            )
            self.assertTrue(result["success"], result)
        selected = await self.selected()
        self.assertEqual(len(selected["singles"]), 3)
        it = selected["singles"][1]
        original = copy.deepcopy(self.bot._get_wakes().read())
        result = await self.bot.wake_edit(
            "single_save",
            **self.identity(selected),
            item_id=it["item_id"],
            version=it["version"],
            intent="只改第二条",
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(self.bot._get_wakes().read()["slots"], original["slots"])
        self.assertFalse(
            (
                await self.bot.wake_edit(
                    "single_cancel",
                    **self.identity(selected),
                    item_id=it["item_id"],
                    version=it["version"],
                )
            )["success"]
        )
        r = await self.selected()
        it = r["singles"][1]
        self.assertTrue(
            (
                await self.bot.wake_edit(
                    "single_cancel",
                    **self.identity(r),
                    item_id=it["item_id"],
                    version=it["version"],
                )
            )["success"]
        )
        statuses = [it["status"] for it in (await self.selected())["singles"]]
        self.assertEqual(statuses, ["scheduled", "cancelled", "scheduled"])

    def test_tool_copy_contains_only_useful_model_information(self):
        for component in self.bot.get_components():
            if component["name"] in {
                "neko_proactive_wake",
                "neko_proactive_repeat",
                "neko_proactive_dayplan",
            }:
                copy_text = component["metadata"]["description"]
                for internal in ("基础周期", "基础唤醒", "防重", "安全间距", "Host", "调度器"):
                    self.assertNotIn(internal, copy_text)
