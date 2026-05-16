"""主动私聊插件。

设计哲学:
    - 插件不做决策。所有"要不要找、找谁、为什么找、聊什么、什么语气"
      全部交给 MaiSaka 的 planner + replyer,基于人设、记忆和当前情境自行判断。
    - 插件只做三件事:
        1) 在不规律的时机"叫醒"MaiSaka 思考。
        2) 采集"外部世界"信号(时段语义/节日/今日热点/天气)。
        3) 把对方身份、原始情境、外部世界打包成 intent,投递给
           maisaka.proactive.trigger。
    - 配置只暴露防呆参数和事实型偏好(白名单/身份/城市),不暴露策略参数。

数据流:
    on_load → 启动后台 _wakeup_loop
        ↓ 不规律睡眠
    _do_one_wakeup_sweep
        ↓ chat.get_private_streams + 白名单过滤
        ↓ 对每个允许私聊
    _try_wakeup_one → 构造情境快照(本地信号 + 外部世界)
        ↓ call_capability("maisaka.proactive.trigger", intent=..., metadata={...})
    MaiSaka 主循环收到任务,planner 思考,自行决定是否走 reply 工具
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

import asyncio
import json
import random
import time

import httpx

_STATE_FILE_NAME: Final[str] = "state.json"
_WEEKDAY_NAMES: Final[tuple[str, ...]] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 公历节日表:(月, 日) -> 节日名称。不含农历节日(避免引依赖)。
_FESTIVALS: Final[dict[tuple[int, int], str]] = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (3, 12): "植树节",
    (4, 1): "愚人节",
    (5, 1): "劳动节",
    (5, 4): "青年节",
    (6, 1): "儿童节",
    (7, 1): "建党节",
    (8, 1): "建军节",
    (9, 10): "教师节",
    (10, 1): "国庆节",
    (10, 31): "万圣节",
    (11, 11): "光棍节",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
}

_HOT_SOURCE_PRESETS: Final[dict[str, str]] = {
    "60s": "https://60s.viki.moe/v2/60s",
    "weibo": "https://api.98dou.cn/api/hotlist?type=weibo",
    "baidu": "https://api.98dou.cn/api/hotlist?type=baidu",
    "zhihu": "https://api.98dou.cn/api/hotlist?type=zhihu",
    "douyin": "https://api.98dou.cn/api/hotlist?type=douyin",
}


# ============================ 配置模型 ============================


class PluginSectionConfig(PluginConfigBase):
    """插件元信息段。"""

    config_version: str = Field(default="0.2.0", description="配置文件版本")
    enabled: bool = Field(default=True, description="是否启用插件")


class WakeupSectionConfig(PluginConfigBase):
    """唤醒循环防呆参数。"""

    min_interval_minutes: int = Field(default=60, description="唤醒循环最小间隔(分钟)")
    max_interval_minutes: int = Field(default=180, description="唤醒循环最大间隔(分钟)")
    daily_wakeup_cap: int = Field(default=24, description="全局每日唤醒次数硬上限")
    per_chat_min_gap_hours: int = Field(default=6, description="单聊两次唤醒最少冷却(小时)")
    startup_delay_seconds: int = Field(default=60, description="插件启动后首次唤醒前的等待秒数")


class PlatformsSectionConfig(PluginConfigBase):
    """平台过滤。"""

    include: list[str] = Field(default_factory=lambda: ["qq"], description="纳入主动聊天的平台列表")


class HistorySectionConfig(PluginConfigBase):
    """历史拉取范围。"""

    lookback_days: int = Field(default=30, description="情境快照回溯天数")
    preview_message_count: int = Field(default=5, description="情境快照中最多保留多少条最新消息")


class WhitelistEntry(PluginConfigBase):
    """单个白名单条目。"""

    platform: str = Field(default="qq", description="对方所在平台,如 qq")
    user_id: str = Field(default="", description="对方账号 ID")
    identity: str = Field(
        default="",
        description="对方在你这里登记的身份(如 哥哥/大学同学小明),会作为已知事实注入 intent",
    )


class WhitelistSectionConfig(PluginConfigBase):
    """白名单及对方身份。"""

    mode: str = Field(
        default="strict",
        description='白名单模式: "strict" 仅主动找 entries 内的对象; "off" 不启用白名单',
    )
    entries: list[WhitelistEntry] = Field(default_factory=list, description="白名单条目数组")


class ExternalWorldSectionConfig(PluginConfigBase):
    """外部世界信号采集开关与参数。"""

    enable_time_semantics: bool = Field(default=True, description="是否注入时段语义")
    enable_festival: bool = Field(default=True, description="是否注入公历节日识别")

    enable_hot_topics: bool = Field(default=True, description="是否注入今日热点")
    hot_topics_source: str = Field(
        default="60s",
        description='热点源: "60s" / "weibo" / "baidu" / "zhihu" / "douyin" / "custom"',
    )
    hot_topics_custom_url: str = Field(default="", description="自定义热点 URL")
    hot_topics_timeout_seconds: int = Field(default=6, description="热点 HTTP 超时(秒)")
    hot_topics_cache_minutes: int = Field(default=60, description="热点缓存分钟数")
    hot_topics_max_items: int = Field(default=5, description="给 MaiSaka 看几条热点")

    enable_weather: bool = Field(default=False, description="是否注入天气(wttr.in)")
    weather_city: str = Field(default="", description="天气查询城市")
    weather_timeout_seconds: int = Field(default=5, description="天气 HTTP 超时(秒)")
    weather_cache_minutes: int = Field(default=60, description="天气缓存分钟数")


class ProactiveChatConfig(PluginConfigBase):
    """主动私聊插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    wakeup: WakeupSectionConfig = Field(default_factory=WakeupSectionConfig)
    platforms: PlatformsSectionConfig = Field(default_factory=PlatformsSectionConfig)
    history: HistorySectionConfig = Field(default_factory=HistorySectionConfig)
    whitelist: WhitelistSectionConfig = Field(default_factory=WhitelistSectionConfig)
    external_world: ExternalWorldSectionConfig = Field(default_factory=ExternalWorldSectionConfig)


# ============================ 插件主体 ============================


class ProactiveChatPlugin(MaiBotPlugin):
    """主动私聊插件。"""

    config_model = ProactiveChatConfig

    def __init__(self) -> None:
        """初始化插件实例。"""
        super().__init__()
        self._wakeup_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._state_path = Path(__file__).with_name(_STATE_FILE_NAME)
        # 状态: stream_id -> 上次主动唤醒成功的 unix 秒
        self._last_wakeup_at: dict[str, float] = {}
        # 当日(YYYY-MM-DD)的唤醒计数,跨天自动重置
        self._wakeup_day: str = ""
        self._wakeup_count_today: int = 0
        # 外部世界缓存: (过期 unix 秒, 数据 str|list)
        self._hot_topics_cache: tuple[float, list[str]] | None = None
        self._weather_cache: tuple[float, str] | None = None

    # ----------------------------- 生命周期 -----------------------------

    async def on_load(self) -> None:
        """加载状态并启动后台唤醒循环。"""
        await self._load_state()
        if self.config.plugin.enabled:
            self._wakeup_task = asyncio.create_task(self._wakeup_loop())
            self.ctx.logger.info(
                f"主动私聊插件已启动 "
                f"白名单模式={self.config.whitelist.mode} "
                f"白名单条目={len(self.config.whitelist.entries)}"
            )
        else:
            self.ctx.logger.info("主动私聊插件被配置为关闭,跳过启动")

    async def on_unload(self) -> None:
        """停止后台任务并落盘状态。"""
        if self._wakeup_task is not None:
            self._wakeup_task.cancel()
            try:
                await self._wakeup_task
            except asyncio.CancelledError:
                pass
            self._wakeup_task = None
        await self._save_state()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置变更时由 SDK 自动重新注入 self.config,这里只清缓存。"""
        del scope
        del config_data
        del version
        # 外部世界配置可能变化(如城市/源切换),清掉缓存重新拉
        self._hot_topics_cache = None
        self._weather_cache = None

    # ----------------------------- 命令:测试触发 -----------------------------

    @Command(
        "proactive_chat_test",
        description="立即触发一次对当前私聊对象的主动唤醒(开发测试用)",
        pattern=r"^/(?:proactive|主动)(?:[_\s-]?test|测试)\s*$",
    )
    async def handle_test_command(
        self,
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        platform: str = "qq",
        **kwargs: Any,
    ) -> tuple[bool, str | None, int]:
        """开发测试命令:对当前私聊对象立即触发一次主动唤醒,绕过白名单/冷却/概率/上限。

        命令格式: /proactive test  或  /主动测试
        仅限私聊场景使用。
        """
        del kwargs

        if not stream_id:
            return False, "缺少 stream_id", 1
        if group_id:
            await self.ctx.send.text("/proactive test 仅限私聊使用。", stream_id)
            return False, "/proactive test 仅限私聊", 1
        if not user_id:
            await self.ctx.send.text("/proactive test 无法解析对方 user_id,放弃执行。", stream_id)
            return False, "缺少 user_id", 1

        stream = {
            "stream_id": stream_id,
            "platform": platform,
            "user_id": user_id,
        }
        self.ctx.logger.info(
            f"[test] 手动触发主动私聊 stream={stream_id} user={user_id} platform={platform}"
        )
        await self._try_wakeup_one(stream, force=True)
        return True, "proactive chat test triggered", 1

    # ----------------------------- 白名单解析 -----------------------------

    def _whitelist_lookup(self, platform: str, user_id: str) -> WhitelistEntry | None:
        """在白名单 entries 里查找匹配项,返回条目或 None。"""
        if not user_id:
            return None
        normalized_platform = platform.strip().lower()
        normalized_user_id = user_id.strip()
        for entry in self.config.whitelist.entries:
            if (
                entry.platform.strip().lower() == normalized_platform
                and entry.user_id.strip() == normalized_user_id
            ):
                return entry
        return None

    # ----------------------------- 唤醒主循环 -----------------------------

    async def _wakeup_loop(self) -> None:
        """后台不规律唤醒主循环。"""
        try:
            await asyncio.sleep(max(0, int(self.config.wakeup.startup_delay_seconds)))
        except asyncio.CancelledError:
            return

        while True:
            try:
                interval_seconds = self._random_interval_seconds()
                await asyncio.sleep(interval_seconds)
                await self._do_one_wakeup_sweep()
            except asyncio.CancelledError:
                break
            except Exception:
                self.ctx.logger.exception("主动唤醒循环异常,1 分钟后继续")
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    break

    def _random_interval_seconds(self) -> float:
        """在配置区间内均匀随机一个睡眠秒数,模拟"念头随机冒出"。"""
        wakeup_cfg = self.config.wakeup
        lo_minutes = max(1, int(wakeup_cfg.min_interval_minutes))
        hi_minutes = max(lo_minutes, int(wakeup_cfg.max_interval_minutes))
        return random.uniform(lo_minutes * 60, hi_minutes * 60)

    # ----------------------------- 单次扫描 -----------------------------

    async def _do_one_wakeup_sweep(self) -> None:
        """对所有可触发的私聊执行一次唤醒检查。"""
        self._maybe_rollover_day()
        wakeup_cfg = self.config.wakeup
        if self._wakeup_count_today >= int(wakeup_cfg.daily_wakeup_cap):
            return

        streams = await self._collect_candidate_streams()
        random.shuffle(streams)
        for stream in streams:
            if self._wakeup_count_today >= int(wakeup_cfg.daily_wakeup_cap):
                return
            await self._try_wakeup_one(stream)

    async def _collect_candidate_streams(self) -> list[dict[str, Any]]:
        """收集本轮待评估的私聊流。

        strict 模式:直接按白名单 entries 解析或主动创建 session,
                     这样白名单里"没和 bot 聊过"的人也能被主动找到。
        off 模式: 扫所有已存在的私聊 session(无白名单可遍历)。
        """
        whitelist_mode = self.config.whitelist.mode.strip().lower()
        if whitelist_mode == "strict":
            return await self._collect_from_whitelist()
        return await self._collect_from_existing_sessions()

    async def _collect_from_whitelist(self) -> list[dict[str, Any]]:
        """按白名单逐条解析私聊流;不存在的会调 chat.open_session 主动建立。"""
        included_platforms = {p.strip() for p in self.config.platforms.include if p.strip()}
        candidates: list[dict[str, Any]] = []
        for entry in self.config.whitelist.entries:
            platform = entry.platform.strip()
            user_id = entry.user_id.strip()
            if not platform or not user_id:
                continue
            if platform not in included_platforms:
                continue
            stream = await self._resolve_or_open_private_session(platform, user_id)
            if stream is not None:
                candidates.append(stream)
        return candidates

    async def _collect_from_existing_sessions(self) -> list[dict[str, Any]]:
        """off 模式下,直接拉所有已有私聊 session。"""
        candidates: list[dict[str, Any]] = []
        for platform in self.config.platforms.include:
            normalized = str(platform or "").strip()
            if not normalized:
                continue
            candidates.extend(await self._fetch_private_streams(normalized))
        return candidates

    async def _resolve_or_open_private_session(
        self,
        platform: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """为白名单条目解析对应的私聊 stream;若不存在则尝试主动创建。"""
        # 先看现有 session
        try:
            existing = await self.ctx.chat.get_stream_by_user_id(
                user_id=user_id,
                platform=platform,
            )
        except Exception:
            self.ctx.logger.exception(
                f"查询私聊 stream 异常 platform={platform} user_id={user_id}"
            )
            existing = None
        if isinstance(existing, dict) and str(existing.get("stream_id") or "").strip():
            return existing

        # 没有就主动 open。注意 chat.open_session 没在 SDK 解包表里,
        # 返回完整 dict {success, created, stream, ...}
        try:
            result = await self.ctx.call_capability(
                "chat.open_session",
                platform=platform,
                user_id=user_id,
                chat_type="private",
            )
        except Exception:
            self.ctx.logger.exception(
                f"chat.open_session 异常 platform={platform} user_id={user_id}"
            )
            return None

        if isinstance(result, dict) and bool(result.get("success")):
            stream = result.get("stream")
            if isinstance(stream, dict) and str(stream.get("stream_id") or "").strip():
                if result.get("created"):
                    self.ctx.logger.info(
                        f"为白名单条目新建私聊 session: platform={platform} "
                        f"user_id={user_id} stream_id={stream.get('stream_id')}"
                    )
                return stream
        self.ctx.logger.warning(
            f"chat.open_session 未返回有效 stream platform={platform} "
            f"user_id={user_id} result={result!r}"
        )
        return None

    async def _fetch_private_streams(self, platform: str) -> list[dict[str, Any]]:
        """拉取指定平台的所有私聊流(off 模式使用)。"""
        try:
            streams = await self.ctx.chat.get_private_streams(platform=platform)
        except Exception:
            self.ctx.logger.exception(f"拉取私聊列表失败 platform={platform}")
            return []
        if isinstance(streams, list):
            return [s for s in streams if isinstance(s, dict)]
        return []

    # ----------------------------- 单聊唤醒 -----------------------------

    async def _try_wakeup_one(self, stream: dict[str, Any], *, force: bool = False) -> None:
        """对单个私聊判断是否唤醒,以及实际投递。

        force=True 时跳过白名单 strict / 单聊冷却 / 软概率 / 每日上限,
        但仍会构造完整 intent 并记录 last_wakeup_at + 计数。用于 /test 命令。
        """
        stream_id = str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        if not stream_id:
            return

        # 白名单查找始终执行 —— 即便 force,也要拿到 identity 注入 intent
        platform = str(stream.get("platform") or "").strip()
        user_id = str(stream.get("user_id") or "").strip()
        whitelist_entry = self._whitelist_lookup(platform, user_id)

        wakeup_cfg = self.config.wakeup
        now_ts = time.time()
        last_at = self._last_wakeup_at.get(stream_id, 0.0)

        if not force:
            # 白名单 strict 过滤
            whitelist_mode = self.config.whitelist.mode.strip().lower()
            if whitelist_mode == "strict" and whitelist_entry is None:
                return

            # 单聊冷却
            gap_seconds = max(0, int(wakeup_cfg.per_chat_min_gap_hours)) * 3600
            if (now_ts - last_at) < gap_seconds:
                return

            # 软概率:距上次主动越久,触发概率越大,但永远不接近 1。
            # 单调连续函数,不分支不写死阈值,保留"想了但没动"的真人感。
            elapsed_ratio = (now_ts - last_at) / max(1.0, float(gap_seconds))
            soft_probability = min(0.6, 0.15 + 0.15 * elapsed_ratio)
            if random.random() > soft_probability:
                return

        snapshot = await self._build_context_snapshot(stream_id, stream)
        intent_text = self._build_intent_text(stream, whitelist_entry, snapshot)

        try:
            result = await self.ctx.call_capability(
                "maisaka.proactive.trigger",
                stream_id=stream_id,
                intent=intent_text,
                reason=(
                    "主动私聊插件被开发者手动测试触发,由你自行判断是否真的要发起"
                    if force
                    else "主动私聊插件唤醒,由你自行判断是否真的要发起"
                ),
                priority="normal",
                metadata={
                    "plugin": "proactive_chat_plugin",
                    "snapshot": snapshot,
                    "whitelist_identity": whitelist_entry.identity if whitelist_entry else "",
                    "force_triggered": force,
                },
            )
        except Exception:
            self.ctx.logger.exception(f"投递 maisaka.proactive.trigger 异常 stream={stream_id}")
            return

        if isinstance(result, dict) and bool(result.get("success")):
            self._last_wakeup_at[stream_id] = now_ts
            self._wakeup_count_today += 1
            await self._save_state()
            self.ctx.logger.info(
                f"已投递主动唤醒任务{' [FORCE]' if force else ''} stream={stream_id} "
                f"identity={whitelist_entry.identity if whitelist_entry else '(未登记)'} "
                f"silence_hours={snapshot.get('silence_hours')} "
                f"today={self._wakeup_count_today}/{int(wakeup_cfg.daily_wakeup_cap)}"
            )
        else:
            self.ctx.logger.warning(f"主动唤醒投递未成功 stream={stream_id} result={result!r}")

    # ----------------------------- 情境快照 -----------------------------

    async def _build_context_snapshot(self, stream_id: str, stream: dict[str, Any]) -> dict[str, Any]:
        """构造一份原始事实情境快照。不做动机解释,只摆事实。"""
        now_dt = datetime.now()
        snapshot: dict[str, Any] = {
            "now": now_dt.isoformat(timespec="seconds"),
            "weekday": _WEEKDAY_NAMES[now_dt.weekday()],
            "silence_hours": None,
            "recent_messages": [],
            # 对方最近一条真实用户消息的 message_id。用于绕过主程序 proactive task
            # 的 anchor 缺失 bug:LLM 看到这个 id 就能正常调 reply 工具,而不会把
            # 不可用的 task_id 当成 msg_id 填进去。
            "latest_user_msg_id": "",
            "time_semantics": "",
            "festival": "",
            "hot_topics": [],
            "weather": "",
        }

        # 最近聊天回顾
        await self._fill_recent_messages(snapshot, stream_id, stream, now_dt)

        # 外部世界各信号(失败静默,不影响主链路)
        ext_cfg = self.config.external_world
        if ext_cfg.enable_time_semantics:
            snapshot["time_semantics"] = self._build_time_semantics(now_dt)
        if ext_cfg.enable_festival:
            snapshot["festival"] = self._build_festival_context(now_dt.date())
        if ext_cfg.enable_hot_topics:
            snapshot["hot_topics"] = await self._fetch_hot_topics()
        if ext_cfg.enable_weather and ext_cfg.weather_city.strip():
            snapshot["weather"] = await self._fetch_weather(ext_cfg.weather_city.strip())

        return snapshot

    async def _fill_recent_messages(
        self,
        snapshot: dict[str, Any],
        stream_id: str,
        stream: dict[str, Any],
        now_dt: datetime,
    ) -> None:
        """拉最近 N 条消息,算 silence_hours、回顾末尾、对方最新 msg_id。"""
        history_cfg = self.config.history
        end_ts = now_dt.timestamp()
        start_ts = end_ts - max(1, int(history_cfg.lookback_days)) * 86400
        preview_count = max(1, int(history_cfg.preview_message_count))

        try:
            messages = await self.ctx.call_capability(
                "message.get_by_time_in_chat",
                chat_id=stream_id,
                start_time=start_ts,
                end_time=end_ts,
                limit=preview_count,
                limit_mode="latest",
            )
        except Exception:
            self.ctx.logger.exception(f"拉取最近消息失败 stream={stream_id}")
            return

        if not isinstance(messages, list) or not messages:
            return

        latest_ts = max(
            (float(m.get("timestamp") or 0.0) for m in messages if isinstance(m, dict)),
            default=0.0,
        )
        if latest_ts > 0:
            snapshot["silence_hours"] = round(max(0.0, end_ts - latest_ts) / 3600.0, 1)

        snapshot["recent_messages"] = [
            self._slim_message(m) for m in messages if isinstance(m, dict)
        ]

        # 找最新一条对方发的真实用户消息,把它的 message_id 放进 snapshot,供 intent 引用。
        # 主程序的 proactive task 在 chat_history 里没挂 original_message,reply 工具
        # 用 task_id 会找不到目标。必须给 LLM 一个真正可解析的 msg_id 作为退路。
        target_user_id = str(stream.get("user_id") or "").strip()
        if target_user_id:
            snapshot["latest_user_msg_id"] = self._extract_latest_user_message_id(
                messages, target_user_id
            )

    @staticmethod
    def _extract_latest_user_message_id(
        messages: list[Any],
        target_user_id: str,
    ) -> str:
        """从消息列表里挑出对方最近一条消息的 message_id。

        消息字段格式来自 PluginMessageUtils._session_message_to_dict:
        message_info.user_info.user_id 为发送者账号 ID。
        """
        latest_ts = -1.0
        latest_id = ""
        for message in messages:
            if not isinstance(message, dict):
                continue
            user_info = (message.get("message_info") or {}).get("user_info") or {}
            sender_id = str(user_info.get("user_id") or "").strip()
            if sender_id != target_user_id:
                continue
            try:
                message_ts = float(message.get("timestamp") or 0.0)
            except (TypeError, ValueError):
                continue
            message_id = str(message.get("message_id") or "").strip()
            if not message_id:
                continue
            if message_ts >= latest_ts:
                latest_ts = message_ts
                latest_id = message_id
        return latest_id

    @staticmethod
    def _slim_message(message: dict[str, Any]) -> dict[str, Any]:
        """从序列化消息里只挑给 MaiSaka 看的字段。"""
        text = str(message.get("processed_plain_text") or "").strip()
        if len(text) > 120:
            text = text[:120] + "..."
        return {
            "ts": float(message.get("timestamp") or 0.0),
            "text": text,
        }

    # ----------------------------- 时段语义 -----------------------------

    @staticmethod
    def _build_time_semantics(now_dt: datetime) -> str:
        """把"周六晚 8 点"翻译成"周末晚上,大多数人在放松"等情境描述。"""
        is_weekend = now_dt.weekday() >= 5
        hour = now_dt.hour
        day_kind = "周末" if is_weekend else "工作日"
        if 0 <= hour < 6:
            period = "凌晨/深夜,大多数人在睡觉"
        elif 6 <= hour < 9:
            period = "早上,通常在准备出门或刚起床" if is_weekend else "早上,大多数人在准备上班/上学"
        elif 9 <= hour < 12:
            period = "上午,大多数人在自由活动" if is_weekend else "上午,大多数人在工作/上课"
        elif 12 <= hour < 14:
            period = "午饭/午休时段"
        elif 14 <= hour < 18:
            period = "下午,大多数人在自由活动" if is_weekend else "下午,大多数人在工作/上课"
        elif 18 <= hour < 22:
            period = "傍晚到晚上,大多数人下班/下课在放松"
        else:
            period = "深夜,大多数人在睡前刷手机或已经睡了"
        return f"{day_kind},{period}"

    # ----------------------------- 节日识别 -----------------------------

    @staticmethod
    def _build_festival_context(today: date) -> str:
        """识别今天/邻近节日。只看公历。"""
        today_key = (today.month, today.day)
        if today_key in _FESTIVALS:
            return f"今天是{_FESTIVALS[today_key]}"

        # 找未来 7 天内最近的节日
        for offset in range(1, 8):
            try:
                future_date = today.replace(day=today.day + offset)
            except ValueError:
                # 跨月,逐日累加
                future_date = date.fromordinal(today.toordinal() + offset)
            key = (future_date.month, future_date.day)
            if key in _FESTIVALS:
                return f"再过 {offset} 天就是{_FESTIVALS[key]}"
        return ""

    # ----------------------------- 今日热点 -----------------------------

    async def _fetch_hot_topics(self) -> list[str]:
        """拉今日热点。失败/超时静默返回空列表,带缓存。"""
        ext_cfg = self.config.external_world
        now_ts = time.time()
        if self._hot_topics_cache is not None and self._hot_topics_cache[0] > now_ts:
            return self._hot_topics_cache[1]

        url = self._resolve_hot_topics_url()
        if not url:
            return []

        try:
            async with httpx.AsyncClient(timeout=float(ext_cfg.hot_topics_timeout_seconds)) as client:
                response = await client.get(url, headers={"User-Agent": "MaiBot-ProactiveChat/0.2"})
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            self.ctx.logger.warning(f"拉取今日热点失败 url={url}: {exc}")
            return []

        items = self._extract_hot_items(payload, max_items=max(1, int(ext_cfg.hot_topics_max_items)))
        expire_ts = now_ts + max(1, int(ext_cfg.hot_topics_cache_minutes)) * 60
        self._hot_topics_cache = (expire_ts, items)
        return items

    def _resolve_hot_topics_url(self) -> str:
        """根据配置选择实际的热点 URL。"""
        ext_cfg = self.config.external_world
        source = ext_cfg.hot_topics_source.strip().lower()
        if source == "custom":
            return ext_cfg.hot_topics_custom_url.strip()
        return _HOT_SOURCE_PRESETS.get(source, _HOT_SOURCE_PRESETS["60s"])

    @staticmethod
    def _extract_hot_items(payload: Any, max_items: int) -> list[str]:
        """从各种第三方热点 API 响应中提取条目文本。

        覆盖几个常见的字段约定:
        - 60s: {"data": {"news": ["...", "..."], "tip": "..."}}
        - vvhan: {"success": True, "data": [{"title": "..."}, ...]}
        - custom: 自动从 data / items / list 等字段试探取列表
        """
        if not isinstance(payload, dict):
            return []

        # 60s API
        data_field = payload.get("data")
        if isinstance(data_field, dict):
            news_list = data_field.get("news")
            if isinstance(news_list, list) and news_list:
                return [str(item).strip() for item in news_list[:max_items] if str(item).strip()]

        # vvhan / 通用 list[dict]
        list_candidate: list[Any] = []
        for key in ("data", "items", "list", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                list_candidate = value
                break

        results: list[str] = []
        for item in list_candidate[:max_items]:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(
                    item.get("title")
                    or item.get("name")
                    or item.get("text")
                    or item.get("hot")
                    or ""
                ).strip()
            else:
                text = ""
            if text:
                results.append(text)
        return results

    # ----------------------------- 天气 -----------------------------

    async def _fetch_weather(self, city: str) -> str:
        """拉天气。失败/超时静默返回空串,带缓存。"""
        ext_cfg = self.config.external_world
        now_ts = time.time()
        if self._weather_cache is not None and self._weather_cache[0] > now_ts:
            return self._weather_cache[1]

        # wttr.in 简短格式:"晴 +18°C"
        url = f"https://wttr.in/{city}?format=%C+%t&lang=zh"
        try:
            async with httpx.AsyncClient(timeout=float(ext_cfg.weather_timeout_seconds)) as client:
                response = await client.get(url, headers={"User-Agent": "curl/8.0"})
                response.raise_for_status()
                text = (response.text or "").strip()
        except Exception as exc:
            self.ctx.logger.warning(f"拉取天气失败 city={city}: {exc}")
            return ""

        expire_ts = now_ts + max(1, int(ext_cfg.weather_cache_minutes)) * 60
        self._weather_cache = (expire_ts, text)
        return text

    # ----------------------------- intent 组装 -----------------------------

    def _build_intent_text(
        self,
        stream: dict[str, Any],
        whitelist_entry: WhitelistEntry | None,
        snapshot: dict[str, Any],
    ) -> str:
        """组装 intent 文本。

        刻意只摆事实(对方身份、当前情境、外部世界),不预设动机/口吻/话题,
        把"是否聊、聊什么、用什么称呼"完整交还给 MaiSaka。
        """
        user_id = str(stream.get("user_id") or "").strip() or "对方"
        identity = whitelist_entry.identity.strip() if whitelist_entry else ""

        # 对方信息段
        if identity:
            who_line = f"对方账号是 {user_id},在你这里登记的身份是「{identity}」。"
        else:
            who_line = f"对方账号是 {user_id}。"

        # 沉默时长段
        silence_hours = snapshot.get("silence_hours")
        if silence_hours is None:
            silence_line = "你们之前没有可见的聊天记录。"
        elif silence_hours < 1:
            silence_line = "你们刚刚还聊过没多久。"
        else:
            silence_line = f"你们上一次聊天大约是 {silence_hours} 小时前。"

        # 最近一句段
        recent = snapshot.get("recent_messages") or []
        if recent:
            last_text = str(recent[-1].get("text") or "").strip()
            last_line = f'你印象里最后一句话是:"{last_text}"\n' if last_text else ""
        else:
            last_line = ""

        # 外部世界段(按段拼装,空段不显示)
        external_lines: list[str] = []
        if snapshot.get("time_semantics"):
            external_lines.append(f"- 当下时段:{snapshot['time_semantics']}")
        if snapshot.get("festival"):
            external_lines.append(f"- 日历:{snapshot['festival']}")
        if snapshot.get("weather"):
            external_lines.append(f"- 天气:{snapshot['weather']}")
        if snapshot.get("hot_topics"):
            topics_text = "\n".join(f"  · {item}" for item in snapshot["hot_topics"])
            external_lines.append(f"- 今日热点(现实世界正在聊的):\n{topics_text}")
        external_block = (
            "外部世界此刻是这样的(仅作背景参考,不要为了用上它而硬扯进话题):\n"
            + "\n".join(external_lines)
            + "\n"
            if external_lines
            else ""
        )

        # reply 工具的 msg_id 提示。
        # 主程序对插件主动任务的处理:把 task_id 写进 chat_history,但没挂
        # original_message。结果 reply 工具用 task_id 找不到目标消息会失败。
        # 插件能看到 DB 里的最近消息,但看不到 MaiSaka 运行时的 _chat_history,
        # 不能担保从 DB 拿到的 msg_id 当前还在 chat_history 里。
        # 所以把"哪个 msg_id 可用"的判断权完全交还给 LLM —— 它能直接看 chat_history。
        latest_user_msg_id = str(snapshot.get("latest_user_msg_id") or "").strip()
        candidate_line = (
            f'(参考:数据库里对方最近一条真实消息 msg_id="{latest_user_msg_id}",\n'
            "但当前聊天历史可能已被裁剪,不一定还在,需要你自己核对。)\n"
            if latest_user_msg_id
            else ""
        )
        tool_hint = (
            "工具使用提示:这条任务由插件投递,任务上下文里出现的 id 是任务编号,\n"
            "不是普通用户消息的 msg_id,绝对不能传给 reply 工具的 msg_id 参数 ——\n"
            "否则会报『未找到要回复的目标消息』。\n"
            f"{candidate_line}"
            "如果你决定主动开口,按以下顺序选择:\n"
            "  1. 抬头看你头顶的聊天历史。如果里面能找到对方真实发的\n"
            '     <message msg_id="..." user="对方名"> 标签,挑一条值得接续的,\n'
            "     把它的 msg_id 填给 reply,set_quote=false(主动话题不必引用某句)。\n"
            "  2. 历史里只有命令(/xxx)、系统提示、本任务自身的痕迹,\n"
            "     没有可作为接续目标的对方真实消息时,reply 工具用不了,\n"
            "     请改用 send_emoji 发一张表情作低姿态的开场。\n"
            "  3. 实在没合适的目标、或当下不想接触,直接 finish 或 no_action 收手 ——\n"
            "     什么都不做也是正确答案,远比错误调用 reply 体面。\n"
        )

        return (
            f"现在是 {snapshot['now']}({snapshot['weekday']})。\n"
            f"{who_line}{silence_line}\n"
            f"{last_line}"
            f"{external_block}\n"
            "请你停下来,把自己放进这一刻 —— 你当下的状态、对方在你心里的位置、"
            "最近这段对话留给你的感受 —— 然后问问自己:\n"
            "我现在,真的想主动找对方聊吗?为什么?\n\n"
            "如果想,完全由你决定开口的方式 —— 说什么、用什么语气、聊哪个角度、"
            "怎么称呼对方,都按你自己当下的真实念头来。话题可以是任何东西:\n"
            "  · 你自己刚经历的事、突然想到的回忆、一个梦、一个新发现的爱好;\n"
            "  · 对方上次没说完的话、你一直惦记的某件事、单纯的问候和撒娇;\n"
            "  · 上面外部世界里某条让你有真实感受的事,但只在你真的有感而发时再聊。\n"
            "完全不挂钩外部世界、纯粹从人设和心情自然冒出的开场,是被鼓励的。\n"
            "身份信息只是事实,不要被字面绑死,也不要为了完成任务而硬找话题。\n\n"
            "如果不想(没念头、觉得不合适、对方此刻应该在忙、或单纯不想),就保持沉默,"
            "什么都不做也是正确答案。\n\n"
            f"{tool_hint}"
        )

    # ----------------------------- 状态持久化 -----------------------------

    def _maybe_rollover_day(self) -> None:
        """跨天时重置当日计数。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._wakeup_day:
            self._wakeup_day = today
            self._wakeup_count_today = 0

    async def _load_state(self) -> None:
        """读取本地状态文件。"""
        async with self._state_lock:
            if not self._state_path.is_file():
                return
            try:
                state = json.loads(self._state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.ctx.logger.warning(f"读取插件状态失败,忽略: {exc}")
                return
            raw_last_at = state.get("last_wakeup_at") or {}
            if isinstance(raw_last_at, dict):
                self._last_wakeup_at = {
                    str(key): float(value)
                    for key, value in raw_last_at.items()
                    if isinstance(value, (int, float))
                }
            self._wakeup_day = str(state.get("wakeup_day") or "").strip()
            try:
                self._wakeup_count_today = int(state.get("wakeup_count_today") or 0)
            except (TypeError, ValueError):
                self._wakeup_count_today = 0

    async def _save_state(self) -> None:
        """落盘状态。"""
        async with self._state_lock:
            payload = {
                "last_wakeup_at": self._last_wakeup_at,
                "wakeup_day": self._wakeup_day,
                "wakeup_count_today": self._wakeup_count_today,
            }
            try:
                self._state_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                self.ctx.logger.warning(f"写入插件状态失败,忽略: {exc}")


def create_plugin() -> ProactiveChatPlugin:
    """SDK 要求的工厂函数。"""
    return ProactiveChatPlugin()
