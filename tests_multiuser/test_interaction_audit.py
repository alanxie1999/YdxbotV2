import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from user_manager import UserContext
import user_manager as um
import zq_multiuser as zm


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if (path.name == "config.json" or path.name.endswith("_config.json")) and path.parent.parent.name == "users" and isinstance(data, dict):
        payload = json.loads(json.dumps(data, ensure_ascii=False))
        groups = payload.get("groups", {}) if isinstance(payload.get("groups", {}), dict) else {}
        notification = payload.get("notification", {}) if isinstance(payload.get("notification", {}), dict) else {}
        admin_console = payload.get("admin_console", {}) if isinstance(payload.get("admin_console", {}), dict) else {}
        if not admin_console:
            admin_chat = notification.get("admin_chat", groups.get("admin_chat", payload.get("telegram", {}).get("user_id", 0)))
            admin_console = {
                "mode": "telegram_id",
                "telegram_id": {"chat_id": admin_chat},
                "telegram_bot": {"bot_token": "", "chat_id": "", "allowed_sender_ids": []},
            }
            payload["admin_console"] = admin_console
        channels = notification.get("channels", {}) if isinstance(notification.get("channels", {}), dict) else {}
        if not channels:
            channels = {
                "iyuu": notification.get("iyuu", {"enable": False, "url": "", "token": ""}),
                "telegram_notify_bot": notification.get("tg_bot", {"enable": False, "bot_token": "", "chat_id": ""}),
            }
        payload["notification"] = {"channels": channels}
        data = payload
    if (path.name == "config.json" or path.name.endswith("_config.json")) and path.parent.parent.name == "users":
        canonical = path.parent / f"{path.parent.name}_config.json"
        canonical.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_text(path: Path):
    return path.read_text(encoding="utf-8")


def test_append_interaction_event_writes_daily_log_and_prunes_old_files(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "8801"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "Audit User"},
            "telegram": {"user_id": 8801},
        },
    )
    ctx = UserContext(str(user_dir))

    log_root = tmp_path / "logs" / "accounts"
    monkeypatch.setattr(zm, "ACCOUNT_LOG_ROOT", str(log_root))

    interaction_dir = log_root / "8801" / "interactions"
    interaction_dir.mkdir(parents=True, exist_ok=True)
    old_name = (datetime.now().date() - timedelta(days=7)).strftime("%Y-%m-%d") + ".log"
    keep_name = (datetime.now().date() - timedelta(days=6)).strftime("%Y-%m-%d") + ".log"
    (interaction_dir / old_name).write_text("old\n", encoding="utf-8")
    (interaction_dir / keep_name).write_text("keep\n", encoding="utf-8")

    zm.append_interaction_event(
        ctx,
        direction="inbound",
        kind="command",
        channel="admin_chat",
        text="help",
        command="help",
    )

    today_path = interaction_dir / (datetime.now().strftime("%Y-%m-%d") + ".log")
    assert today_path.exists()
    assert not (interaction_dir / old_name).exists()
    assert (interaction_dir / keep_name).exists()

    content = _load_text(today_path)
    assert "接收 | admin_chat | 命令 | help" in content
    assert "\nhelp\n" in content


def test_send_message_v2_records_outbound_interactions(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "Route User"},
            "telegram": {"user_id": 5001},
            "groups": {"admin_chat": 5001},
            "notification": {
                "iyuu": {"enable": True, "url": "https://iyuu.test/send"},
                "tg_bot": {"enable": True, "bot_token": "token", "chat_id": "chat"},
            },
        },
    )
    ctx = UserContext(str(user_dir))

    log_root = tmp_path / "logs" / "accounts"
    monkeypatch.setattr(zm, "ACCOUNT_LOG_ROOT", str(log_root))

    outbound_payloads = []

    def fake_post(url, data=None, json=None, timeout=5):
        outbound_payloads.append({"url": url, "data": data, "json": json, "timeout": timeout})
        return SimpleNamespace(status_code=200, url=url, data=data, json=json, timeout=timeout)

    monkeypatch.setattr(zm.requests, "post", fake_post)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=7)

    asyncio.run(
        zm.send_message_v2(
            DummyClient(),
            "lose_streak",
            "【账号：Route User】\n测试告警",
            ctx,
            {},
            title="标题",
            desp="测试告警",
        )
    )

    today_path = log_root / "5001" / "interactions" / (datetime.now().strftime("%Y-%m-%d") + ".log")
    content = _load_text(today_path)
    tg_payload = next(item["json"] for item in outbound_payloads if "api.telegram.org" in item["url"])
    iyuu_payload = next(item["data"] for item in outbound_payloads if "iyuu.test" in item["url"])

    assert "发送 | admin_chat | 通知 | lose_streak | 成功 | chat_id=5001" in content
    assert "发送 | iyuu | 通知 | lose_streak | 成功" in content
    assert "发送 | tg_bot | 通知 | lose_streak | 成功 | chat_id=chat" in content
    assert "\n测试告警\n" in content
    assert "【账号：Route User】\n\n测试告警" in content
    assert tg_payload["text"].startswith("【账号：Route User】\n\n测试告警")
    assert iyuu_payload["desp"].startswith("【账号：Route User】\n\n测试告警")


def test_process_user_command_records_masked_apikey_command(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5008"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "Secret User"},
            "telegram": {"user_id": 5008},
            "groups": {"admin_chat": 5008},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {"enabled": True, "models": {"1": {"model_id": "m1", "enabled": True}}},
        },
    )
    ctx = UserContext(str(user_dir))

    log_root = tmp_path / "logs" / "accounts"
    monkeypatch.setattr(zm, "ACCOUNT_LOG_ROOT", str(log_root))

    class DummyEvent:
        raw_text = "apikey add sk-test-secret"
        chat_id = 5008
        id = 88

        def __init__(self):
            self.replies = []

        async def reply(self, message):
            self.replies.append(message)
            return SimpleNamespace(chat_id=self.chat_id, id=len(self.replies))

    event = DummyEvent()
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    today_path = log_root / "5008" / "interactions" / (datetime.now().strftime("%Y-%m-%d") + ".log")
    content = _load_text(today_path)

    assert "接收 | admin_chat | 命令 | apikey | 已脱敏 | chat_id=5008" in content
    assert "\napikey add ***\n" in content
    assert event.replies


def test_console_handler_uses_account_label_without_category():
    fmt = zm.console_handler.formatter._fmt
    assert "%(account_label)s" in fmt
    assert "%(category)s" not in fmt
    assert "%(account_tag)s" not in fmt


def test_no_root_log_file_handlers_remain():
    zq_file_names = {
        Path(getattr(handler, "baseFilename", "")).name
        for handler in zm.logger.handlers
        if getattr(handler, "baseFilename", "")
    }
    um_file_names = {
        Path(getattr(handler, "baseFilename", "")).name
        for handler in um.logger.handlers
        if getattr(handler, "baseFilename", "")
    }

    assert "bot.log" not in zq_file_names
    assert "user_manager.log" not in um_file_names


def test_user_manager_logs_route_into_account_directory(tmp_path):
    log_root = tmp_path / "logs" / "accounts"
    um.account_category_handler.close()
    um.account_category_handler.root_dir = str(log_root)
    um.account_category_handler._handlers = {}

    um.log_event(logging.INFO, "save_state", "保存用户状态成功", "user_id=7123")

    account_log = log_root / "user-7123" / "runtime.log"
    assert account_log.exists()
    content = account_log.read_text(encoding="utf-8")
    assert "保存用户状态成功" in content


def test_build_priority_summary_preserves_key_fields():
    card = zm._build_ops_card(
        "⚠️ 连输告警",
        summary="当前已经触发连续亏损阈值。",
        fields=[
            ("状态", "自动暂停（剩3局）"),
            ("预设", "yc05"),
            ("账户余额", "12.00 万"),
        ],
        action="建议立即执行 `status`。",
    )

    summary = zm._build_priority_summary("lose_streak", card, "【账号：Route User】")

    assert summary.startswith("【账号：Route User】\n[P1] ⚠️ 连输告警")
    assert "当前已经触发连续亏损阈值。" in summary
    assert "状态：自动暂停（剩3局）" in summary
    assert "预设：yc05" in summary
    assert "操作：建议立即查看 `status`，如需止损可执行 `pause`。" in summary
