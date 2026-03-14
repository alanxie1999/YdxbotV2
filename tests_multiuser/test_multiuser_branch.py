import asyncio
import json
import logging
import re
import threading
from pathlib import Path
from types import SimpleNamespace

from user_manager import UserContext, UserManager
from model_manager import ModelManager
import constants
import zq_multiuser as zm
import main_multiuser as mm


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_user_context_user_id_fallback_numeric_dir(tmp_path):
    user_dir = tmp_path / "users" / "1001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "测试用户"},
            "telegram": {},
        },
    )

    ctx = UserContext(str(user_dir))
    assert ctx.user_id == 1001
    assert ctx.config.name == "测试用户"


def test_user_context_user_id_fallback_hash_dir(tmp_path):
    user_dir = tmp_path / "users" / "alpha_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "Alpha"},
            "telegram": {},
        },
    )

    ctx = UserContext(str(user_dir))
    assert isinstance(ctx.user_id, int)
    assert ctx.user_id > 0


def test_user_manager_get_iflow_config_compatible_with_ai_key(tmp_path):
    users_dir = tmp_path / "users"
    config_dir = tmp_path / "config"
    users_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        config_dir / "global_config.json",
        {
            "ai": {"enabled": True, "base_url": "https://apis.iflow.cn/v1"},
        },
    )

    mgr = UserManager(users_dir=str(users_dir), config_dir=str(config_dir))
    mgr.load_all_users()
    cfg = mgr.get_iflow_config()
    assert cfg.get("enabled") is True
    assert "base_url" in cfg


def test_user_context_merges_global_common_and_user_private_config(tmp_path):
    users_dir = tmp_path / "users"
    config_dir = tmp_path / "config"
    users_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        config_dir / "global_config.json",
        {
            "groups": {"zq_group": [201], "zq_bot": 8},
            "zhuque": {"api_url": "https://zhuque.in/api/user/getInfo?"},
        },
    )
    _write_json(
        users_dir / "6001" / "6001_config.json",
        {
            "account": {"name": "合并用户"},
            "telegram": {"user_id": 6001},
            "zhuque": {"cookie": "c1", "x_csrf": "x1"},
            "notification": {
                "admin_chat": 6001,
                "iyuu": {"enable": True},
                "tg_bot": {"enable": True, "chat_id": "9"},
            },
            "ai": {
                "enabled": True,
                "base_url": "https://apis.iflow.cn/v1",
                "models": {"1": {"model_id": "m1", "enabled": True}},
            },
        },
    )

    mgr = UserManager(users_dir=str(users_dir), config_dir=str(config_dir))
    assert mgr.load_all_users() == 1

    ctx = mgr.get_user(6001)
    assert ctx is not None
    assert ctx.config.groups["admin_chat"] == 6001
    assert ctx.config.zhuque["api_url"] == "https://zhuque.in/api/user/getInfo?"
    assert ctx.config.zhuque["cookie"] == "c1"
    assert ctx.config.notification["iyuu"]["enable"] is True
    assert ctx.config.ai["base_url"] == "https://apis.iflow.cn/v1"


def test_model_manager_apply_shared_config_uses_shared_chain():
    mgr = ModelManager()
    mgr.apply_shared_config(
        {
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "base_url": "https://apis.iflow.cn/v1",
                "models": {
                    "1": {"model_id": "model-1", "enabled": True},
                    "2": {"model_id": "model-2", "enabled": True},
                },
                "fallback_chain": ["2", "1"],
            }
        }
    )

    assert mgr.fallback_chain == ["2", "1"]
    assert mgr.get_model("1")["model_id"] == "model-1"
    assert mgr.get_model("2")["model_id"] == "model-2"


def test_model_manager_call_model_immediately_falls_back_to_next_ranked_model():
    mgr = ModelManager()
    mgr.apply_shared_config(
        {
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "base_url": "https://apis.iflow.cn/v1",
                "models": {
                    "1": {"model_id": "model-1", "enabled": True},
                    "2": {"model_id": "model-2", "enabled": True},
                },
                "fallback_chain": ["1", "2"],
            }
        }
    )

    async def fake_iflow(config, messages, **kwargs):
        if config["model_id"] == "model-1":
            return {"success": False, "error": "model-1 unavailable", "content": ""}
        return {"success": True, "error": "", "content": '{"prediction": 1}'}

    mgr._call_iflow = fake_iflow

    result = asyncio.run(
        mgr.call_model("model-1", [{"role": "user", "content": "ping"}], temperature=0.1, max_tokens=10)
    )

    assert result["success"] is True
    assert result["model_id"] == "model-2"
    assert result["requested_model_id"] == "model-1"
    assert result["fallback_used"] is True


def test_parse_analysis_result_insight_supports_skip_prediction():
    parsed = zm.parse_analysis_result_insight(
        '{"prediction":"SKIP","confidence":66,"reason":"证据冲突"}',
        default_prediction=1,
    )
    assert parsed["prediction"] == -1
    assert parsed["confidence"] == 66


def test_consume_shadow_probe_settle_result_pass_sets_resume_pending():
    rt = {
        "shadow_probe_active": True,
        "shadow_probe_target_rounds": 2,
        "shadow_probe_pass_required": 1,
        "shadow_probe_checked": 1,
        "shadow_probe_hits": 0,
        "shadow_probe_pending_prediction": 1,
        "stop_count": 0,
    }

    progress = zm._consume_shadow_probe_settle_result(rt, result=1)

    assert progress["updated"] is True
    assert progress["done"] is True
    assert progress["passed"] is True
    assert rt["shadow_probe_active"] is False
    assert rt["pause_resume_pending"] is True
    assert rt["pause_resume_pending_reason"] == "影子验证通过"


def test_consume_shadow_probe_settle_result_fail_rearms_pause():
    rt = {
        "shadow_probe_active": True,
        "shadow_probe_target_rounds": 2,
        "shadow_probe_pass_required": 2,
        "shadow_probe_checked": 1,
        "shadow_probe_hits": 0,
        "shadow_probe_pending_prediction": 1,
        "stop_count": 0,
        "bet_on": True,
        "mode_stop": True,
    }

    progress = zm._consume_shadow_probe_settle_result(rt, result=0)

    assert progress["updated"] is True
    assert progress["done"] is True
    assert progress["passed"] is False
    assert rt["shadow_probe_active"] is False
    assert rt["shadow_probe_rearm"] is True
    assert rt["stop_count"] == zm.SHADOW_PROBE_RETRY_PAUSE_ROUNDS + 1
    assert rt["pause_countdown_active"] is True


def test_record_hand_stall_block_triggers_force_unlock_on_skip_limit():
    rt = {}
    zm._clear_hand_stall_guard(rt)
    # 前两次允许观望
    s1 = zm._record_hand_stall_block(rt, next_sequence=5, history_len=100, reason="skip")
    s2 = zm._record_hand_stall_block(rt, next_sequence=5, history_len=101, reason="skip")
    assert s1["force_unlock"] is False
    assert s2["force_unlock"] is False
    # 第三次触发解锁
    s3 = zm._record_hand_stall_block(rt, next_sequence=5, history_len=102, reason="skip")
    assert s3["force_unlock"] is True
    assert s3["skip_streak"] == 3


def test_heal_stale_pending_bets_marks_orphan_none_records(tmp_path):
    user_dir = tmp_path / "users" / "heal_user_1"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "自愈用户"},
            "telegram": {"user_id": 7101},
            "groups": {"admin_chat": 7101},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = False
    ctx.state.bet_sequence_log = [
        {"bet_id": "b1", "result": "赢", "profit": 990},
        {"bet_id": "b2", "result": None, "profit": 0},
        {"bet_id": "b3", "result": None, "profit": None},
    ]

    result = zm.heal_stale_pending_bets(ctx)
    assert result["count"] == 2
    assert ctx.state.bet_sequence_log[1]["result"] == "异常未结算"
    assert ctx.state.bet_sequence_log[2]["result"] == "异常未结算"
    assert ctx.state.bet_sequence_log[2]["profit"] == 0
    assert rt["pending_bet_last_heal_count"] == 2
    assert rt["pending_bet_heal_total"] >= 2


def test_heal_stale_pending_bets_keeps_latest_when_bet_pending(tmp_path):
    user_dir = tmp_path / "users" / "heal_user_2"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "自愈用户2"},
            "telegram": {"user_id": 7102},
            "groups": {"admin_chat": 7102},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    ctx.state.bet_sequence_log = [
        {"bet_id": "b1", "result": "输", "profit": -1000},
        {"bet_id": "b2", "result": None, "profit": None},
    ]

    result = zm.heal_stale_pending_bets(ctx)
    assert result["count"] == 0
    assert ctx.state.bet_sequence_log[-1]["result"] is None
    assert "pending_bet_last_heal_count" not in rt


def test_user_context_supports_hash_comments_in_config(tmp_path):
    user_dir = tmp_path / "users" / "commented"
    user_dir.mkdir(parents=True, exist_ok=True)
    config_text = """{
    # Telegram 登录参数
    "telegram": {
        "api_id": 123456,
        "api_hash": "abc123",
        "session_name": "demo",
        "user_id": 778899
    },
    # 账号信息
    "account": {"name": "注释用户"} # 行尾注释
}
"""
    (user_dir / "config.json").write_text(config_text, encoding="utf-8")
    ctx = UserContext(str(user_dir))
    assert ctx.user_id == 778899
    assert ctx.config.name == "注释用户"


def test_zq_log_event_includes_account_prefix_and_business_category(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "log_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "Musk Xu"},
            "telegram": {"user_id": 7001},
        },
    )
    ctx = UserContext(str(user_dir))
    zm.register_user_log_identity(ctx)

    captured = {}

    def fake_log(level, message, extra=None):
        captured["level"] = level
        captured["message"] = message
        captured["extra"] = extra or {}

    monkeypatch.setattr(zm.logger, "log", fake_log)
    zm.log_event(logging.INFO, "user_cmd", "处理用户命令", "ok", user_id=7001)

    assert captured["level"] == logging.INFO
    assert captured["extra"]["account_tag"] == "【ydx-musk-xu】"
    assert captured["extra"]["category"] == "business"
    assert captured["extra"]["user_id"] == "7001"


def test_zq_log_event_warning_level_goes_to_warning_category(monkeypatch):
    captured = {}

    def fake_log(level, message, extra=None):
        captured["level"] = level
        captured["extra"] = extra or {}

    monkeypatch.setattr(zm.logger, "log", fake_log)
    zm.log_event(logging.ERROR, "start", "用户启动失败", "fail", user_id=9001)

    assert captured["level"] == logging.ERROR
    assert captured["extra"]["category"] == "warning"
    assert captured["extra"]["account_tag"] == "【ydx-user-9001】"


def test_user_context_migrates_risk_default_switches_from_legacy_runtime(tmp_path):
    user_dir = tmp_path / "users" / "risk_migrate_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "迁移用户"},
            "telegram": {"user_id": 6122},
        },
    )
    _write_json(
        user_dir / "state.json",
        {
            "history": [],
            "bet_type_history": [],
            "predictions": [],
            "bet_sequence_log": [],
            "runtime": {
                "risk_base_enabled": False,
                "risk_deep_enabled": True,
            },
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    assert rt["risk_base_enabled"] is False
    assert rt["risk_deep_enabled"] is True
    assert rt["risk_base_default_enabled"] is False
    assert rt["risk_deep_default_enabled"] is True


def test_user_context_refreshes_builtin_presets_but_keeps_custom(tmp_path):
    user_dir = tmp_path / "users" / "preset_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "预设用户"},
            "telegram": {"user_id": 6123},
        },
    )
    _write_json(
        user_dir / "presets.json",
        {
            "yc05": ["1", "13", "3", "2.1", "2.1", "2.05", "500"],
            "my_custom": ["1", "6", "2.2", "2.1", "2.0", "2.0", "800"],
        },
    )

    ctx = UserContext(str(user_dir))
    assert ctx.presets["yc05"] == constants.PRESETS["yc05"]
    assert ctx.presets["my_custom"] == ["1", "6", "2.2", "2.1", "2.0", "2.0", "800"]

    saved_presets = json.loads((user_dir / "presets.json").read_text(encoding="utf-8"))
    assert saved_presets["yc05"] == constants.PRESETS["yc05"]
    assert saved_presets["my_custom"] == ["1", "6", "2.2", "2.1", "2.0", "2.0", "800"]


def test_main_multiuser_settle_regex_is_strict():
    source = Path("main_multiuser.py").read_text(encoding="utf-8")
    assert 'pattern=r"已结算: 结果为 (\\d+) (大|小)"' in source

    pattern = re.compile(r"已结算: 结果为 (\d+) (大|小)")
    assert pattern.search("已结算: 结果为 12 大")
    assert pattern.search("已结算: 结果为 8 小")
    assert pattern.search("已结算: 结果为 9 |") is None


def test_main_multiuser_session_lock_prevents_duplicate_acquire(tmp_path):
    user_dir = tmp_path / "users" / "lock_user"
    user_dir.mkdir(parents=True, exist_ok=True)

    ctx1 = SimpleNamespace(
        user_dir=str(user_dir),
        user_id=9001,
        config=SimpleNamespace(telegram={"session_name": "dup"}),
    )
    ctx2 = SimpleNamespace(
        user_dir=str(user_dir),
        user_id=9002,
        config=SimpleNamespace(telegram={"session_name": "dup"}),
    )

    assert mm._acquire_session_lock(ctx1) is True
    try:
        assert mm._acquire_session_lock(ctx2) is False
        mm._release_session_lock(ctx1)
        assert mm._acquire_session_lock(ctx2) is True
    finally:
        mm._release_session_lock(ctx1)
        mm._release_session_lock(ctx2)


def test_main_log_event_includes_account_prefix(monkeypatch):
    captured = {}
    fake_ctx = SimpleNamespace(
        user_id=8801,
        config=SimpleNamespace(name="Musk Xu"),
    )
    mm.register_main_user_log_identity(fake_ctx)

    def fake_log(level, message, extra=None):
        captured["level"] = level
        captured["extra"] = extra or {}

    monkeypatch.setattr(mm.logger, "log", fake_log)
    mm.log_event(logging.INFO, "start", "用户启动成功", "ok", user_id=8801)

    assert captured["level"] == logging.INFO
    assert captured["extra"]["account_tag"] == "【ydx-musk-xu】"
    assert captured["extra"]["category"] in {"runtime", "business"}


def test_user_isolation_between_two_contexts(tmp_path):
    users_dir = tmp_path / "users"
    config_dir = tmp_path / "config"
    _write_json(config_dir / "global_config.json", {"groups": {"zq_group": [1]}})

    _write_json(users_dir / "1001" / "config.json", {"account": {"name": "U1"}, "telegram": {"user_id": 1001}})
    _write_json(users_dir / "1002" / "config.json", {"account": {"name": "U2"}, "telegram": {"user_id": 1002}})

    mgr = UserManager(users_dir=str(users_dir), config_dir=str(config_dir))
    assert mgr.load_all_users() == 2

    u1 = mgr.get_user(1001)
    u2 = mgr.get_user(1002)
    assert u1 is not None and u2 is not None

    u1.set_runtime("bet_amount", 12345)
    u2.set_runtime("bet_amount", 54321)

    assert u1.get_runtime("bet_amount") == 12345
    assert u2.get_runtime("bet_amount") == 54321


def test_user_state_save_concurrent_no_corruption(tmp_path):
    user_dir = tmp_path / "users" / "2001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "并发用户"},
            "telegram": {"user_id": 2001},
        },
    )
    ctx = UserContext(str(user_dir))

    def worker(i):
        ctx.set_runtime("counter", i)
        ctx.save_state()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state_path = user_dir / "state.json"
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert "runtime" in loaded
    assert isinstance(loaded["runtime"], dict)
    assert "counter" in loaded["runtime"]


def test_send_message_returns_admin_message_object(tmp_path):
    user_dir = tmp_path / "users" / "3001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "消息用户"},
            "telegram": {"user_id": 3001},
            "groups": {"admin_chat": 3001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=88)

    message = asyncio.run(
        zm.send_message(
            DummyClient(),
            "admin",
            "hello",
            ctx,
            {},
        )
    )
    assert message is not None
    assert message.id == 88


def test_process_bet_on_parses_history_and_places_bet(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "下注用户"},
            "telegram": {"user_id": 4001},
            "groups": {"admin_chat": 4001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_info"] = "test"
        user_ctx.state.predictions.append(1)
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=1, id=1)

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 1
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert len(ctx.state.history) >= 40
    assert rt.get("bet") is True
    assert len(ctx.state.bet_sequence_log) == 1
    assert rt.get("current_bet_seq", 1) >= 2


def test_process_bet_on_allows_short_history_like_master(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4002"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "短历史用户"},
            "telegram": {"user_id": 4002},
            "groups": {"admin_chat": 4002},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["initial_amount"] = 500
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_info"] = "test-short-history"
        user_ctx.state.predictions.append(1)
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=1, id=1)

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            self.message = SimpleNamespace(message="[近 40 次结果][由近及远][0 小 1 大] 1 0 1")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 1
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert len(ctx.state.history) == 3
    assert rt.get("bet") is True
    assert len(ctx.state.bet_sequence_log) == 1


def test_process_bet_on_recovers_when_source_message_id_invalid(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4003"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "回溯点击用户"},
            "telegram": {"user_id": 4003},
            "groups": {"admin_chat": 4003, "zq_bot": 9001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["initial_amount"] = 500
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0

    sent_messages = []

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_info"] = "test-recover"
        user_ctx.state.predictions.append(1)
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=len(sent_messages))

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 100
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)
            raise Exception("The specified message ID is invalid or you can't do that operation on such message (caused by GetBotCallbackAnswerRequest)")

    class DummyFreshMsg:
        def __init__(self):
            self.sender_id = 9001
            self.reply_markup = object()
            self.message = "[近 40 次结果][由近及远][0 小 1 大] 1 0 1 0"
            self.raw_text = self.message
            self.id = 101
            self.clicked = []

        async def click(self, data):
            self.clicked.append(data)

    fresh_msg = DummyFreshMsg()

    class DummyClient:
        def __init__(self):
            self._fresh_msg = fresh_msg

        def iter_messages(self, chat_id, limit=20):
            async def _gen():
                yield self._fresh_msg
            return _gen()

    event = DummyEvent()
    client = DummyClient()
    asyncio.run(zm.process_bet_on(client, event, ctx, {}))

    assert fresh_msg.clicked  # 使用回溯消息完成点击
    assert len(ctx.state.bet_sequence_log) == 1
    assert all("押注出错" not in msg for msg in sent_messages)


def test_process_bet_on_prediction_timeout_pauses_and_skips_bet(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4004"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "超时回退用户"},
            "telegram": {"user_id": 4004},
            "groups": {"admin_chat": 4004},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["initial_amount"] = 500
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0
    ctx.state.history = [0, 1] * 20

    async def fake_predict(user_ctx, global_cfg):
        raise asyncio.TimeoutError("predict timeout")

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=1)

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 200
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {"betting": {"predict_timeout_sec": 2}}))

    assert not event.clicks
    assert "预测超时 - 本局不下注" in rt.get("last_predict_info", "")
    assert len(ctx.state.bet_sequence_log) == 0
    assert rt.get("stop_count") == 2
    assert any("模型可用性门控（超时）" in m for m in sent_messages)


def test_process_bet_on_prediction_timeout_gate_dedup_same_snapshot(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4005"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "超时去重用户"},
            "telegram": {"user_id": 4005},
            "groups": {"admin_chat": 4005},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["initial_amount"] = 500
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0
    ctx.state.history = [0, 1] * 20

    async def fake_predict(user_ctx, global_cfg):
        raise asyncio.TimeoutError("predict timeout")

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=len(sent_messages))

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 205
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {"betting": {"predict_timeout_sec": 2}}))
    # 模拟倒计时走完但仍无新结算快照（同一连押阶段）。
    rt["stop_count"] = 0
    rt["bet_on"] = True
    rt["mode_stop"] = True
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {"betting": {"predict_timeout_sec": 2}}))

    timeout_msgs = [m for m in sent_messages if "触发类型：模型可用性门控（超时）" in m]
    assert len(timeout_msgs) == 1


def test_process_bet_on_forces_unlock_after_repeated_skip_same_sequence(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4016"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "防卡死用户"},
            "telegram": {"user_id": 4016},
            "groups": {"admin_chat": 4016},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["initial_amount"] = 500
    rt["bet_amount"] = 500
    rt["lose_count"] = 4
    rt["bet_sequence_count"] = 4  # 下一手=5
    rt["win_count"] = 0
    rt["stall_guard_sequence"] = 5
    rt["stall_guard_no_bet_streak"] = 2
    rt["stall_guard_skip_streak"] = 2
    rt["stall_guard_timeout_streak"] = 0
    rt["stall_guard_gate_streak"] = 0
    rt["stall_guard_last_history_len"] = 39
    ctx.state.history = [0, 1] * 20

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_source"] = "model_skip"
        user_ctx.state.runtime["last_predict_tag"] = "CHAOS_SWITCH"
        user_ctx.state.runtime["last_predict_confidence"] = 35
        user_ctx.state.runtime["last_predict_info"] = "M-SMP/CHAOS_SWITCH | 观望"
        return -1

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=len(sent_messages))

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 206
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert event.clicks
    assert len(ctx.state.bet_sequence_log) == 1
    assert rt.get("stall_guard_force_unlock_total", 0) >= 1
    assert any("防卡死解锁已触发" in m for m in sent_messages)


def test_process_bet_on_step3_quality_gate_blocks_low_confidence(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4013"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "三手门控用户"},
            "telegram": {"user_id": 4013},
            "groups": {"admin_chat": 4013},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["bet_sequence_count"] = 2  # 下一手=3
    rt["bet_amount"] = 5_000
    rt["lose_count"] = 2
    rt["win_count"] = 0
    ctx.state.history = [0, 1] * 25

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_source"] = "model"
        user_ctx.state.runtime["last_predict_tag"] = "CHAOS_SWITCH"
        user_ctx.state.runtime["last_predict_confidence"] = 67
        user_ctx.state.runtime["last_predict_info"] = "M-SMP/CHAOS_SWITCH | 信:67%"
        return 1

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=len(sent_messages))

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 300
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert not event.clicks
    assert len(ctx.state.bet_sequence_log) == 0
    assert rt.get("stop_count") == 3  # 暂停2局，内部计数=2+1
    assert any("第3手质量门控" in m for m in sent_messages)


def test_process_bet_on_step3_quality_gate_skipped_when_deep_risk_off(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5033"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "三手门控关闭用户"},
            "telegram": {"user_id": 5033},
            "groups": {"admin_chat": 5033},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["bet_sequence_count"] = 2  # 下一手=3
    rt["bet_amount"] = 5_000
    rt["lose_count"] = 2
    rt["win_count"] = 0
    rt["risk_deep_enabled"] = False
    ctx.state.history = [0, 1] * 25
    sent_messages = []

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_source"] = "model"
        user_ctx.state.runtime["last_predict_tag"] = "DRAGON_CANDIDATE"
        user_ctx.state.runtime["last_predict_confidence"] = 65
        user_ctx.state.runtime["last_predict_info"] = "M-SMP/DRAGON_CANDIDATE | 信:65%"
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=len(sent_messages))

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 302
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert event.clicks
    assert len(ctx.state.bet_sequence_log) == 1
    assert rt.get("stop_count", 0) == 0
    assert not any("第3手质量门控" in m for m in sent_messages)


def test_process_bet_on_step4_quality_gate_blocks_non_whitelisted_tag(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4014"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "四手门控用户"},
            "telegram": {"user_id": 4014},
            "groups": {"admin_chat": 4014},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["bet_sequence_count"] = 3  # 下一手=4
    rt["bet_amount"] = 14_000
    rt["lose_count"] = 3
    rt["win_count"] = 0
    ctx.state.history = [0, 1] * 25
    ctx.state.bet_sequence_log = [{"result": "赢"} for _ in range(40)]

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_source"] = "model"
        user_ctx.state.runtime["last_predict_tag"] = "LONG_DRAGON"
        user_ctx.state.runtime["last_predict_confidence"] = 86
        user_ctx.state.runtime["last_predict_info"] = "M-SMP/LONG_DRAGON | 信:86%"
        return 1

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=len(sent_messages))

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 301
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert not event.clicks
    assert len(ctx.state.bet_sequence_log) == 40
    assert rt.get("stop_count") == 4  # 暂停3局，内部计数=3+1
    assert any("第4手强风控门控" in m for m in sent_messages)


def test_process_bet_on_timeout_gate_skipped_when_deep_risk_off(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5034"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "超时门控关闭用户"},
            "telegram": {"user_id": 5034},
            "groups": {"admin_chat": 5034},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["initial_amount"] = 500
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0
    rt["risk_deep_enabled"] = False
    ctx.state.history = [0, 1] * 20
    sent_messages = []

    async def fake_predict(user_ctx, global_cfg):
        raise asyncio.TimeoutError("predict timeout")

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=len(sent_messages))

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 303
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {"betting": {"predict_timeout_sec": 2}}))

    assert event.clicks
    assert len(ctx.state.bet_sequence_log) == 1
    assert rt.get("stop_count", 0) == 0
    assert not any("模型可用性门控（超时）" in m for m in sent_messages)


def test_user_context_migrates_legacy_state_when_history_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    legacy_user_id = 500099

    user_dir = tmp_path / "users" / "xu"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "迁移用户"},
            "telegram": {"user_id": legacy_user_id},
        },
    )
    _write_json(user_dir / "state.json", {"history": [], "runtime": {}})
    (tmp_path / "config.py").write_text(f"user = {legacy_user_id}\n", encoding="utf-8")

    legacy_state = {
        "history": [0, 1] * 30,  # 60条
        "bet_type_history": [0, 1] * 30,
        "predictions": [1, 0] * 30,
        "bet_sequence_log": [],
        "state": {"current_model_id": "qwen3-coder-plus", "bet_amount": 500},
    }
    _write_json(tmp_path / "state.json", legacy_state)

    ctx = UserContext(str(user_dir))
    assert len(ctx.state.history) >= 40


def test_send_message_v2_routes_and_account_prefix(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "路由用户"},
            "telegram": {"user_id": 5001},
            "groups": {"admin_chat": 5001},
            "notification": {
                "iyuu": {"enable": True, "url": "https://iyuu.test/send"},
                "tg_bot": {"enable": True, "bot_token": "token", "chat_id": "chat"},
            },
        },
    )
    ctx = UserContext(str(user_dir))

    requests_payloads = []

    def fake_post(url, data=None, json=None, timeout=5):
        requests_payloads.append({"url": url, "data": data, "json": json})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(zm.requests, "post", fake_post)

    class DummyClient:
        def __init__(self):
            self.messages = []

        async def send_message(self, target, message, parse_mode=None):
            self.messages.append((target, message))
            return SimpleNamespace(chat_id=target, id=7)

    client = DummyClient()
    asyncio.run(
        zm.send_message_v2(
            client,
            "lose_streak",
            "【账号：路由用户】\n测试告警",
            ctx,
            {},
            title="标题",
            desp="测试告警",
        )
    )

    assert client.messages == [(5001, "测试告警")]
    assert len(requests_payloads) == 2
    iyuu_payload = next(item for item in requests_payloads if "iyuu" in item["url"])
    tg_payload = next(item for item in requests_payloads if "api.telegram.org" in item["url"])
    assert iyuu_payload["data"]["desp"].startswith("【账号：路由用户】")
    assert tg_payload["json"]["text"].startswith("【账号：路由用户】")


def test_send_message_v2_lose_end_priority_keeps_account_prefix(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5011"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "回补用户"},
            "telegram": {"user_id": 5011},
            "groups": {"admin_chat": 5011},
            "notification": {
                "iyuu": {"enable": True, "url": "https://iyuu.test/send"},
                "tg_bot": {"enable": True, "bot_token": "token", "chat_id": "chat"},
            },
        },
    )
    ctx = UserContext(str(user_dir))

    requests_payloads = []

    def fake_post(url, data=None, json=None, timeout=5):
        requests_payloads.append({"url": url, "data": data, "json": json})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(zm.requests, "post", fake_post)

    class DummyClient:
        def __init__(self):
            self.messages = []

        async def send_message(self, target, message, parse_mode=None):
            self.messages.append((target, message))
            return SimpleNamespace(chat_id=target, id=8)

    client = DummyClient()
    asyncio.run(
        zm.send_message_v2(
            client,
            "lose_end",
            "【账号：回补用户】\n连输已终止",
            ctx,
            {},
            title="标题",
            desp="连输已终止",
        )
    )

    # 管理员通道不带账号前缀
    assert client.messages == [(5011, "连输已终止")]
    # 重点通道必须带账号前缀
    iyuu_payload = next(item for item in requests_payloads if "iyuu" in item["url"])
    tg_payload = next(item for item in requests_payloads if "api.telegram.org" in item["url"])
    assert iyuu_payload["data"]["desp"].startswith("【账号：回补用户】")
    assert tg_payload["json"]["text"].startswith("【账号：回补用户】")


def test_build_yc_result_message_uses_codeblock_table():
    params = {
        "continuous": 1,
        "lose_stop": 13,
        "lose_once": 3.0,
        "lose_twice": 2.1,
        "lose_three": 2.1,
        "lose_four": 2.05,
        "initial_amount": 3000,
    }
    msg = zm._build_yc_result_message(params, "yc_demo", current_fund=30_000_000, auto_trigger=False)

    assert msg.startswith("```")
    assert "🎯 策略参数" in msg
    assert "策略命令: 1 13 3.0 2.1 2.1 2.05 3000" in msg
    assert "🎯 策略总结:" in msg
    assert "资金最多连数:" in msg
    assert "连数|倍率|下注| 盈利 |所需本金" in msg
    assert " 15|" in msg
    assert msg.count("```") == 2


def test_process_settle_no_longer_auto_sends_ydx(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5002"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "结算用户"},
            "telegram": {"user_id": 5002},
            "groups": {"admin_chat": 5002, "zq_group": [101, 102]},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.runtime["open_ydx"] = True
    ctx.state.runtime["bet"] = False

    async def fake_fetch_balance(user_ctx):
        return 123456

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5002, id=99)

    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    class DummyClient:
        def __init__(self):
            self.sent = []

        async def send_message(self, target, message, parse_mode=None):
            self.sent.append((target, message))
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(message=SimpleNamespace(message="已结算: 结果为 8 小"))
    client = DummyClient()
    asyncio.run(zm.process_settle(client, event, ctx, {}))

    ydx_messages = [msg for msg in client.sent if msg[1] == "/ydx"]
    assert ydx_messages == []
    assert ctx.state.history[-1] == 0


def test_check_bet_status_can_resume_when_fund_sufficient(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5003"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "恢复用户"},
            "telegram": {"user_id": 5003},
            "groups": {"admin_chat": 5003},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["stop_count"] = 0
    rt["bet"] = False
    rt["gambling_fund"] = 2_000_000
    rt["bet_amount"] = 0
    rt["lose_count"] = 0
    rt["win_count"] = 0

    sent = {}

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent["message"] = message
        return SimpleNamespace(chat_id=5003, id=1)

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    asyncio.run(zm.check_bet_status(SimpleNamespace(), ctx, {}))

    # 恢复可下注状态时不应提前标记为“已下注”，避免结算时序误判。
    assert rt["bet"] is False
    assert rt["pause_count"] == 0
    assert "恢复可下注状态" in sent["message"]


def test_pause_command_sets_manual_pause_and_blocks_bet_on(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5005"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "暂停用户"},
            "telegram": {"user_id": 5005},
            "groups": {"admin_chat": 5005},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5005, id=1)

    async def fake_sleep(*args, **kwargs):
        return None

    async def fail_predict(*args, **kwargs):
        raise AssertionError("predict should not run while manual pause is active")

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm, "predict_next_bet_v10", fail_predict)

    cmd_event = SimpleNamespace(raw_text="pause", chat_id=5005, id=10)
    asyncio.run(zm.process_user_command(SimpleNamespace(), cmd_event, ctx, {}))

    assert rt["manual_pause"] is True
    assert rt["bet_on"] is False
    assert rt["bet"] is False

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 5005
            self.id = 11

    asyncio.run(zm.process_bet_on(SimpleNamespace(), DummyEvent(), ctx, {}))


def test_risk_command_can_toggle_base_and_deep_switches(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5030"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "风控开关用户"},
            "telegram": {"user_id": 5030},
            "groups": {"admin_chat": 5030},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5030, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    asyncio.run(zm.process_user_command(SimpleNamespace(), SimpleNamespace(raw_text="risk deep off", chat_id=5030, id=1), ctx, {}))
    assert rt["risk_deep_enabled"] is False
    assert rt["risk_deep_default_enabled"] is False
    assert rt["risk_base_enabled"] is True

    asyncio.run(zm.process_user_command(SimpleNamespace(), SimpleNamespace(raw_text="risk base off", chat_id=5030, id=2), ctx, {}))
    assert rt["risk_base_enabled"] is False
    assert rt["risk_base_default_enabled"] is False

    asyncio.run(zm.process_user_command(SimpleNamespace(), SimpleNamespace(raw_text="risk all on", chat_id=5030, id=3), ctx, {}))
    assert rt["risk_base_enabled"] is True
    assert rt["risk_deep_enabled"] is True
    assert rt["risk_base_default_enabled"] is True
    assert rt["risk_deep_default_enabled"] is True
    assert any("当前风控开关" in msg for msg in sent_messages)


def test_apply_account_risk_default_mode_resets_current_switches():
    rt = {
        "risk_base_enabled": True,
        "risk_deep_enabled": False,
        "risk_base_default_enabled": False,
        "risk_deep_default_enabled": True,
    }

    result = zm.apply_account_risk_default_mode(rt)

    assert result["base_enabled"] is False
    assert result["deep_enabled"] is True
    assert rt["risk_base_enabled"] is False
    assert rt["risk_deep_enabled"] is True


def test_build_startup_focus_reminder_contains_risk_and_preset_guidance(tmp_path):
    user_dir = tmp_path / "users" / "startup_notice_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "提醒用户"},
            "telegram": {"user_id": 6130},
            "groups": {"admin_chat": 6130},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["current_preset_name"] = "yc05"
    rt["risk_base_enabled"] = True
    rt["risk_deep_enabled"] = False
    rt["risk_base_default_enabled"] = True
    rt["risk_deep_default_enabled"] = False

    msg = zm.build_startup_focus_reminder(ctx)

    assert "启动重点设置提醒" in msg
    assert "风控提醒" in msg
    assert "st <预设名>" in msg
    assert "help" in msg


def test_check_bet_status_does_not_resume_when_manual_pause(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5006"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "手动暂停用户"},
            "telegram": {"user_id": 5006},
            "groups": {"admin_chat": 5006},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["manual_pause"] = True
    rt["stop_count"] = 0
    rt["bet"] = False
    rt["gambling_fund"] = 2_000_000
    rt["bet_amount"] = 0
    rt["lose_count"] = 0
    rt["win_count"] = 0

    sent = {"called": False}

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent["called"] = True
        return SimpleNamespace(chat_id=5006, id=1)

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    asyncio.run(zm.check_bet_status(SimpleNamespace(), ctx, {}))

    assert rt["bet"] is False
    assert sent["called"] is False


def test_process_settle_lose_warning_matches_master_style(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5004"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "告警用户"},
            "telegram": {"user_id": 5004},
            "groups": {"admin_chat": 5004},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 0  # 押小
    rt["bet_amount"] = 500
    rt["warning_lose_count"] = 1
    rt["bet_sequence_count"] = 1
    rt["account_balance"] = 10_000_000
    rt["gambling_fund"] = 9_000_000
    rt["current_round"] = 1
    rt["current_bet_seq"] = 2
    rt["current_preset_name"] = "yc10"
    ctx.state.bet_sequence_log = [{"bet_id": "20260223_1_1", "profit": None}]

    captured = {}

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        captured["type"] = msg_type
        captured["message"] = message
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5004, id=12)

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert captured["type"] == "lose_streak"
    assert "⚠️⚠️  1 连输告警 ⚠️⚠️" in captured["message"]
    assert "第 1 轮第 1 次" in captured["message"]
    assert "📋 预设名称：yc10" in captured["message"]
    assert "💰 账户余额：" in captured["message"]
    assert "🤖 当局 AI 预测提示" not in captured["message"]


def test_process_settle_lose_end_message_contains_balance_lines(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5007"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "回补用户"},
            "telegram": {"user_id": 5007},
            "groups": {"admin_chat": 5007},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1  # 押大，下面开大 -> 赢
    rt["bet_amount"] = 1000
    rt["warning_lose_count"] = 1
    rt["lose_count"] = 3
    rt["lose_notify_pending"] = True
    rt["lose_start_info"] = {"round": 1, "seq": 5, "fund": 24_566_390}
    rt["current_round"] = 1
    rt["current_bet_seq"] = 10
    rt["current_preset_name"] = "yc10"
    rt["account_balance"] = 24_634_900
    rt["gambling_fund"] = 24_567_390
    ctx.state.bet_sequence_log = [{"bet_id": "20260224_1_9", "profit": None}]

    captured = {}

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        if msg_type == "lose_end":
            captured["message"] = message
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5007, id=1)

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    msg = captured["message"]
    assert "✅✅  3 连输已终止！✅✅" in msg
    assert "🔢 " in msg and "第 1 轮第 5 次 至 第 9 次" in msg
    assert "📋 预设名称：yc10" in msg
    assert "😀 连续押注：4 次" in msg
    assert "⚠️本局连输： 3 次" in msg
    assert "💰 本局盈利： 1,990" in msg
    assert "💰 账户余额：2463.49 万" in msg
    assert "💰 菠菜资金剩余：2456.84 万" in msg


def test_process_settle_skips_stale_lose_end_when_old_lose_count_zero(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5022"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "连输脏状态用户"},
            "telegram": {"user_id": 5022},
            "groups": {"admin_chat": 5022},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1  # 押大，下面开大 -> 赢
    rt["bet_amount"] = 1000
    rt["warning_lose_count"] = 3
    rt["lose_count"] = 0
    rt["lose_notify_pending"] = True
    rt["lose_start_info"] = {"round": 2, "seq": 56, "fund": 9_999_999}
    rt["current_round"] = 1
    rt["current_bet_seq"] = 1
    rt["current_preset_name"] = "yc05"
    rt["account_balance"] = 315_300
    rt["gambling_fund"] = 314_800
    ctx.state.bet_sequence_log = [{"bet_id": "20260228_1_1", "profit": None}]

    sent_types = []
    sent_msgs = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        sent_types.append(msg_type)
        sent_msgs.append(message)
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_msgs.append(message)
        return SimpleNamespace(chat_id=5022, id=len(sent_msgs))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=45001, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert "lose_end" not in sent_types
    assert not any("0 连输已终止" in m for m in sent_msgs)
    assert rt["lose_notify_pending"] is False
    assert rt["lose_start_info"] == {}


def test_process_settle_skips_lose_end_when_range_is_invalid(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5023"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "连输区间异常用户"},
            "telegram": {"user_id": 5023},
            "groups": {"admin_chat": 5023},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1  # 押大，下面开大 -> 赢
    rt["bet_amount"] = 1000
    rt["warning_lose_count"] = 3
    rt["lose_count"] = 3
    rt["lose_notify_pending"] = True
    rt["lose_start_info"] = {"round": 2, "seq": 56, "fund": 9_999_999}
    rt["current_round"] = 1
    rt["current_bet_seq"] = 1
    rt["current_preset_name"] = "yc05"
    rt["account_balance"] = 315_300
    rt["gambling_fund"] = 314_800
    ctx.state.bet_sequence_log = [{"bet_id": "20260228_1_1", "profit": None}]

    sent_types = []
    sent_msgs = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        sent_types.append(msg_type)
        sent_msgs.append(message)
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_msgs.append(message)
        return SimpleNamespace(chat_id=5023, id=len(sent_msgs))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=45002, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert "lose_end" not in sent_types
    assert not any("连输已终止" in m for m in sent_msgs)
    assert rt["lose_notify_pending"] is False
    assert rt["lose_start_info"] == {}


def test_reconcile_bet_runtime_from_log_ignores_stale_pending_entries(tmp_path):
    user_dir = tmp_path / "users" / "5091"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "脏挂单修复用户"},
            "telegram": {"user_id": 5091},
            "groups": {"admin_chat": 5091},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["initial_amount"] = 50_000
    rt["bet"] = False
    rt["bet_sequence_count"] = 6
    rt["lose_count"] = 5
    rt["bet_amount"] = 2_145_500
    rt["lose_once"] = 2.1
    rt["lose_twice"] = 2.1
    rt["lose_three"] = 2.1
    rt["lose_four"] = 2.05
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260312_1_25", "amount": 106_000, "result": "赢", "profit": 104_940},
        {"bet_id": "20260312_1_26", "amount": 50_000, "result": "输", "profit": -50_000},
        {"bet_id": "20260312_1_27", "amount": 106_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_28", "amount": 225_000, "result": "输", "profit": -225_000},
        {"bet_id": "20260312_1_29", "amount": 477_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_30", "amount": 1_011_500, "result": None, "profit": 0},
    ]

    healed = zm.heal_stale_pending_bets(ctx)
    summary = zm.reconcile_bet_runtime_from_log(ctx)

    assert healed["count"] == 3
    assert summary["continuous_count"] == 2
    assert summary["lose_count"] == 2
    assert rt["bet_sequence_count"] == 2
    assert rt["lose_count"] == 2
    assert rt["bet_amount"] == 225_000
    assert zm.calculate_bet_amount(rt) == 477_000


def test_build_pending_bet_heal_notice_contains_reconciled_status():
    rt = {
        "initial_amount": 50_000,
        "bet_sequence_count": 2,
        "lose_count": 2,
        "bet_amount": 225_000,
        "lose_once": 2.1,
        "lose_twice": 2.1,
        "lose_three": 2.1,
        "lose_four": 2.05,
    }
    notice = zm.build_pending_bet_heal_notice(
        {"count": 3, "items": ["20260312_1_27", "20260312_1_29", "20260312_1_30"]},
        {"continuous_count": 2, "lose_count": 2},
        rt,
    )

    assert "已修正历史异常挂单" in notice
    assert "修复条数：3" in notice
    assert "当前连续押注：2 次" in notice
    assert "当前连输：2 次" in notice
    assert "下一手预计下注：477,000" in notice


def test_process_bet_on_sends_heal_notice_when_stale_pending_entries_are_fixed(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5094"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "自愈提示用户"},
            "telegram": {"user_id": 5094},
            "groups": {"admin_chat": 5094},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = False
    rt["bet_on"] = True
    rt["stop_count"] = 2
    rt["initial_amount"] = 50_000
    rt["bet_sequence_count"] = 6
    rt["lose_count"] = 5
    rt["bet_amount"] = 2_145_500
    rt["lose_once"] = 2.1
    rt["lose_twice"] = 2.1
    rt["lose_three"] = 2.1
    rt["lose_four"] = 2.05
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260312_1_25", "amount": 106_000, "result": "赢", "profit": 104_940},
        {"bet_id": "20260312_1_26", "amount": 50_000, "result": "输", "profit": -50_000},
        {"bet_id": "20260312_1_27", "amount": 106_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_28", "amount": 225_000, "result": "输", "profit": -225_000},
        {"bet_id": "20260312_1_29", "amount": 477_000, "result": None, "profit": 0},
    ]

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5094, id=len(sent_messages))

    async def fake_refresh_pause_countdown_notice(client, user_ctx, global_cfg, remaining_rounds):
        return None

    async def fake_clear_pause_countdown_notice(client, user_ctx):
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "_refresh_pause_countdown_notice", fake_refresh_pause_countdown_notice)
    monkeypatch.setattr(zm, "_clear_pause_countdown_notice", fake_clear_pause_countdown_notice)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(
        id=9902,
        chat_id=5094,
        reply_markup=None,
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(DummyClient(), event, ctx, {}))

    assert any("已修正历史异常挂单" in message for message in sent_messages)
    assert any("当前连续押注：2 次" in message for message in sent_messages)
    assert any("下一手预计下注：477,000" in message for message in sent_messages)
    assert rt["stop_count"] == 1
    assert rt["bet_sequence_count"] == 2
    assert rt["lose_count"] == 2


def test_process_bet_on_skips_duplicate_trigger_when_previous_bet_pending(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5092"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "重复下注保护用户"},
            "telegram": {"user_id": 5092},
            "groups": {"admin_chat": 5092},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = True
    rt["bet_on"] = True
    ctx.state.history = [0, 1] * 30
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260312_1_28", "sequence": 2, "direction": "small", "amount": 225_000, "result": None, "profit": 0}
    ]

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5092, id=1)

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(
        id=9901,
        chat_id=5092,
        reply_markup=None,
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(DummyClient(), event, ctx, {}))

    assert len(ctx.state.bet_sequence_log) == 1
    assert ctx.state.bet_sequence_log[0]["bet_id"] == "20260312_1_28"
    assert ctx.state.bet_sequence_log[0]["result"] is None
    assert rt["bet"] is True
    assert sent_messages == []


def test_process_settle_warn_message_uses_real_settled_chain_count(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5093"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "连押纠偏用户"},
            "telegram": {"user_id": 5093},
            "groups": {"admin_chat": 5093},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 0  # 押小，下面开大 -> 输
    rt["bet_amount"] = 2_145_500
    rt["bet_sequence_count"] = 6
    rt["lose_count"] = 2
    rt["warning_lose_count"] = 3
    rt["current_round"] = 1
    rt["current_bet_seq"] = 32
    rt["current_preset_name"] = "yc50000"
    rt["account_balance"] = 14_508_552
    rt["gambling_fund"] = 6_187_734
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260312_1_25", "amount": 106_000, "result": "赢", "profit": 104_940},
        {"bet_id": "20260312_1_26", "amount": 50_000, "result": "输", "profit": -50_000},
        {"bet_id": "20260312_1_27", "amount": 106_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_28", "amount": 225_000, "result": "输", "profit": -225_000},
        {"bet_id": "20260312_1_29", "amount": 477_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_30", "amount": 1_011_500, "result": None, "profit": 0},
        {"bet_id": "20260312_1_31", "amount": 2_145_500, "result": None, "profit": 0},
    ]

    captured = {}

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        if msg_type == "lose_streak":
            captured["message"] = message
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5093, id=1)

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=45031, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    msg = captured["message"]
    assert "⚠️⚠️  3 连输告警 ⚠️⚠️" in msg
    assert "😀 连续押注：3 次" in msg
    assert "💰 累计损失：2,420,500" in msg
    assert rt["bet_sequence_count"] == 3
    assert rt["lose_count"] == 3


def test_process_settle_profit_pause_does_not_immediately_resume(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5014"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "盈利暂停用户"},
            "telegram": {"user_id": 5014},
            "groups": {"admin_chat": 5014},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1  # 押大，下面开大 -> 赢
    rt["bet_amount"] = 10_000
    rt["period_profit"] = 95_000
    rt["profit"] = 100_000
    rt["profit_stop"] = 2
    rt["flag"] = True
    rt["current_round"] = 1
    rt["current_bet_seq"] = 3
    rt["account_balance"] = 10_000_000
    rt["gambling_fund"] = 9_000_000
    ctx.state.bet_sequence_log = [{"bet_id": "20260227_1_3", "profit": None}]

    sent_messages = []
    routed_messages = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        routed_messages.append((msg_type, message))
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5014, id=len(sent_messages))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=41001, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert any(msg_type == "goal_pause" and "原因：盈利达成" in m for msg_type, m in routed_messages)
    assert any("暂停倒计时提醒" in m for m in sent_messages)
    assert not any(m.startswith("**恢复押注**") for m in sent_messages)
    assert rt["stop_count"] == 3  # profit_stop=2, 内部计数应为3
    assert rt["pause_countdown_active"] is True
    assert rt["pause_countdown_total_rounds"] == 2
    assert rt["period_profit"] == 0
    assert rt["current_round"] == 2
    assert rt["bet"] is False
    assert rt["bet_on"] is False


def test_process_bet_on_pause_countdown_refreshes_while_paused(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5017"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "倒计时用户"},
            "telegram": {"user_id": 5017},
            "groups": {"admin_chat": 5017},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["manual_pause"] = False
    rt["stop_count"] = 3
    rt["pause_countdown_active"] = True
    rt["pause_countdown_reason"] = "基础风控暂停"
    rt["pause_countdown_total_rounds"] = 2
    rt["pause_countdown_last_remaining"] = -1

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5017, id=len(sent_messages))

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    event = SimpleNamespace(reply_markup=object(), message=SimpleNamespace(message="unused"))
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["stop_count"] == 2
    assert any("暂停倒计时提醒" in m for m in sent_messages)
    assert any("倒计时：1 局" in m for m in sent_messages)
    assert not any(m.startswith("**恢复押注**") for m in sent_messages)


def test_process_bet_on_pause_countdown_clears_on_resume(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5018"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "倒计时恢复用户"},
            "telegram": {"user_id": 5018},
            "groups": {"admin_chat": 5018},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["manual_pause"] = False
    rt["stop_count"] = 1
    rt["pause_countdown_active"] = True
    rt["pause_countdown_reason"] = "盈利达成暂停"
    rt["pause_countdown_total_rounds"] = 1
    rt["pause_countdown_last_remaining"] = 1

    class DummyMsg:
        chat_id = 5018
        id = 99

        async def delete(self):
            return None

    ctx.pause_countdown_message = DummyMsg()
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5018, id=len(sent_messages))

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_source"] = "model"
        user_ctx.state.runtime["last_predict_tag"] = "DRAGON_CANDIDATE"
        user_ctx.state.runtime["last_predict_confidence"] = 90
        return 1

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)

    class DummyEvent:
        def __init__(self):
            self.reply_markup = object()
            self.message = SimpleNamespace(message="unused")
            self.chat_id = 5018
            self.id = 5018001

        async def click(self, _):
            return None

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["stop_count"] == 0
    assert rt["pause_countdown_active"] is False
    assert ctx.pause_countdown_message is None
    assert any("恢复押注（已执行）" in m for m in sent_messages)


def test_process_bet_on_insufficient_fund_sends_pause_notice_even_without_pending_bet(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5019"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "资金不足用户"},
            "telegram": {"user_id": 5019},
            "groups": {"admin_chat": 5019},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["manual_pause"] = False
    rt["stop_count"] = 0
    rt["bet"] = False
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["initial_amount"] = 500
    rt["lose_count"] = 0
    rt["gambling_fund"] = 100

    sent_messages = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        if msg_type == "fund_pause":
            sent_messages.append(message)
        return None

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)

    event = SimpleNamespace(reply_markup=object(), message=SimpleNamespace(message="unused"))
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert any("菠菜资金不足，已暂停押注" in m for m in sent_messages)
    assert rt["fund_pause_notified"] is True
    assert rt["bet"] is False
    assert rt["bet_on"] is False


def test_check_bet_status_does_not_resume_when_next_bet_amount_is_zero(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5020"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "上限暂停用户"},
            "telegram": {"user_id": 5020},
            "groups": {"admin_chat": 5020},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["manual_pause"] = False
    rt["bet"] = False
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["initial_amount"] = 10000
    rt["bet_amount"] = 10000
    rt["lose_stop"] = 3
    rt["lose_count"] = 3  # 下一手将超过上限，calculate_bet_amount 返回0
    rt["gambling_fund"] = 10_000_000

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5020, id=len(sent_messages))

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    asyncio.run(zm.check_bet_status(SimpleNamespace(), ctx, {}))

    assert any("已达到预设连投上限" in m for m in sent_messages)
    assert rt["limit_stop_notified"] is True
    assert rt["bet"] is False
    assert rt["bet_on"] is False
    assert not any("押注已恢复" in m for m in sent_messages)


def test_process_settle_syncs_fund_from_balance_before_next_bet_check(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5021"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "结算资金不足用户"},
            "telegram": {"user_id": 5021},
            "groups": {"admin_chat": 5021},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 0  # 押小，下面开大 -> 输
    rt["bet_amount"] = 730000
    rt["lose_count"] = 4
    rt["lose_stop"] = 9
    rt["lose_four"] = 2.05
    rt["current_round"] = 1
    rt["current_bet_seq"] = 9
    rt["account_balance"] = 2_200_000
    rt["gambling_fund"] = 1_417_800
    rt["fund_pause_notified"] = True
    ctx.state.bet_sequence_log = [{"bet_id": "20260228_1_9", "profit": None}]

    sent_messages = []

    async def fake_send_message_v2(*args, **kwargs):
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5021, id=len(sent_messages))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=44001, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert rt["gambling_fund"] == 2_200_000
    assert rt["fund_pause_notified"] is False
    assert rt["bet_on"] is False
    assert rt["mode_stop"] is True
    assert not any("菠菜资金不足，已暂停押注" in m for m in sent_messages)


def test_process_settle_keeps_pending_bet_settlement_before_fund_pause(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5022"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "结算时序用户"},
            "telegram": {"user_id": 5022},
            "groups": {"admin_chat": 5022},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 0  # 押小，开大 -> 输
    rt["bet_amount"] = 1_322_000
    rt["bet_sequence_count"] = 8
    rt["lose_count"] = 7
    rt["lose_stop"] = 12
    rt["lose_four"] = 2.05
    rt["current_round"] = 1
    rt["current_bet_seq"] = 90
    rt["account_balance"] = 1_334_559
    rt["gambling_fund"] = 1_334_559
    ctx.state.bet_sequence_log = [{"bet_id": "20260302_1_90", "result": None, "profit": 0}]

    sent_messages = []
    sent_types = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        sent_types.append(msg_type)
        sent_messages.append(message)
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5022, id=len(sent_messages))

    async def fake_fetch_balance(user_ctx):
        # 模拟远端余额已变化（比如该笔下注已在平台侧扣减）
        return 12_559

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=44002, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert ctx.state.bet_sequence_log[-1]["result"] == "输"
    assert ctx.state.bet_sequence_log[-1]["profit"] == -1_322_000
    assert rt["gambling_fund"] == 12_559
    assert rt["bet"] is False
    assert "fund_pause" in sent_types
    assert any("菠菜资金不足，已暂停押注" in m for m in sent_messages)


def test_process_settle_only_consumes_pending_bet_once(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5015"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "单次结算用户"},
            "telegram": {"user_id": 5015},
            "groups": {"admin_chat": 5015},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1
    rt["bet_amount"] = 1_000
    rt["current_round"] = 1
    rt["current_bet_seq"] = 1
    rt["account_balance"] = 10_000_000
    rt["gambling_fund"] = 9_000_000
    ctx.state.bet_sequence_log = [{"bet_id": "20260227_1_1", "profit": None}]

    sent_messages = []

    async def fake_send_message_v2(*args, **kwargs):
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5015, id=len(sent_messages))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event1 = SimpleNamespace(id=42001, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event1, ctx, {}))
    first_result_msgs = [m for m in sent_messages if "押注结果" in m]
    assert len(first_result_msgs) == 1
    assert rt["bet"] is False

    sent_messages.clear()
    event2 = SimpleNamespace(id=42002, message=SimpleNamespace(message="已结算: 结果为 8 小"))
    asyncio.run(zm.process_settle(DummyClient(), event2, ctx, {}))
    second_result_msgs = [m for m in sent_messages if "押注结果" in m]
    assert len(second_result_msgs) == 0


def test_process_settle_triggers_deep_risk_pause_immediately_on_loss_milestone(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5016"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "深度风控用户"},
            "telegram": {"user_id": 5016},
            "groups": {"admin_chat": 5016},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1  # 押大，下面开小 -> 输
    rt["bet_amount"] = 1000
    rt["bet_sequence_count"] = 3
    rt["lose_count"] = 2
    rt["lose_stop"] = 20
    rt["current_preset_name"] = "yc10"
    rt["current_round"] = 1
    rt["current_bet_seq"] = 3
    rt["account_balance"] = 10_000_000
    rt["gambling_fund"] = 9_000_000
    rt["current_model_id"] = "qwen3-max"
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260227_1_1", "result": "赢", "profit": 990},
        {"bet_id": "20260227_1_2", "result": "输", "profit": -1000},
        {"bet_id": "20260227_1_3", "result": None, "profit": None},
    ]

    sent_messages = []

    async def fake_send_message_v2(*args, **kwargs):
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5016, id=len(sent_messages))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    async def fake_suggest_pause_rounds_by_model(user_ctx, risk_eval, max_pause):
        assert risk_eval.get("deep_milestone") == 3
        return 3, "测试建议", "model"

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)
    monkeypatch.setattr(zm, "_suggest_pause_rounds_by_model", fake_suggest_pause_rounds_by_model)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=43001, message=SimpleNamespace(message="已结算: 结果为 8 小"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    settle_idx = next(i for i, msg in enumerate(sent_messages) if "押注结果" in msg)
    pause_idx = next(i for i, msg in enumerate(sent_messages) if "触发层级：深度风控（3连输档）" in msg)
    assert pause_idx > settle_idx
    assert rt["stop_count"] == 4  # 模型建议暂停3局，内部计数为3+1
    assert rt["bet_on"] is False
    assert rt["bet"] is False
    assert 3 in rt.get("risk_deep_triggered_milestones", [])


def test_trigger_deep_risk_pause_skips_when_deep_switch_off(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5031"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "深度关闭用户"},
            "telegram": {"user_id": 5031},
            "groups": {"admin_chat": 5031},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["risk_deep_enabled"] = False
    rt["stop_count"] = 0

    async def fake_send_to_admin(*args, **kwargs):
        raise AssertionError("deep off 时不应发送深度风控暂停通知")

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    risk_eval = {
        "deep_trigger": True,
        "deep_milestone": 3,
        "deep_level_cap": 5,
        "wins": 12,
        "total": 40,
        "win_rate": 0.3,
        "reasons": ["连输达到3局档位（每3局触发）"],
    }

    triggered = asyncio.run(
        zm._trigger_deep_risk_pause_after_settle(
            SimpleNamespace(),
            ctx,
            {},
            risk_eval,
            next_sequence=4,
            settled_count=100,
        )
    )
    assert triggered is False
    assert rt["stop_count"] == 0


def test_trigger_deep_risk_pause_relaxes_cap_on_long_dragon(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5032"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "长龙放宽用户"},
            "telegram": {"user_id": 5032},
            "groups": {"admin_chat": 5032},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["risk_deep_enabled"] = True
    ctx.state.history = [1, 0, 1, 1, 1, 1, 1, 1]  # 尾部6连大
    sent_messages = []

    async def fake_suggest_pause_rounds_by_model(user_ctx, risk_eval, max_pause):
        assert max_pause == zm.RISK_DEEP_LONG_DRAGON_MAX_PAUSE_ROUNDS
        return 5, "模型建议", "model"

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5032, id=len(sent_messages))

    monkeypatch.setattr(zm, "_suggest_pause_rounds_by_model", fake_suggest_pause_rounds_by_model)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    risk_eval = {
        "deep_trigger": True,
        "deep_milestone": 3,
        "deep_level_cap": 5,
        "wins": 14,
        "total": 40,
        "win_rate": 0.35,
        "reasons": ["连输达到3局档位（每3局触发）"],
    }

    triggered = asyncio.run(
        zm._trigger_deep_risk_pause_after_settle(
            SimpleNamespace(),
            ctx,
            {},
            risk_eval,
            next_sequence=4,
            settled_count=200,
        )
    )
    assert triggered is True
    # 放宽后上限=2，最终暂停2局 => 内部 stop_count=2+1
    assert rt["stop_count"] == zm.RISK_DEEP_LONG_DRAGON_MAX_PAUSE_ROUNDS + 1
    assert any("本层暂停上限由 5 调整为 2" in msg for msg in sent_messages)


def test_format_dashboard_shows_software_version_and_preset_lines(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5013"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "仪表盘用户"},
            "telegram": {"user_id": 5013},
            "groups": {"admin_chat": 5013},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["current_preset_name"] = "yc10"
    rt["continuous"] = 1
    rt["lose_stop"] = 11
    rt["lose_once"] = 2.8
    rt["lose_twice"] = 2.3
    rt["lose_three"] = 2.2
    rt["lose_four"] = 2.05
    rt["initial_amount"] = 10000
    ctx.state.history = [1, 0] * 20

    monkeypatch.setattr(zm, "get_current_repo_info", lambda: {"current_tag": "v1.0.10", "nearest_tag": "v1.0.10", "short_commit": "abcd1234"})

    msg = zm.format_dashboard(ctx)
    assert "🔢 **软件版本：v1.0.10(abcd1234)**" in msg
    assert "📋 **预设名称：yc10**" in msg
    assert "🤖 **预设参数：1 11 2.8 2.3 2.2 2.05 10000**" in msg


def test_st_command_triggers_auto_yc_report(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5008"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "预设测算用户"},
            "telegram": {"user_id": 5008},
            "groups": {"admin_chat": 5008},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5008, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    cmd_event = SimpleNamespace(raw_text="st yc05", chat_id=5008, id=21)
    asyncio.run(zm.process_user_command(SimpleNamespace(), cmd_event, ctx, {}))

    assert ctx.state.runtime.get("current_preset_name") == "yc05"
    assert any("预设启动成功: yc05" in msg for msg in sent_messages)
    assert any("🔮 已根据当前预设自动测算" in msg for msg in sent_messages)
    assert any("🎯 策略参数" in msg for msg in sent_messages)
    assert any("连数|倍率|下注| 盈利 |所需本金" in msg for msg in sent_messages)


def test_xx_command_cleans_messages_in_config_groups(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5009"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "清理用户"},
            "telegram": {"user_id": 5009},
            "groups": {"admin_chat": 5009, "zq_group": [111, 222]},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))

    sent_messages = []
    deleted_calls = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5009, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    class DummyClient:
        def iter_messages(self, chat_id, from_user=None, limit=None):
            async def _gen():
                sample = {111: [1, 2, 3], 222: [10]}
                for msg_id in sample.get(chat_id, []):
                    yield SimpleNamespace(id=msg_id)

            return _gen()

        async def delete_messages(self, chat_id, message_ids):
            deleted_calls.append((chat_id, list(message_ids)))

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    cmd_event = SimpleNamespace(raw_text="xx", chat_id=5009, id=30)
    asyncio.run(zm.process_user_command(DummyClient(), cmd_event, ctx, {}))

    assert (111, [1, 2, 3]) in deleted_calls
    assert (222, [10]) in deleted_calls
    assert any("群组消息已清理" in msg for msg in sent_messages)
    assert any("删除消息：4" in msg for msg in sent_messages)


def test_process_red_packet_claim_success_sends_admin_notice(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5010"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "红包用户"},
            "telegram": {"user_id": 5010},
            "groups": {"admin_chat": 5010, "zq_bot": 9001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent = {}

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent["message"] = message
        return SimpleNamespace(chat_id=5010, id=1)

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)

    class DummyClient:
        async def __call__(self, request):
            return SimpleNamespace(message="已获得 88 灵石")

    class DummyButton:
        data = b"red-packet"

    class DummyRow:
        buttons = [DummyButton()]

    class DummyMarkup:
        rows = [DummyRow()]

    class DummyEvent:
        sender_id = 9001
        raw_text = "恭喜领取灵石红包"
        text = "恭喜领取灵石红包"
        chat_id = -10001
        id = 99
        reply_markup = DummyMarkup()

        def __init__(self):
            self.clicked = []

        async def click(self, *args):
            self.clicked.append(args)

    event = DummyEvent()
    asyncio.run(zm.process_red_packet(DummyClient(), event, ctx, {}))

    assert event.clicked
    assert sent.get("message") == "🎉 抢到红包88灵石！"


def test_process_red_packet_ignores_game_message(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5012"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "游戏过滤用户"},
            "telegram": {"user_id": 5012},
            "groups": {"admin_chat": 5012, "zq_bot": 9001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent = {"called": False}

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent["called"] = True
        return SimpleNamespace(chat_id=5012, id=1)

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    class DummyClient:
        async def __call__(self, request):
            raise AssertionError("游戏消息不应触发红包回调请求")

    class DummyButton:
        data = b"game-start"
        text = "开始游戏"

    class DummyRow:
        buttons = [DummyButton()]

    class DummyMarkup:
        rows = [DummyRow()]

    class DummyEvent:
        sender_id = 9001
        raw_text = "灵石对战游戏开始啦"
        text = "灵石对战游戏开始啦"
        chat_id = -10001
        id = 109
        reply_markup = DummyMarkup()

        def __init__(self):
            self.clicked = []

        async def click(self, *args):
            self.clicked.append(args)

    event = DummyEvent()
    asyncio.run(zm.process_red_packet(DummyClient(), event, ctx, {}))

    assert event.clicked == []
    assert sent["called"] is False
def test_compute_replay_linkage_coverage_only_counts_settled_records():
    settled_tokens = next(
        const
        for const in zm._compute_replay_linkage_coverage.__code__.co_consts[1].co_consts
        if isinstance(const, tuple) and len(const) >= 2 and all(isinstance(item, str) for item in const)
    )
    win_token = settled_tokens[0]
    lose_token = settled_tokens[1]
    abnormal_token = "abnormal"

    state = SimpleNamespace(
        bet_sequence_log=[
            {"bet_id": "b1", "result": win_token, "decision_id": "dec_1"},
            {"bet_id": "b2", "result": lose_token, "decision_id": ""},
            {"bet_id": "b3", "result": None, "decision_id": "dec_3"},
            {"bet_id": "b4", "result": abnormal_token, "decision_id": "dec_4"},
        ]
    )

    coverage = zm._compute_replay_linkage_coverage(state, limit=300)

    assert coverage["total"] == 2
    assert coverage["linked"] == 1
    assert coverage["coverage_pct"] == 50.0


def test_process_user_command_replay_outputs_focus_message(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "replay_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "复盘用户"},
            "telegram": {"user_id": 7011},
            "groups": {"admin_chat": 7011},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.bet_sequence_log = [
        {
            "bet_id": "b100",
            "sequence": 1,
            "direction": "big",
            "amount": 500,
            "result": "赢",
            "profit": 495,
            "decision_source": "model",
            "decision_tag": "LONG_DRAGON",
            "decision_confidence": 74,
            "decision_id": "dec_100",
        },
        {
            "bet_id": "b101",
            "sequence": 2,
            "direction": "small",
            "amount": 1000,
            "result": "输",
            "profit": -1000,
            "decision_source": "model",
            "decision_tag": "REVERSAL",
            "decision_confidence": 61,
            "decision_id": "dec_101",
        },
    ]

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=7011, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    cmd_event = SimpleNamespace(raw_text="replay 2", chat_id=7011, id=1)
    asyncio.run(zm.process_user_command(SimpleNamespace(), cmd_event, ctx, {}))

    assert sent_messages
    replay_msg = sent_messages[-1]
    assert "b100" in replay_msg
    assert "b101" in replay_msg
    assert "decision_id" in replay_msg


def test_process_bet_on_records_decision_linkage_fields(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "replay_link_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "链路字段用户"},
            "telegram": {"user_id": 7012},
            "groups": {"admin_chat": 7012},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0

    async def fake_predict(user_ctx, global_cfg):
        runtime = user_ctx.state.runtime
        runtime["last_predict_info"] = "replay-link-test"
        runtime["last_decision_id"] = "dec_test_001"
        runtime["last_decision_timestamp"] = "2026-03-05 10:00:00"
        runtime["last_decision_source"] = "model"
        runtime["last_decision_model_id"] = "qwen-test"
        runtime["last_decision_prediction"] = 1
        runtime["last_decision_confidence"] = 88
        runtime["last_decision_tag"] = "STABILITY"
        runtime["last_decision_reason"] = "unit-test"
        runtime["last_decision_round"] = 1
        runtime["last_decision_mode"] = "M-SMP"
        user_ctx.state.predictions.append(1)
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=7012, id=1)

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_v10", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近40次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 1
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert len(ctx.state.bet_sequence_log) == 1
    entry = ctx.state.bet_sequence_log[-1]
    assert entry["decision_id"] == "dec_test_001"
    assert entry["decision_source"] == "model"
    assert entry["decision_tag"] == "STABILITY"
    assert entry["decision_confidence"] == 88
    assert rt["pending_bet_id"] == entry["bet_id"]


def test_predict_next_bet_v10_updates_current_model_after_fallback(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "fallback_model_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "闄嶇骇妯″瀷鐢ㄦ埛"},
            "telegram": {"user_id": 70121},
            "groups": {"admin_chat": 70121},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "models": {
                    "1": {"model_id": "model-1", "enabled": True},
                    "2": {"model_id": "model-2", "enabled": True},
                },
                "fallback_chain": ["1", "2"],
            },
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.history = [0, 1] * 30
    rt = ctx.state.runtime
    rt["current_model_id"] = "model-1"

    class FakeModelManager:
        async def call_model(self, model_id, messages, **kwargs):
            assert model_id == "model-1"
            return {
                "success": True,
                "error": "",
                "content": '{"prediction": 1, "confidence": 91, "reason": "fallback"}',
                "model_id": "model-2",
                "requested_model_id": "model-1",
                "fallback_used": True,
            }

    monkeypatch.setattr(ctx, "get_model_manager", lambda: FakeModelManager())

    prediction = asyncio.run(zm.predict_next_bet_v10(ctx, {}))

    assert prediction == 1
    assert rt["current_model_id"] == "model-2"
    assert '"model_id": "model-2"' in rt["last_logic_audit"]


def test_process_settle_updates_target_pending_entry_by_pending_bet_id(tmp_path, monkeypatch):
    settled_tokens = next(
        const
        for const in zm._find_pending_bet_entry.__code__.co_consts
        if isinstance(const, tuple) and len(const) >= 3 and all(isinstance(item, str) for item in const)
    )
    win_token = settled_tokens[0]

    user_dir = tmp_path / "users" / "replay_settle_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "结算定位用户"},
            "telegram": {"user_id": 7013},
            "groups": {"admin_chat": 7013},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1
    rt["bet_amount"] = 1_000
    rt["bet_sequence_count"] = 1
    rt["current_round"] = 1
    rt["current_bet_seq"] = 2
    rt["account_balance"] = 10_000_000
    rt["gambling_fund"] = 9_000_000
    rt["pending_bet_id"] = "bet_pending_1"
    rt["last_predict_info"] = "settle-link-test"
    ctx.state.bet_sequence_log = [
        {
            "bet_id": "bet_pending_1",
            "result": None,
            "profit": 0,
            "round": 1,
            "sequence": 1,
            "direction": "big",
            "amount": 1000,
            "decision_id": "dec_settle_1",
            "decision_source": "model",
            "decision_tag": "TEST",
            "decision_confidence": 70,
        },
        {"bet_id": "bet_old_done", "result": win_token, "profit": 990, "round": 1, "sequence": 0},
    ]

    async def fake_send_message_v2(*args, **kwargs):
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=7013, id=1)

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=9901, message=SimpleNamespace(message="已结算: 结果为 8 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert ctx.state.bet_sequence_log[0]["result"] == win_token
    assert ctx.state.bet_sequence_log[0]["profit"] == 990
    assert ctx.state.bet_sequence_log[1]["bet_id"] == "bet_old_done"
    assert ctx.state.bet_sequence_log[1]["profit"] == 990
    assert rt["pending_bet_id"] == ""
