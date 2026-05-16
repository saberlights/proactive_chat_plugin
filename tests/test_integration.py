"""集成测试:用 mock ctx 把插件完整链路跑通。
不依赖 MaiBot 主程序,只验证插件本身行为是否符合设计。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plugin as P  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")

# 总测试计数
_OK = 0
_FAIL = 0


def assert_eq(actual, expected, label: str) -> None:
    global _OK, _FAIL
    if actual == expected:
        _OK += 1
        print(f"  ✓ {label}")
    else:
        _FAIL += 1
        print(f"  ✗ {label}  expected={expected!r}  actual={actual!r}")


def assert_true(cond, label: str) -> None:
    global _OK, _FAIL
    if cond:
        _OK += 1
        print(f"  ✓ {label}")
    else:
        _FAIL += 1
        print(f"  ✗ {label}")


def assert_in(needle: str, haystack: str, label: str) -> None:
    global _OK, _FAIL
    if needle in haystack:
        _OK += 1
        print(f"  ✓ {label}")
    else:
        _FAIL += 1
        print(f"  ✗ {label}  '{needle}' not in: {haystack[:200]}")


def make_plugin(config: P.ProactiveChatConfig, state_dir: Path) -> P.ProactiveChatPlugin:
    """构造一个完整可跑的插件实例,带 mock ctx 和临时 state 文件。"""
    inst = P.ProactiveChatPlugin()
    inst._plugin_config_instance = config
    inst._state_path = state_dir / "state.json"
    inst._ctx = MagicMock()
    inst._ctx.logger = logging.getLogger("test-plugin")
    inst._ctx.chat.get_private_streams = AsyncMock(return_value=[])
    inst._ctx.call_capability = AsyncMock(return_value={"success": True})
    return inst


def build_default_config() -> P.ProactiveChatConfig:
    """关掉所有外部世界(避免实测打外网),白名单两个对象。"""
    return P.ProactiveChatConfig(
        whitelist=P.WhitelistSectionConfig(
            mode="strict",
            entries=[
                P.WhitelistEntry(platform="qq", user_id="584232670", identity="哥哥"),
                P.WhitelistEntry(platform="qq", user_id="100000001", identity="同学小明"),
            ],
        ),
        external_world=P.ExternalWorldSectionConfig(
            enable_time_semantics=True,
            enable_festival=True,
            enable_hot_topics=False,
            enable_weather=False,
        ),
        wakeup=P.WakeupSectionConfig(
            per_chat_min_gap_hours=6,
            daily_wakeup_cap=24,
            startup_delay_seconds=0,
        ),
    )


# =================================================================


def test_config_defaults():
    print("\n[1] 配置默认值")
    cfg = P.ProactiveChatConfig()
    assert_eq(cfg.plugin.enabled, True, "默认启用")
    assert_eq(cfg.whitelist.mode, "strict", "默认 strict 模式")
    assert_eq(len(cfg.whitelist.entries), 0, "默认空白名单")
    assert_eq(cfg.platforms.include, ["qq"], "默认 platform=qq")
    assert_eq(cfg.external_world.hot_topics_source, "60s", "默认热点源=60s")


def test_time_semantics():
    print("\n[2] 时段语义全时段覆盖")
    f = P.ProactiveChatPlugin._build_time_semantics
    assert_in("周末", f(datetime(2026, 5, 16, 20, 0)), "周六晚→周末")
    assert_in("工作日", f(datetime(2026, 5, 18, 10, 0)), "周一上午→工作日")
    assert_in("睡觉", f(datetime(2026, 5, 18, 3, 0)), "凌晨→睡觉")
    assert_in("午饭", f(datetime(2026, 5, 18, 12, 30)), "中午→午饭")
    assert_in("深夜", f(datetime(2026, 5, 18, 23, 30)), "晚 23:30→深夜")


def test_festival():
    print("\n[3] 节日识别")
    f = P.ProactiveChatPlugin._build_festival_context
    assert_in("圣诞", f(date(2026, 12, 25)), "12/25 → 圣诞节")
    assert_in("平安夜", f(date(2026, 12, 22)), "12/22 → 最近节日=平安夜")
    assert_eq(f(date(2026, 5, 16)), "", "5/16 无邻近")
    assert_in("元旦", f(date(2026, 1, 1)), "1/1 → 元旦")


def test_whitelist_lookup():
    print("\n[4] 白名单查找")
    with tempfile.TemporaryDirectory() as tmp:
        inst = make_plugin(build_default_config(), Path(tmp))
        e = inst._whitelist_lookup("qq", "584232670")
        assert_true(e is not None, "命中 584232670")
        assert_eq(e.identity, "哥哥", "身份=哥哥")
        assert_eq(inst._whitelist_lookup("qq", "999"), None, "未命中返回 None")
        assert_eq(inst._whitelist_lookup("discord", "584232670"), None, "平台不匹配")
        assert_eq(inst._whitelist_lookup("QQ", " 584232670 "), e, "大小写/空格不影响")


def test_intent_text():
    print("\n[5] intent 文本组装")
    with tempfile.TemporaryDirectory() as tmp:
        inst = make_plugin(build_default_config(), Path(tmp))
        cfg = inst.config

        snapshot_full = {
            "now": "2026-05-16T20:00:00",
            "weekday": "周六",
            "silence_hours": 36.5,
            "recent_messages": [{"ts": 0, "text": "演讲准备到一半,有点紧张"}],
            "latest_user_msg_id": "real-msg-id-9527",
            "time_semantics": "周末,傍晚",
            "festival": "再过 3 天就是儿童节",
            "hot_topics": ["AI 又出新模型"],
            "weather": "晴 +22°C",
        }
        text_full = inst._build_intent_text(
            stream={"user_id": "584232670"},
            whitelist_entry=cfg.whitelist.entries[0],
            snapshot=snapshot_full,
        )
        assert_in("哥哥", text_full, "intent 含身份")
        assert_in("36.5 小时前", text_full, "intent 含沉默时长")
        assert_in("演讲准备到一半", text_full, "intent 含最后一句")
        assert_in("AI 又出新模型", text_full, "intent 含热点")
        assert_in("背景参考", text_full, "intent 含'背景参考'解绑")
        assert_in("被鼓励", text_full, "intent 含'被鼓励'解绑")
        assert_in("保持沉默", text_full, "intent 含'保持沉默'否决权")
        assert_in("real-msg-id-9527", text_full, "intent 暴露真实 msg_id 给 LLM")
        assert_in("reply 工具", text_full, "intent 提到 reply 工具用法")

        snapshot_empty = {
            "now": "2026-05-16T20:00:00",
            "weekday": "周六",
            "silence_hours": None,
            "recent_messages": [],
            "latest_user_msg_id": "",
            "time_semantics": "",
            "festival": "",
            "hot_topics": [],
            "weather": "",
        }
        text_empty = inst._build_intent_text(
            stream={"user_id": "584232670"},
            whitelist_entry=None,
            snapshot=snapshot_empty,
        )
        assert_in("584232670", text_empty, "无身份时直接用 user_id")
        assert_in("没有可见的聊天记录", text_empty, "无历史时友好提示")
        assert_true("外部世界此刻是这样的" not in text_empty, "外部世界全空时不输出整段")
        assert_in("send_emoji", text_empty, "无 msg_id 时引导 send_emoji 兜底")
        assert_in("finish", text_empty, "无 msg_id 时也提示 finish 退路")


def test_extract_latest_user_message_id():
    print("\n[5b] 抽取对方最新真实 msg_id(过滤掉 bot 自己的消息)")
    extract = P.ProactiveChatPlugin._extract_latest_user_message_id
    messages = [
        {
            "message_id": "u-old",
            "timestamp": 1000.0,
            "message_info": {"user_info": {"user_id": "584232670"}},
        },
        {
            "message_id": "bot-1",
            "timestamp": 2000.0,
            "message_info": {"user_info": {"user_id": "bot-self"}},
        },
        {
            "message_id": "u-new",
            "timestamp": 1500.0,
            "message_info": {"user_info": {"user_id": "584232670"}},
        },
    ]
    assert_eq(extract(messages, "584232670"), "u-new", "挑出对方时间戳最大的 msg_id")
    assert_eq(extract(messages, "999"), "", "没有匹配 user_id 时返回空串")
    assert_eq(extract([], "584232670"), "", "空列表返回空串")
    assert_eq(
        extract(
            [{"message_id": "x", "timestamp": 1.0, "message_info": {}}],
            "584232670",
        ),
        "",
        "缺 user_info 视为不可用",
    )


def test_hot_items_extraction():
    print("\n[6] 热点条目提取(多种响应结构)")
    f = P.ProactiveChatPlugin._extract_hot_items
    # 60s
    items = f({"data": {"news": ["A", "B", "C", "D", "E", "F"]}}, max_items=3)
    assert_eq(items, ["A", "B", "C"], "60s 风格")
    # 98dou / vvhan list-of-dict
    items = f({"success": True, "data": [{"title": "热搜1"}, {"title": "热搜2"}]}, max_items=5)
    assert_eq(items, ["热搜1", "热搜2"], "98dou 风格")
    # items 字段
    items = f({"items": [{"name": "X"}, {"text": "Y"}]}, max_items=2)
    assert_eq(items, ["X", "Y"], "items + name/text")
    # 空响应
    assert_eq(f({}, max_items=3), [], "空 dict")
    assert_eq(f("not a dict", max_items=3), [], "非 dict")


def test_state_persistence():
    print("\n[7] 状态持久化读写")
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_plugin(build_default_config(), Path(tmp))
            inst._last_wakeup_at = {"qq:user:1": 1700000000.0}
            inst._wakeup_day = "2026-05-16"
            inst._wakeup_count_today = 7
            await inst._save_state()
            assert_true(inst._state_path.exists(), "state.json 已写入")

            inst2 = make_plugin(build_default_config(), Path(tmp))
            await inst2._load_state()
            assert_eq(inst2._last_wakeup_at, {"qq:user:1": 1700000000.0}, "last_wakeup_at 还原")
            assert_eq(inst2._wakeup_day, "2026-05-16", "wakeup_day 还原")
            assert_eq(inst2._wakeup_count_today, 7, "wakeup_count_today 还原")
    asyncio.run(run())


def test_daily_rollover():
    print("\n[8] 跨天 rollover")
    with tempfile.TemporaryDirectory() as tmp:
        inst = make_plugin(build_default_config(), Path(tmp))
        inst._wakeup_day = "1999-01-01"
        inst._wakeup_count_today = 23
        inst._maybe_rollover_day()
        today = datetime.now().strftime("%Y-%m-%d")
        assert_eq(inst._wakeup_day, today, "wakeup_day 更新到今天")
        assert_eq(inst._wakeup_count_today, 0, "今日计数重置")


def test_strict_whitelist_filters():
    print("\n[9] strict 白名单模式:按白名单遍历,自动解析已有 session")
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_plugin(build_default_config(), Path(tmp))
            # 584... 已有 session;100000001 也已有 session
            stream_584 = {"stream_id": "qq:user:584232670", "platform": "qq", "user_id": "584232670"}
            stream_100 = {"stream_id": "qq:user:100000001", "platform": "qq", "user_id": "100000001"}
            inst._ctx.chat.get_stream_by_user_id = AsyncMock(side_effect=[stream_584, stream_100])
            P.random.random = lambda: 0.0  # type: ignore

            async def fake_cap(cap_name: str, **kw):
                if cap_name == "message.get_by_time_in_chat":
                    return []
                if cap_name == "maisaka.proactive.trigger":
                    return {"success": True}
                return None
            inst._ctx.call_capability = fake_cap  # type: ignore

            await inst._do_one_wakeup_sweep()
            assert_true(
                inst._ctx.chat.get_stream_by_user_id.await_count == 2,
                "对 2 个白名单条目都做了查询",
            )
            assert_eq(inst._wakeup_count_today, 2, "两条都触发")
    asyncio.run(run())


def test_strict_auto_open_session():
    print("\n[9b] strict + open_session:对从未聊过的白名单对象主动建 session")
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_plugin(build_default_config(), Path(tmp))
            # get_stream_by_user_id 返回 None(都没有 session)
            inst._ctx.chat.get_stream_by_user_id = AsyncMock(return_value=None)
            P.random.random = lambda: 0.0  # type: ignore

            open_calls: list[dict] = []
            trigger_calls: list[dict] = []

            async def fake_cap(cap_name: str, **kw):
                if cap_name == "chat.open_session":
                    open_calls.append(kw)
                    return {
                        "success": True,
                        "created": True,
                        "stream": {
                            "stream_id": f"qq:user:{kw['user_id']}",
                            "platform": kw["platform"],
                            "user_id": kw["user_id"],
                        },
                    }
                if cap_name == "message.get_by_time_in_chat":
                    return []
                if cap_name == "maisaka.proactive.trigger":
                    trigger_calls.append(kw)
                    return {"success": True}
                return None
            inst._ctx.call_capability = fake_cap  # type: ignore

            await inst._do_one_wakeup_sweep()
            assert_eq(len(open_calls), 2, "对 2 个白名单条目都调用 open_session")
            assert_eq(open_calls[0]["chat_type"], "private", "open_session chat_type=private")
            triggered_ids = sorted(c["stream_id"] for c in trigger_calls)
            assert_eq(
                triggered_ids,
                ["qq:user:100000001", "qq:user:584232670"],
                "新建的 stream 都被投递了",
            )
    asyncio.run(run())


def test_strict_open_session_failure_skip():
    print("\n[9c] strict + open_session 失败:对应条目静默跳过,不阻塞其他人")
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_plugin(build_default_config(), Path(tmp))
            inst._ctx.chat.get_stream_by_user_id = AsyncMock(return_value=None)
            P.random.random = lambda: 0.0  # type: ignore

            async def fake_cap(cap_name: str, **kw):
                if cap_name == "chat.open_session":
                    # 第一个失败,第二个成功
                    if kw["user_id"] == "584232670":
                        return {"success": False, "error": "用户不是好友"}
                    return {
                        "success": True,
                        "stream": {
                            "stream_id": "qq:user:100000001",
                            "platform": "qq",
                            "user_id": "100000001",
                        },
                    }
                if cap_name == "message.get_by_time_in_chat":
                    return []
                if cap_name == "maisaka.proactive.trigger":
                    return {"success": True}
                return None
            inst._ctx.call_capability = fake_cap  # type: ignore

            await inst._do_one_wakeup_sweep()
            assert_eq(inst._wakeup_count_today, 1, "失败那条静默跳过,另一条正常触发")
    asyncio.run(run())


def test_full_sweep_pipeline():
    print("\n[10] 完整流程:成功触发 → 状态更新 + intent 含身份 + metadata 含 snapshot")
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_plugin(build_default_config(), Path(tmp))
            stream_584 = {"stream_id": "qq:user:584232670", "platform": "qq", "user_id": "584232670"}
            stream_100 = {"stream_id": "qq:user:100000001", "platform": "qq", "user_id": "100000001"}
            inst._ctx.chat.get_stream_by_user_id = AsyncMock(side_effect=[stream_584, stream_100])

            captured: dict[str, dict] = {}

            async def fake_cap(cap_name: str, **kw):
                if cap_name == "message.get_by_time_in_chat":
                    if kw.get("chat_id") == "qq:user:584232670":
                        return [{
                            "timestamp": datetime.now().timestamp() - 36 * 3600,
                            "processed_plain_text": "演讲准备到一半,有点紧张",
                        }]
                    return []
                if cap_name == "maisaka.proactive.trigger":
                    captured[kw["stream_id"]] = kw
                    return {"success": True, "task_id": "tid-1", "queued": True}
                return None

            inst._ctx.call_capability = fake_cap  # type: ignore
            P.random.random = lambda: 0.0  # type: ignore

            await inst._do_one_wakeup_sweep()

            assert_eq(inst._wakeup_count_today, 2, "今日计数 +2(两条白名单都触发)")
            assert_true("qq:user:584232670" in inst._last_wakeup_at, "last_wakeup_at 已记录")
            assert_true(inst._state_path.exists(), "状态已落盘")

            last_584 = captured["qq:user:584232670"]
            assert_in("哥哥", last_584["intent"], "intent 含身份")
            assert_in("36.0 小时前", last_584["intent"], "intent 含沉默时长")
            assert_in("演讲准备到一半", last_584["intent"], "intent 含最后一句")
            assert_eq(last_584["metadata"]["whitelist_identity"], "哥哥", "metadata 含 identity")
            assert_true("snapshot" in last_584["metadata"], "metadata 含 snapshot")
    asyncio.run(run())


def test_per_chat_cooldown():
    print("\n[11] 单聊冷却:刚刚触发过的对象立即不会再触发")
    async def run():
        import time as t
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_plugin(build_default_config(), Path(tmp))
            stream_584 = {"stream_id": "qq:user:584232670", "platform": "qq", "user_id": "584232670"}
            stream_100 = {"stream_id": "qq:user:100000001", "platform": "qq", "user_id": "100000001"}
            inst._ctx.chat.get_stream_by_user_id = AsyncMock(side_effect=[stream_584, stream_100])
            inst._last_wakeup_at = {
                "qq:user:584232670": t.time() - 60,
                "qq:user:100000001": t.time() - 60,
            }
            P.random.random = lambda: 0.0  # type: ignore
            await inst._do_one_wakeup_sweep()
            calls = [
                c for c in inst._ctx.call_capability.call_args_list
                if c.args and c.args[0] == "maisaka.proactive.trigger"
            ]
            assert_eq(len(calls), 0, "冷却内不重复触发")
    asyncio.run(run())


def test_daily_cap():
    print("\n[12] 每日上限生效")
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_plugin(build_default_config(), Path(tmp))
            inst._wakeup_count_today = 24
            inst._wakeup_day = datetime.now().strftime("%Y-%m-%d")
            inst._ctx.chat.get_stream_by_user_id = AsyncMock(return_value={
                "stream_id": "qq:user:584232670", "platform": "qq", "user_id": "584232670",
            })
            P.random.random = lambda: 0.0  # type: ignore
            await inst._do_one_wakeup_sweep()
            calls = [
                c for c in inst._ctx.call_capability.call_args_list
                if c.args and c.args[0] == "maisaka.proactive.trigger"
            ]
            assert_eq(len(calls), 0, "达到 cap 后不再触发")
    asyncio.run(run())


def test_off_mode_allows_all():
    print("\n[13] off 模式:所有私聊都可被找")
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_default_config()
            cfg.whitelist.mode = "off"
            inst = make_plugin(cfg, Path(tmp))
            inst._ctx.chat.get_private_streams = AsyncMock(return_value=[
                {"stream_id": "qq:user:999999999", "platform": "qq", "user_id": "999999999"},
            ])
            P.random.random = lambda: 0.0  # type: ignore
            await inst._do_one_wakeup_sweep()
            calls = [
                c for c in inst._ctx.call_capability.call_args_list
                if c.args and c.args[0] == "maisaka.proactive.trigger"
            ]
            assert_eq(len(calls), 1, "off 模式下非白名单也触发")
    asyncio.run(run())


def test_force_bypasses_all_gating():
    print("\n[14] force 模式:绕过白名单/冷却/概率/上限,但身份仍能注入")
    async def run():
        import time as t
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_plugin(build_default_config(), Path(tmp))
            # 设置极端不利条件:不在白名单 + 刚刚触发过 + 达到日上限
            inst._last_wakeup_at = {"qq:user:584232670": t.time()}
            inst._wakeup_count_today = 100
            inst._wakeup_day = datetime.now().strftime("%Y-%m-%d")
            # 强制软概率不命中(0.999 > 任何 soft_probability)
            P.random.random = lambda: 0.999  # type: ignore

            captured: dict[str, object] = {}

            async def fake_cap(cap_name: str, **kw):
                if cap_name == "message.get_by_time_in_chat":
                    return []
                if cap_name == "maisaka.proactive.trigger":
                    captured.update(kw)
                    return {"success": True, "task_id": "t-force", "queued": True}
                return None

            inst._ctx.call_capability = fake_cap  # type: ignore
            stream = {
                "stream_id": "qq:user:584232670",
                "platform": "qq",
                "user_id": "584232670",
            }
            await inst._try_wakeup_one(stream, force=True)

            assert_eq(captured.get("stream_id"), "qq:user:584232670", "force 触发到正确 stream")
            assert_in("哥哥", str(captured.get("intent", "")), "force 仍注入白名单 identity")
            meta = captured.get("metadata", {})
            assert_eq(meta.get("force_triggered"), True, "metadata.force_triggered=True")  # type: ignore
            assert_in("开发者手动测试", str(captured.get("reason", "")), "reason 标注手动测试")

    asyncio.run(run())


def test_force_works_for_non_whitelisted():
    print("\n[15] force 模式:即便不在白名单也能触发(身份留空)")
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_plugin(build_default_config(), Path(tmp))
            P.random.random = lambda: 0.999  # type: ignore

            captured: dict[str, object] = {}

            async def fake_cap(cap_name: str, **kw):
                if cap_name == "message.get_by_time_in_chat":
                    return []
                if cap_name == "maisaka.proactive.trigger":
                    captured.update(kw)
                    return {"success": True}
                return None

            inst._ctx.call_capability = fake_cap  # type: ignore
            await inst._try_wakeup_one(
                {"stream_id": "qq:user:8888", "platform": "qq", "user_id": "8888"},
                force=True,
            )
            assert_eq(captured.get("stream_id"), "qq:user:8888", "非白名单 force 触发成功")
            meta = captured.get("metadata", {})
            assert_eq(meta.get("whitelist_identity"), "", "无白名单时 identity 为空")  # type: ignore

    asyncio.run(run())


def test_command_decorator_registered():
    print("\n[16] Command 装饰器已注册(用 SDK collect_components 反射)")
    from maibot_sdk.components import collect_components
    with tempfile.TemporaryDirectory() as tmp:
        inst = make_plugin(build_default_config(), Path(tmp))
        components = collect_components(inst)
        commands = [c for c in components if str(c.get("type", "")).upper() == "COMMAND"]
        names = [c.get("name") for c in commands]
        assert_in("proactive_chat_test", str(names), "test 命令已被 collect_components 识别")
        # 校验 pattern 能匹配 /proactive test 和 /主动测试
        import re
        target = next(c for c in commands if c.get("name") == "proactive_chat_test")
        pattern = target["metadata"]["command_pattern"]
        assert_true(re.match(pattern, "/proactive test") is not None, "pattern 匹配 /proactive test")
        assert_true(re.match(pattern, "/proactive_test") is not None, "pattern 匹配 /proactive_test")
        assert_true(re.match(pattern, "/主动测试") is not None, "pattern 匹配 /主动测试")
        assert_true(re.match(pattern, "/something_else") is None, "pattern 不匹配无关命令")


# =================================================================


def main():
    test_config_defaults()
    test_time_semantics()
    test_festival()
    test_whitelist_lookup()
    test_intent_text()
    test_extract_latest_user_message_id()
    test_hot_items_extraction()
    test_state_persistence()
    test_daily_rollover()
    test_strict_whitelist_filters()
    test_strict_auto_open_session()
    test_strict_open_session_failure_skip()
    test_full_sweep_pipeline()
    test_per_chat_cooldown()
    test_daily_cap()
    test_off_mode_allows_all()
    test_force_bypasses_all_gating()
    test_force_works_for_non_whitelisted()
    test_command_decorator_registered()
    print(f"\n========== 结果: {_OK} 通过 / {_FAIL} 失败 ==========")
    sys.exit(0 if _FAIL == 0 else 1)


if __name__ == "__main__":
    main()
