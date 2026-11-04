import importlib.util
import pathlib
import sys
import tomllib
import types
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from test_wakes import host_stream


@unittest.skipUnless(importlib.util.find_spec("maibot_sdk"), "requires MaiBot SDK")
class LifeIntentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = pathlib.Path(__file__).parent
        pkg = types.ModuleType("lonely_intent_test")
        pkg.__path__ = [str(self.root)]
        sys.modules[pkg.__name__] = pkg
        spec = importlib.util.spec_from_file_location(
            "lonely_intent_test.plugin", self.root / "plugin.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.mod = mod

        class Bot(mod.LonelyMaiPlugin):
            config = mod.LonelyMaiConfig()
            ctx = types.SimpleNamespace(
                logger=Mock(),
                maisaka=types.SimpleNamespace(
                    proactive=types.SimpleNamespace(
                        trigger=AsyncMock(return_value={"success": True})
                    )
                ),
            )

        self.bot = Bot()
        self.bot._wake_ready = True
        self.bot._bot_qq = "bot"
        self.bot.ctx.chat = types.SimpleNamespace(
            get_stream_by_group_id=AsyncMock(
                side_effect=lambda target: host_stream(target, True, session="group-chat")
            ),
            get_stream_by_user_id=AsyncMock(
                side_effect=lambda target: host_stream(target, False, session="chat")
            ),
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bot.ctx.paths = types.SimpleNamespace(data_dir=directory.name)
        self.bot.config.plugin.enabled = True
        self.bot.config.target.allowed_groups = ["target", "42"]
        self.bot.config.target.allowed_friends = ["target", "42"]
        self.state = mod.ChatStreamState("chat", "target")

    async def test_default_life_intent_uses_same_planner_path(self):
        self.assertTrue(await self.bot._do_proactive_chat(self.state, "target"))
        kw = self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs
        self.assertEqual(kw["stream_id"], "chat")
        self.assertEqual(kw["intent"], self.bot.config.intent.private_text)
        self.assertIn("优先分享有依据", kw["intent"])
        self.assertIn("自然续接未结束的话题", kw["intent"])
        self.assertIn("不自行补写", kw["intent"])
        self.assertIn("须有真实约定", kw["intent"])
        self.assertIn("早安", kw["intent"])
        self.assertIn("叫起床", kw["intent"])
        self.assertLess(len(kw["intent"]), 200)
        self.assertEqual(kw["reason"], "attention_proactive")
        self.assertEqual(
            kw["metadata"],
            {"source": "lonelymai", "target_id": "target", "chat_kind": "private"},
        )

    async def test_custom_intent_is_not_replaced_or_extended(self):
        self.bot.config.intent.private_text = "自定义意图"
        self.bot.config.intent.reason = "custom_reason"
        await self.bot._do_proactive_chat(self.state, "target")
        kw = self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs
        self.assertEqual(kw["intent"], "自定义意图")
        self.assertEqual(kw["reason"], "custom_reason")

    def test_template_uses_complete_default_and_stays_disabled(self):
        template = tomllib.loads((self.root / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(template["intent"]["private_text"], self.bot.config.intent.private_text)
        self.assertNotIn("text", template["intent"])
        self.assertNotIn("text", type(self.bot.config.intent).model_fields)
        self.assertFalse(template["plugin"]["enabled"])
        self.assertEqual(template["plugin"]["config_version"], "2.0.0")
        self.assertEqual(template["plugin"]["version"], self.bot.config.plugin.version)
        self.assertEqual(template["intent"]["group_text"], self.bot.config.intent.group_text)

    async def test_group_uses_own_prompt_without_asserting_a_cold_chat(self):
        self.bot.config.intent.private_text = "明确私聊定制"
        state = self.mod.ChatStreamState("group-chat", "target", is_group=True)
        await self.bot._do_proactive_chat(state, "target")
        kw = self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs
        self.assertEqual(kw["intent"], self.mod.GROUP_PROACTIVE_INTENT)
        self.assertNotIn("冷场了", kw["intent"])
        self.assertIn("没有合适内容可以不发", kw["intent"])
        self.assertEqual(kw["metadata"]["chat_kind"], "group")

    async def test_group_and_private_custom_prompts_select_independently(self):
        self.bot.ctx.chat.get_stream_by_group_id.side_effect = lambda target: host_stream(
            target, True, session="chat"
        )
        self.bot.config.intent.group_text = "群聊自定义"
        self.bot.config.intent.private_text = "私聊自定义"
        for group, expected in [(True, "群聊自定义"), (False, "私聊自定义")]:
            state = self.mod.ChatStreamState("chat", "target", is_group=group)
            await self.bot._do_proactive_chat(state, "target")
            self.assertEqual(
                self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs["intent"],
                expected,
            )

    def test_blank_group_does_not_fall_back_to_private_text(self):
        self.bot.config.intent.group_text = "  "
        self.bot.config.intent.private_text = "私聊新设置"
        state = self.mod.ChatStreamState("chat", "target", is_group=True)
        self.assertEqual(self.bot._intent_for(state), self.mod.GROUP_PROACTIVE_INTENT)
        self.bot.config.intent.private_text = "  "
        default = self.mod.LonelyMaiConfig().intent.private_text
        self.assertEqual(self.bot._intent_for(self.state), default)

    def test_unknown_legacy_text_is_not_kept_in_config(self):
        cfg = self.mod.LonelyMaiConfig.model_validate({"intent": {"text": "旧字段不应被保存"}})
        self.assertNotIn("text", cfg.model_dump()["intent"])
        self.assertFalse(hasattr(cfg.intent, "text"))

    def test_description_changes_do_not_change_scheduler_or_authorization_defaults(self):
        cfg = self.mod.LonelyMaiConfig()
        self.assertEqual(
            cfg.scheduler.model_dump(),
            {
                "enabled": True,
                "start_time": "08:00",
                "end_time": "23:00",
                "check_interval": 30,
                "jitter": 5,
                "probability": 0.2,
                "min_interval_between_chats": 60,
                "isolation_time": 15,
            },
        )
        self.assertEqual(cfg.target.model_dump(), {"allowed_groups": [], "allowed_friends": []})
        self.assertFalse(cfg.life.enabled)
        self.assertFalse(cfg.bilibili.enabled)

    def test_real_config_schema_explains_scope_and_non_delivery_semantics(self):
        schema = self.mod.LonelyMaiConfig.model_json_schema()
        fields = schema["$defs"]["SchedulerSection"]["properties"]
        self.assertIn("其他插件", fields["enabled"]["description"])
        self.assertIn("服务器本地时间", fields["start_time"]["description"])
        self.assertIn("检查等待", fields["jitter"]["description"])
        self.assertIn("不保证", fields["probability"]["description"])
        self.assertIn("宿主接受", fields["min_interval_between_chats"]["description"])

    async def test_equal_numeric_group_and_friend_ids_have_separate_states(self):
        self.bot.config.target.allowed_groups = ["42"]
        self.bot.config.target.allowed_friends = ["42"]
        self.bot.ctx.chat = types.SimpleNamespace(
            get_stream_by_group_id=AsyncMock(
                return_value={"stream": host_stream("42", True, session="group-chat")}
            ),
            get_stream_by_user_id=AsyncMock(
                return_value={"stream": host_stream("42", False, session="private-chat")}
            ),
        )
        await self.bot._init_target_streams()
        self.assertEqual(len(self.bot._states), 2)
        group = self.bot._states[(True, "42")]
        private = self.bot._states[(False, "42")]
        self.assertEqual((group.stream_id, group.is_group), ("group-chat", True))
        self.assertEqual((private.stream_id, private.is_group), ("private-chat", False))
        group.last_proactive_time = 123
        private.last_proactive_time = 456
        await self.bot._init_target_streams()
        self.assertEqual(self.bot._states[(True, "42")].last_proactive_time, 123)
        self.assertEqual(self.bot._states[(False, "42")].last_proactive_time, 456)

    async def test_manual_command_uses_matched_stream_kind(self):
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = lambda target: host_stream(
            target, False, session="private-chat"
        )
        self.bot.config.intent.group_text = "群聊手动"
        self.bot.config.intent.private_text = "私聊手动"
        self.bot._states = {
            (True, "42"): self.mod.ChatStreamState("group-chat", "42", is_group=True),
            (False, "42"): self.mod.ChatStreamState("private-chat", "42"),
        }
        for sid, expected in [("group-chat", "群聊手动"), ("private-chat", "私聊手动")]:
            result = await self.bot.cmd_trigger(stream_id=sid)
            self.assertTrue(result[0])
            self.assertEqual(
                self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs["intent"],
                expected,
            )

    async def test_trigger_exception_is_reported_as_failure(self):
        self.bot.ctx.maisaka.proactive.trigger.side_effect = RuntimeError("test")
        self.assertFalse(await self.bot._do_proactive_chat(self.state, "target"))

    async def test_only_explicit_host_success_is_success(self):
        for result in [
            {"success": False, "error": "rejected"},
            {},
            None,
            {"success": 1},
            {"success": "true"},
            {"result": {"success": True}},
            AsyncMock(),
        ]:
            with self.subTest(result=type(result).__name__):
                self.bot.ctx.maisaka.proactive.trigger.return_value = result
                self.assertFalse(await self.bot._do_proactive_chat(self.state, "target"))

    async def test_real_sdk_preserves_host_protocol(self):
        from maibot_sdk.capabilities.maisaka import MaisakaCapability

        context = types.SimpleNamespace(call_capability=AsyncMock())
        self.bot.ctx.maisaka = MaisakaCapability(context)
        for success in [False, True]:
            context.call_capability.return_value = {"success": success}
            self.assertIs(await self.bot._do_proactive_chat(self.state, "target"), success)
            self.assertEqual(context.call_capability.call_args.args, ("maisaka.proactive.trigger",))

    async def test_cached_manual_state_cannot_bypass_plugin_disable(self):
        self.bot._states = {(False, "target"): self.state}
        self.bot.config.plugin.enabled = False
        self.assertFalse((await self.bot.cmd_trigger(stream_id="chat"))[0])
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_cached_states_cannot_bypass_revoked_whitelists(self):
        for group in [True, False]:
            state = self.mod.ChatStreamState("chat", "target", is_group=group)
            self.bot._states = {(group, "target"): state}
            self.bot.config.target.allowed_groups = [] if group else ["target"]
            self.bot.config.target.allowed_friends = ["target"] if group else []
            self.assertFalse((await self.bot.cmd_trigger(stream_id="chat"))[0])
            self.assertFalse(await self.bot._do_proactive_chat(state, "target"))
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_manual_still_bypasses_scheduler_conditions(self):
        self.bot._states = {(False, "target"): self.state}
        self.bot.config.scheduler.enabled = False
        self.bot.config.scheduler.probability = 0
        self.state.last_proactive_time = 123
        self.assertTrue((await self.bot.cmd_trigger(stream_id="chat"))[0])
        self.assertEqual(self.state.last_proactive_time, 123)

    async def test_scheduled_rejection_does_not_consume_cooldown(self):
        cfg = self.bot.config.scheduler
        cfg.start_time, cfg.end_time = "00:00", "23:59"
        cfg.probability, cfg.isolation_time = 1, 0
        self.bot.ctx.maisaka.proactive.trigger.return_value = {"success": False}
        with patch.object(self.mod.random, "random", return_value=0):
            await self.bot._check_and_trigger(self.state, "target")
        self.assertEqual(self.state.last_proactive_time, 0)
        self.bot.ctx.maisaka.proactive.trigger.return_value = {"success": True}
        # Rejection keeps the durable 60s fence even though successful cooldown
        # stays empty. The old fixture bypassed it by never completing on_load.
        from datetime import datetime, timedelta

        later = datetime.now() + timedelta(seconds=61)
        with (
            patch.object(self.mod.random, "random", return_value=0),
            patch.object(self.mod, "datetime") as clock,
            patch.object(self.mod.time, "time", return_value=later.timestamp()),
        ):
            clock.now.return_value = later
            await self.bot._check_and_trigger(self.state, "target")
        self.assertGreater(self.state.last_proactive_time, 0)

    async def test_manual_host_rejection_reports_failure(self):
        self.bot._states = {(False, "target"): self.state}
        self.bot.ctx.maisaka.proactive.trigger.return_value = {"success": False}
        self.assertEqual(
            await self.bot.cmd_trigger(stream_id="chat"), (False, "主动发言失败", True)
        )

    async def test_revocation_during_life_query_prevents_host_dispatch(self):
        self.bot.config.life.enabled = True
        self.bot._life = types.SimpleNamespace(
            flush=AsyncMock(), claim=AsyncMock(), update=AsyncMock()
        )

        async def claim(_session):
            self.bot.config.target.allowed_friends = []
            return {"receipt_id": "a" * 32, "summary": "public"}

        self.bot._life.claim.side_effect = claim
        self.assertFalse(await self.bot._do_proactive_chat(self.state, "target"))
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.bot._life.update.assert_awaited_once_with("a" * 32, "released")

    async def test_life_host_rejection_and_unknown_settle_without_retry(self):
        self.bot.config.life.enabled = True
        self.bot._life = types.SimpleNamespace(
            flush=AsyncMock(),
            claim=AsyncMock(return_value={"receipt_id": "a" * 32, "summary": "public"}),
            update=AsyncMock(),
        )
        for result in [{"success": False}, None, {"success": True, "task_id": "bad"}]:
            self.bot.ctx.maisaka.proactive.trigger.reset_mock()
            self.bot.ctx.maisaka.proactive.trigger.return_value = result
            self.bot._life.update.reset_mock()
            await self.bot._do_proactive_chat(self.state, "target")
            self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
            expected = (
                "released"
                if isinstance(result, dict) and result.get("success") is False
                else "unknown"
            )
            self.bot._life.update.assert_awaited_once_with("a" * 32, expected, "")
