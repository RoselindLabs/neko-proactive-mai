import asyncio
import importlib.util
import pathlib
import sys
import types
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock


@unittest.skipUnless(importlib.util.find_spec("maibot_sdk"), "requires MaiBot SDK")
class Lifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_hot_enable_disable_and_repeated_update(self):
        root = pathlib.Path(__file__).parent
        pkg = types.ModuleType("lonely_test")
        pkg.__path__ = [str(root)]
        sys.modules[pkg.__name__] = pkg
        spec = importlib.util.spec_from_file_location("lonely_test.plugin", root / "plugin.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        class TestBot(mod.LonelyMaiPlugin):
            config = mod.LonelyMaiConfig()
            ctx = types.SimpleNamespace(logger=Mock())

        bot = object.__new__(TestBot)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        bot.ctx.paths = types.SimpleNamespace(data_dir=directory.name)
        bot._wakes = None
        bot._life = None
        bot._wake_ready = False
        bot.config.plugin.enabled = False
        bot._scheduler_task = None
        bot._lifecycle_lock = asyncio.Lock()
        bot._get_global_str = AsyncMock(return_value="bot")
        bot._states = {"chat": types.SimpleNamespace(last_proactive_time=123)}

        async def loop():
            await asyncio.Event().wait()

        bot._schedule_loop = loop
        await bot.on_load()
        self.assertIsNone(bot._scheduler_task)
        bot.config.plugin.enabled = True
        await bot.on_config_update()
        first = bot._scheduler_task
        self.assertIsNotNone(first)
        await bot.on_config_update()
        self.assertTrue(first.done())
        self.assertEqual(bot._states["chat"].last_proactive_time, 123)
        second = bot._scheduler_task
        bot.config.scheduler.enabled = False
        await bot.on_config_update()
        self.assertTrue(second.done())
        self.assertIsNone(bot._scheduler_task)
        await bot.on_unload()
