"""Opt-in offline real maintained Host context/discovery and plan integration."""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace, ModuleType

ROOT = Path(__file__).resolve().parents[1]


async def verify(host, legacy_ref):
    from maibot_sdk.components import collect_components
    from src.maisaka.reasoning_engine import MaisakaReasoningEngine
    from src.maisaka.focus.tools import execution_context
    from src.maisaka.runtime import MaisakaHeartFlowChatting
    from src.plugin_runtime.component_query import ComponentQueryService
    from src.plugin_runtime.host.component_registry import ToolEntry
    from src.core.tooling import ToolInvocation, ToolSpec
    from src.common.database.database import engine
    from src.common.logger import close_handlers

    sys.path.insert(0, str(ROOT))
    from test_telegram_plans import TelegramPlanTests
    from test_wakes import NOW
    from test_prompt_visibility import item, task

    hashes, cases, matches = {}, [], {}
    for method in (
        MaisakaReasoningEngine._build_tool_execution_context,
        execution_context,
        ComponentQueryService._build_tool_invocation_payload,
        MaisakaHeartFlowChatting.search_deferred_tool_specs,
    ):
        path = Path(method.__code__.co_filename).resolve()
        assert path.is_relative_to(host), "Unexpected Host checkout"
        hashes[str(path.relative_to(host))] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        for mode in ("normal", "focus"):
            for platform in ("qq", "telegram"):
                for repeated in (False, True):
                    fixture = TelegramPlanTests()
                    fixture.setUp()
                    try:
                        await fixture.asyncSetUp()
                        bot = fixture.bot
                        entries = {
                            c["name"]: ToolEntry(c["name"], "TOOL", "neko.proactive", c["metadata"])
                            for c in collect_components(bot)
                            if c["type"] == "TOOL"
                        }
                        specs = {
                            name: ToolSpec(
                                name=name,
                                description=entry.description,
                                parameters_schema=entry._get_parameters_schema(),
                            )
                            for name, entry in entries.items()
                        }
                        search_runtime = SimpleNamespace(
                            deferred_tool_specs_by_name=specs, discovered_tool_names=set()
                        )
                        for query in ("预约", "重复预约", "每天", "每隔N天"):
                            found = MaisakaHeartFlowChatting.search_deferred_tool_specs(
                                search_runtime, query, limit=5
                            )
                            matches[query] = [spec.name for spec in found]
                            assert "neko_proactive_repeat" in matches[query], matches
                            assert all(
                                spec.to_llm_definition()["description"]
                                == entries[spec.name].description
                                for spec in found
                            )
                        reminder = MaisakaHeartFlowChatting.build_deferred_tools_reminder(
                            search_runtime
                        )
                        assert "每隔N天" in reminder and "2880" in reminder
                        session = "qq-123" if platform == "qq" else "tg-123"
                        runtime = SimpleNamespace(
                            session_id=session,
                            chat_stream=SimpleNamespace(
                                platform=platform,
                                group_id="",
                                user_id="123",
                                is_group_session=False,
                            ),
                        )
                        context = (
                            MaisakaReasoningEngine(runtime)._build_tool_execution_context("offline")
                            if mode == "normal"
                            else execution_context(runtime, "offline")
                        )
                        name = "neko_proactive_repeat" if repeated else "neko_proactive_wake"
                        handler = bot.private_repeat if repeated else bot.private_wake
                        business = (
                            dict(action="set", interval_minutes=5, intent="离线真实Host计划依据")
                            if repeated
                            else dict(action="add", at="10分钟后", intent="离线真实Host计划依据")
                        )
                        invocation = ToolInvocation(
                            tool_name=name,
                            arguments={
                                **business,
                                "_host_context": fixture.envelope("telegram", account_id="902"),
                                "platform": "fake",
                                "stream_id": "forged",
                                "user_id": "999",
                                "account_id": "902",
                            },
                        )
                        payload = ComponentQueryService._build_tool_invocation_payload(
                            invocation, context, entry=entries[name]
                        )
                        assert payload["_host_context"]["account_id"] == ""
                        assert payload["_host_context"]["session_id"] == session
                        assert (await handler(**payload))["success"]
                        missing = ComponentQueryService._build_tool_invocation_payload(
                            invocation, None, entry=entries[name]
                        )
                        assert not (await handler(**missing))["success"]

                        original_rpc = bot.ctx._rpc_call

                        async def rpc(method, plugin_id, request, **kwargs):
                            if request["capability"] != "maisaka.proactive.trigger":
                                return await original_rpc(method, plugin_id, request, **kwargs)
                            args = request["args"]
                            assert args["stream_id"] == session
                            assert not bot._wake_lock.locked()
                            text = task(1793505900000).replace(
                                '{"source": "lonelymai"}', json.dumps(args["metadata"])
                            )
                            hook = await bot.filter_planner_tasks(
                                session_id=session, items=[item(text)]
                            )
                            assert "离线真实Host计划依据" in json.dumps(hook, ensure_ascii=False)
                            assert not await bot._wake_prompt(
                                dict(session_id=session, items=[item(text)])
                            )
                            fixture.sent.append(args)
                            return {
                                "success": True,
                                "task_id": "proactive:neko.proactive:1793505900000",
                            }

                        bot.ctx._rpc_call = rpc
                        fixture.clock_mock.return_value = NOW + (300 if repeated else 600)
                        await bot._run_wakes()
                        assert len(fixture.sent) == 1
                        key = fixture.KEY if platform == "telegram" else "123"
                        record = (
                            fixture.data()["repeats"][key]["last"]
                            if repeated
                            else fixture.data()["slots"][key]
                        )
                        assert record["status"] == "submitted" and record["shown"]
                        assert record["planner_requested_at"] == fixture.clock_mock.return_value
                        cases.append(f"{mode}/{platform}/{'repeat' if repeated else 'single'}")
                    finally:
                        fixture.doCleanups()
        legacy = None
        if legacy_ref:
            fixture = TelegramPlanTests()
            fixture.setUp()
            try:
                await fixture.asyncSetUp()
                source = subprocess.check_output(
                    ["git", "show", f"{legacy_ref}:wake_store.py"], cwd=ROOT
                )
                module = ModuleType("wake_test.legacy_store")
                module.__package__ = "wake_test"
                exec(compile(source, f"{legacy_ref}:wake_store.py", "exec"), module.__dict__)
                assert (await fixture.add("qq"))["success"]
                old = module.WakeStore(fixture.directory.name)
                old.read()
                original = fixture.data()
                assert (await fixture.add())["success"]
                before = fixture.bot._get_wakes().path.read_bytes()
                for operation in (old.read, lambda: old.recover(NOW), lambda: old.write(original)):
                    try:
                        operation()
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Old reader unexpectedly accepted v5")
                    assert fixture.bot._get_wakes().path.read_bytes() == before
                legacy = dict(
                    ref=legacy_ref,
                    sha256=hashlib.sha256(source).hexdigest(),
                    v4="readable",
                    v5="read/recover/write refused; bytes preserved",
                    rollback="v5-aware patch required; bare old code unsafe",
                )
            finally:
                fixture.doCleanups()
        return dict(
            sdk=importlib.metadata.version("maibot-plugin-sdk"),
            cases=cases,
            search=matches,
            host_sources=hashes,
            legacy=legacy,
            plugin_sha256=hashlib.sha256((ROOT / "plugin.py").read_bytes()).hexdigest(),
        )
    finally:
        engine.dispose()
        close_handlers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-source", type=Path, required=True)
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--legacy-ref")
    args = parser.parse_args()
    host = args.host_source.resolve()
    report = args.report.resolve() if args.report else None
    with ExitStack() as cleanup:
        directory = cleanup.enter_context(
            TemporaryDirectory(prefix="proactive-host-", dir=args.temp_dir)
        )
        instance = Path(directory).resolve()
        old_cwd = Path.cwd()
        os.chdir(instance)
        cleanup.callback(os.chdir, old_cwd)
        os.environ.update(
            MAIBOT_INSTANCE_DIR=str(instance),
            MAIBOT_LOCALE="zh-CN",
            MAIBOT_RUNTIME_CONTEXT=json.dumps(
                dict(
                    bot_id="0" * 32,
                    http_endpoint=str(instance / "synthetic.sock"),
                    public_base_url="http://synthetic.invalid",
                    status_path=str(instance / "worker-status.json"),
                )
            ),
        )
        sys.path[:0] = [str(host / "scripts"), str(host)]
        import neko_pytest_guard  # noqa: F401 -- denies all network before Host imports

        result = asyncio.run(verify(host, args.legacy_ref))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if report:
        report.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
