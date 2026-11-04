"""Nonempty discovery and zero-skip gate, including real SDK-backed tests."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")

    def ids(group):
        for test in group:
            if isinstance(test, unittest.TestSuite):
                yield from ids(test)
            else:
                yield test.id()

    discovered = set(ids(suite))
    for module in (
        "test_intent",
        "test_life_bridge",
        "test_lifecycle",
        "test_prompt_visibility",
        "test_bilibili_bridge",
        "test_wakes",
        "test_management",
        "test_chat_sdk",
        "test_repeat",
        "test_repeat_management",
        "test_plan_dispatch",
        "test_dayplan",
        "test_simple_plans",
        "test_multi_wakes",
        "test_admission",
    ):
        if not any(name.startswith(module + ".") for name in discovered):
            raise SystemExit("Required proactive test group is empty: " + module)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.skipped:
        raise SystemExit("Tests failed or skipped; SDK tests must actually execute")


if __name__ == "__main__":
    main()
