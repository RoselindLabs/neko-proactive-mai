"""Neko Proactive Mai 配置模型。"""

from __future__ import annotations

from typing import List

from maibot_sdk import Field, PluginConfigBase


GROUP_PROACTIVE_INTENT = (
    "想和群友聊两句。结合当前对话、记忆和本次可用素材，选择一个自然话头；"
    "正在聊就顺着接，没有合适内容可以不发。不重复刚聊过的话，不必反问。"
)


PRIVATE_PROACTIVE_INTENT = (
    "想找对方聊两句，优先分享有依据的见闻、感受或想法，也可以自然续接未结束的话题。"
    "吃饭、天气、行程等具体经历只依据已知信息，不自行补写。"
    "早晨可说早安；叫起床须有真实约定。"
    "正在聊就顺着接，不机械查岗，不重复追问已回答的事。"
    "没合适话头可以不发，不必以问题收尾。"
)


class PluginSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="2.0.0", description="配置文件版本号")
    version: str = Field(default="2.10.0", description="插件版本，由维护流程更新，无需手改")


class SchedulerSection(PluginConfigBase):
    """调度器设置——控制何时、多久触发一次主动注入。"""

    enabled: bool = Field(
        default=True,
        description="本插件基础检查、单次和重复计划的总暂停开关；不控制其他插件，手动/saymai另行处理",
    )
    start_time: str = Field(
        default="08:00",
        description="仅基础检查的每日开始时间HH:MM，按服务器本地时间；不限制明确计划，支持跨午夜",
    )
    end_time: str = Field(
        default="23:00",
        description="自动检查的每日结束时间HH:MM，按服务器本地时间；与开始相同不是全天",
    )
    check_interval: int = Field(
        default=30, description="每轮检查完成后等待的分钟数，不是固定发言间隔；初次启动可立即检查"
    )
    jitter: int = Field(
        default=5,
        description="检查等待随机±多少分钟；45配5表示40～50分钟，最短等1分钟，不延迟已生成正文",
    )
    probability: float = Field(
        default=0.2,
        description="每个目标满足时段、冷却和静默条件后的触发概率0～1；1也不保证模型发言或QQ送达",
    )
    min_interval_between_chats: int = Field(
        default=60,
        description="同一目标两次基础任务被宿主接受的最小间隔（分钟），不是QQ发送间隔；仅基础冷却且只在内存，不限制单次/重复计划；计划另有持久60秒安全间距",
    )
    isolation_time: int = Field(
        default=15,
        description="距最近真人消息至少静默多少分钟，0为不要求；只管本插件自动检查，不限制其他插件",
    )


class TargetSection(PluginConfigBase):
    """白名单——只对指定的群/用户生效。"""

    allowed_groups: List[str] = Field(
        default_factory=list,
        description="本插件可主动联系的QQ群号；不是全局白名单，B站素材另须双方群授权交集",
    )
    allowed_friends: List[str] = Field(
        default_factory=list,
        description="本插件可主动联系的好友QQ号；不自动授予B站或其他插件私聊权限",
    )
    telegram_account_id: str = Field(
        default="", description="Telegram机器人数字账号ID；与独立私聊名单一起授权单次/重复计划"
    )
    allowed_telegram_user_ids: List[str] = Field(
        default_factory=list,
        description="Telegram私聊用户数字ID，默认空；仅授权单次/重复计划，不启用基础主动或群/话题日程",
    )


class LogSection(PluginConfigBase):
    verbose: bool = Field(
        default=True, description="兼容保留项，当前未被日志逻辑使用；不会改变频率或消息内容"
    )
    scheduler_log: bool = Field(
        default=True, description="显示下次检查时间与时段、冷却、静默期、概率等跳过原因；不改变调度"
    )


class IntentSection(PluginConfigBase):
    """主动触发时注入 Planner 的意图描述。"""

    group_text: str = Field(
        default=GROUP_PROACTIVE_INTENT,
        description="群聊Planner意图，不是直接发送文案，也不预设已经冷场；空白回退群聊默认，不沿用私聊意图",
    )
    private_text: str = Field(
        default=PRIVATE_PROACTIVE_INTENT,
        description="私聊Planner意图：有依据的见闻/想法与自然续聊，不补写日常；叫起床须有真实约定，空白回退私聊默认",
    )
    reason: str = Field(
        default="attention_proactive", description="内部触发原因标识，不是聊天正文，通常保持原值"
    )


class LifeSection(PluginConfigBase):
    enabled: bool = Field(
        default=False,
        description="旧Life素材桥；Life已停用的部署保持关闭，不用于重新启用梦境/日记；失败仍可普通聊天",
    )


class BilibiliSection(PluginConfigBase):
    enabled: bool = Field(
        default=False,
        description="领取B站阅读手记作为本次主动聊天可选素材；按当前聊天判断是否想分享。双方须允许同一群或私聊，资料提供不等于已发QQ。",
    )


class LonelyMaiConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    scheduler: SchedulerSection = Field(default_factory=SchedulerSection)
    target: TargetSection = Field(default_factory=TargetSection)
    log: LogSection = Field(default_factory=LogSection)
    intent: IntentSection = Field(default_factory=IntentSection)
    life: LifeSection = Field(default_factory=LifeSection)
    bilibili: BilibiliSection = Field(default_factory=BilibiliSection)
