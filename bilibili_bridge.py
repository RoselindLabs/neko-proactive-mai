"""Signed, at-most-once reading handoffs; never claim confirmed QQ delivery."""

from .life_bridge import LifeBridge


class BilibiliBridge(LifeBridge):
    terminal_statuses = {"shown", "released", "unavailable"}
    accepted_statuses = {"shown", "released", "unavailable", "unknown", "submitted", "prepared"}

    def __init__(self, context):
        super().__init__(
            context,
            provider="local.bilibili-autopilot",
            prefix="bilibili",
            api_name="local.bilibili-autopilot.discovery_v1",
        )
