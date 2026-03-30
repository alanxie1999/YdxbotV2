"""Group market broadcast and alert tool.

This module is intentionally standalone:
- It manages its own config/state files
- It can reuse pure helpers from ``zq_multiuser``
- The main runtime does not need to import it
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from html import escape as escape_html
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

import zq_multiuser as zm


MODULE_DIR = Path(__file__).resolve().parent
CONFIG_EXAMPLE_PATH = MODULE_DIR / "market_broadcast_alert_config.example.json"
CONFIG_PATH = MODULE_DIR / "market_broadcast_alert_config.json"
STATE_PATH = MODULE_DIR / "market_broadcast_alert_state.json"
logger = logging.getLogger("market_broadcast_alert")
RUNTIME_LOCK = threading.RLock()
AUTO_DELETE_SECONDS = 60

DEFAULT_CONFIG: Dict[str, Any] = {
    "enable": False,
    "bot_token": "",
    "chat_ids": [],
    "allowed_sender_ids": [],
    "report_enable": True,
    "streak_threshold": 4,
    "pair_trigger_consecutive": 3,
    "report_interval": 10,
    "cooldown_seconds": 600,
    "mention_users": [],
}

DEFAULT_STATE: Dict[str, Any] = {
    "last_update_id": 0,
    "last_history_fingerprint": "",
    "round_counter": 0,
    "market_history": [],
    "last_streak_side": "",
    "last_streak_notified_len": 0,
    "last_pair_tag": "",
    "last_pair_count": 0,
    "last_pair_notified_count": 0,
    "last_report_round": 0,
    "last_alert_at": {},
    "last_message_ids": {},
}


@dataclass
class AlertEvent:
    event_type: str
    message: str
    parse_mode: Optional[str] = "HTML"


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    merged = dict(default)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "on", "yes", "y", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "off", "no", "n", "disable", "disabled"}:
            return False
    return bool(default)


def load_config() -> Dict[str, Any]:
    if not CONFIG_EXAMPLE_PATH.exists():
        CONFIG_EXAMPLE_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    if not CONFIG_PATH.exists():
        config = dict(DEFAULT_CONFIG)
    else:
        config = _read_json(CONFIG_PATH, DEFAULT_CONFIG)
    config["streak_threshold"] = max(1, int(config.get("streak_threshold", 4) or 4))
    config["pair_trigger_consecutive"] = max(1, int(config.get("pair_trigger_consecutive", 3) or 3))
    config["report_interval"] = max(1, int(config.get("report_interval", 10) or 10))
    config["cooldown_seconds"] = max(0, int(config.get("cooldown_seconds", 600) or 600))
    config["report_enable"] = _to_bool(config.get("report_enable", True), True)
    raw_chat_ids = config.get("chat_ids", config.get("chat_id", []))
    if isinstance(raw_chat_ids, (int, str)):
        raw_chat_ids = [raw_chat_ids]
    if not isinstance(raw_chat_ids, list):
        raw_chat_ids = []
    normalized_chat_ids: List[int] = []
    for item in raw_chat_ids:
        try:
            chat_id = int(item)
        except (TypeError, ValueError):
            continue
        if chat_id and chat_id not in normalized_chat_ids:
            normalized_chat_ids.append(chat_id)
    config["chat_ids"] = normalized_chat_ids
    if "chat_id" in config:
        config.pop("chat_id", None)
    mention_users = config.get("mention_users", [])
    if not isinstance(mention_users, list):
        mention_users = []
    config["mention_users"] = [str(item).strip() for item in mention_users if str(item).strip()]
    allowed_sender_ids = config.get("allowed_sender_ids", [])
    if not isinstance(allowed_sender_ids, list):
        allowed_sender_ids = []
    normalized_sender_ids: List[int] = []
    for item in allowed_sender_ids:
        try:
            normalized_sender_ids.append(int(item))
        except (TypeError, ValueError):
            continue
    config["allowed_sender_ids"] = normalized_sender_ids
    return config


def save_config(config: Dict[str, Any]) -> None:
    _write_json(CONFIG_PATH, config)


def load_state() -> Dict[str, Any]:
    state = _read_json(STATE_PATH, DEFAULT_STATE)
    if not isinstance(state.get("market_history"), list):
        state["market_history"] = []
    if not isinstance(state.get("last_alert_at"), dict):
        state["last_alert_at"] = {}
    if not isinstance(state.get("last_message_ids"), dict):
        state["last_message_ids"] = {}
    return state


def save_state(state: Dict[str, Any]) -> None:
    _write_json(STATE_PATH, state)


def validate_runtime_config(config: Dict[str, Any], require_chat_ids: bool = True) -> tuple[str, List[int]]:
    bot_token = str(config.get("bot_token", "") or "").strip()
    chat_ids = list(config.get("chat_ids", [])) if isinstance(config.get("chat_ids", []), list) else []
    if not bot_token:
        raise ValueError("盘口播报提醒未配置 bot_token")
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{20,}", bot_token):
        raise ValueError(
            "盘口播报提醒 bot_token 格式无效，请填写 BotFather 提供的完整 token，格式应为“数字:密钥”"
        )
    if require_chat_ids and not chat_ids:
        raise ValueError("盘口播报提醒未配置 chat_ids")
    return bot_token, chat_ids


def parse_market_history(text: str) -> List[int]:
    raw = str(text or "")
    history_str = raw.split("]")[-1] if "]" in raw else raw
    values = [int(x) for x in zm.re.findall(r"(?<!\d)[01](?!\d)", history_str)]
    return values


def get_current_streak(history: List[int]) -> tuple[int, int]:
    if not history:
        return 0, -1
    tail = int(history[-1])
    streak = 1
    for value in reversed(history[:-1]):
        if int(value) == tail:
            streak += 1
        else:
            break
    return streak, tail


def _cooldown_ready(state: Dict[str, Any], config: Dict[str, Any], key: str, now_ts: Optional[int] = None) -> bool:
    now = int(now_ts or time.time())
    last_map = state.get("last_alert_at", {})
    last_ts = int(last_map.get(key, 0) or 0)
    return now - last_ts >= int(config.get("cooldown_seconds", 600) or 600)


def _mark_alert_sent(state: Dict[str, Any], key: str, now_ts: Optional[int] = None) -> None:
    if "last_alert_at" not in state or not isinstance(state["last_alert_at"], dict):
        state["last_alert_at"] = {}
    state["last_alert_at"][key] = int(now_ts or time.time())


def _format_history_block(history: List[int], width: int = 10, rows: int = 4) -> str:
    recent = history[-(width * rows):]
    if not recent:
        return "[ ]"
    lines: List[str] = []
    for start in range(0, len(recent), width):
        row = recent[start:start + width]
        lines.append("[ " + " ".join(str(x) for x in row) + " ]")
    return "\n".join(lines)


def _format_history_grid_like_main(history: List[int]) -> str:
    try:
        return str(zm._build_recent_history_grid(history))
    except Exception:
        recent = history[-40:][::-1]
        if not recent:
            return "暂无数据"
        icons = ["✅" if x == 1 else "❌" for x in recent]
        return "\n".join(" ".join(icons[i:i + 10]) for i in range(0, len(icons), 10))


def _build_history_html(history: List[int]) -> str:
    history_grid = escape_html(_format_history_grid_like_main(history))
    return (
        "<b>📊 近期 40 次结果（由近及远）</b>\n"
        "✅：大（1）  ❌：小（0）\n"
        f"<pre>{history_grid}</pre>"
    )


def _format_mentions(config: Dict[str, Any]) -> str:
    mention_users = config.get("mention_users", [])
    if not mention_users:
        return ""
    return " ".join(mention_users)


def _build_card_html(
    title: str,
    *,
    summary: str = "",
    fields: Optional[List[tuple[str, Any]]] = None,
    history: Optional[List[int]] = None,
    mentions: str = "",
) -> str:
    lines = [f"<b>{escape_html(str(title or '').strip())}</b>"]
    if summary:
        lines.extend(["", escape_html(summary)])
    for label, value in fields or []:
        if value in (None, ""):
            continue
        lines.append(f"{escape_html(str(label))}：{escape_html(str(value))}")
    if history is not None:
        lines.extend(["", _build_history_html(history[-40:])])
    if mentions:
        lines.extend(["", escape_html(mentions).replace("&commat;", "@")])
    return "\n".join(lines).strip()


def _build_pattern_alert_html(
    *,
    alert_type: str,
    rule_text: str,
    advice_text: str,
    history: List[int],
    mentions: str,
) -> str:
    lines = [
        "<b>🚨 盘口重点规律提醒 🚨</b>",
        "",
        f"⚠️类型：{escape_html(alert_type)}",
        f"⚠️规律：{escape_html(rule_text)}",
        "",
        f"⚠️建议：{escape_html(advice_text)}",
        "",
        _build_history_html(history[-40:]),
    ]
    if mentions:
        lines.extend(["", escape_html(mentions).replace("&commat;", "@")])
    return "\n".join(lines).strip()


def build_streak_alert(history: List[int], config: Dict[str, Any]) -> Optional[str]:
    streak_len, side = get_current_streak(history)
    threshold = int(config.get("streak_threshold", 4) or 4)
    if streak_len < threshold or side not in {0, 1}:
        return None

    alert_type = "连大提醒" if side == 1 else "连小提醒"
    advice_side = "小" if side == 1 else "大"
    rule_text = f"已出现 {streak_len} 连{'大' if side == 1 else '小'}"
    return _build_pattern_alert_html(
        alert_type=alert_type,
        rule_text=rule_text,
        advice_text=f"建议手动连续反向押注，押注：{advice_side}",
        history=history,
        mentions=_format_mentions(config),
    )


def build_pair_alert(history: List[int], config: Dict[str, Any]) -> Optional[str]:
    rhythm = zm.analyze_rhythm_context(history)
    threshold = int(config.get("pair_trigger_consecutive", 3) or 3)
    tag = str(rhythm.get("rhythm_tag", ""))
    if tag != "ALTERNATION_RHYTHM":
        return None

    next_char = rhythm.get("alternation_next")
    if next_char not in {0, 1}:
        return None
    advice_side = "大" if int(next_char) == 0 else "小"
    return _build_pattern_alert_html(
        alert_type="配对规律提醒",
        rule_text="当前盘口连续识别为交替型（010101 / 101010）",
        advice_text=f"建议手动押注，结束交替规律，押注：{advice_side}",
        history=history,
        mentions=_format_mentions(config),
    )


def build_market_stats_report(history: List[int], report_interval: int, config: Dict[str, Any]) -> str:
    windows = [1000, 500, 200, 100]
    labels: List[int] = []
    stats = {"连大": [], "连小": []}
    all_ns = set()

    for window in windows:
        actual = min(int(window), len(history))
        if actual <= 0:
            continue
        if actual in labels:
            continue
        labels.append(actual)
        result_counts = zm.count_consecutive(history[-actual:])
        stats["连大"].append(result_counts["大"])
        stats["连小"].append(result_counts["小"])
        all_ns.update(result_counts["大"].keys())
        all_ns.update(result_counts["小"].keys())

    label_width = max(3, max(len(str(label)) for label in labels)) if labels else 3
    header = "类别 |" + "".join(f" {str(label).rjust(label_width)} |" for label in labels)
    divider = "-" * len(header)

    lines: List[str] = []
    for category in ["连大", "连小"]:
        lines.append(category)
        lines.append("=" * len(header))
        lines.append(header)
        lines.append(divider)
        for n in sorted(all_ns, reverse=True):
            if any(n in stats[category][i] for i in range(len(labels))):
                row = f" {str(n).center(2)}  |"
                for i in range(len(labels)):
                    count = stats[category][i].get(n, 0)
                    value = str(count) if count > 0 else "-"
                    row += f" {value.center(label_width)} |"
                lines.append(row)
        lines.append("")

    table_block = escape_html("\n".join(lines).rstrip())
    return f"<b>📊 群盘口统计播报 📊</b>\n\n<pre>{table_block}</pre>"


def update_market_state(state: Dict[str, Any], history: List[int]) -> bool:
    fingerprint = "".join(str(x) for x in history[-40:])
    if not history or state.get("last_history_fingerprint", "") == fingerprint:
        return False
    state["last_history_fingerprint"] = fingerprint
    state["market_history"] = history[-2000:]
    state["round_counter"] = int(state.get("round_counter", 0) or 0) + 1
    return True


def evaluate_alerts(state: Dict[str, Any], config: Dict[str, Any], history: List[int]) -> List[AlertEvent]:
    events: List[AlertEvent] = []
    if not history or not bool(config.get("enable", False)):
        return events

    streak_len, side = get_current_streak(history)
    streak_key = f"streak_{side}_{streak_len}"
    threshold = int(config.get("streak_threshold", 4) or 4)
    if streak_len >= threshold and _cooldown_ready(state, config, streak_key):
        message = build_streak_alert(history, config)
        if message:
            events.append(AlertEvent("streak", message))
            _mark_alert_sent(state, streak_key)

    rhythm = zm.analyze_rhythm_context(history)
    rhythm_tag = str(rhythm.get("rhythm_tag", ""))
    prev_tag = str(state.get("last_pair_tag", "") or "")
    prev_count = int(state.get("last_pair_count", 0) or 0)
    if rhythm_tag in {"ALTERNATION_RHYTHM", "PAIR_FORMATION"}:
        current_count = prev_count + 1 if rhythm_tag == prev_tag else 1
        state["last_pair_tag"] = rhythm_tag
        state["last_pair_count"] = current_count
        notify_threshold = int(config.get("pair_trigger_consecutive", 3) or 3)
        pair_key = f"pair_{rhythm_tag}_{current_count}"
        if current_count >= notify_threshold and _cooldown_ready(state, config, pair_key):
            message = build_pair_alert(history, config)
            if message:
                events.append(AlertEvent("pair", message))
                _mark_alert_sent(state, pair_key)
    else:
        state["last_pair_tag"] = ""
        state["last_pair_count"] = 0

    report_interval = int(config.get("report_interval", 10) or 10)
    round_counter = int(state.get("round_counter", 0) or 0)
    last_report_round = int(state.get("last_report_round", 0) or 0)
    if (
        bool(config.get("report_enable", True))
        and round_counter >= report_interval
        and round_counter - last_report_round >= report_interval
    ):
        events.append(AlertEvent("report", build_market_stats_report(history, report_interval, config)))
        state["last_report_round"] = round_counter

    return events


def _is_admin(bot_token: str, chat_id: int, user_id: int) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/getChatMember"
    response = requests.get(url, params={"chat_id": chat_id, "user_id": user_id}, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        return False
    status = str(data.get("result", {}).get("status", "")).lower()
    return status in {"administrator", "creator"}


def _send_text(bot_token: str, chat_id: int, text: str, parse_mode: Optional[str] = None) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    return response.json()


def _build_bot_commands() -> List[Dict[str, str]]:
    return [
        {"command": "fa", "description": "查看盘口播报配置"},
        {"command": "faon", "description": "开启盘口播报提醒"},
        {"command": "faoff", "description": "关闭盘口播报提醒"},
        {"command": "fas", "description": "设置连大连小提醒阈值"},
        {"command": "fap", "description": "设置配对规律提醒阈值"},
        {"command": "far", "description": "设置周期播报开关或间隔"},
        {"command": "fam", "description": "查看或修改@名单"},
    ]


def _ensure_bot_menu(bot_token: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/setMyCommands"
    response = requests.post(url, json={"commands": _build_bot_commands()}, timeout=15)
    response.raise_for_status()
    menu_url = f"https://api.telegram.org/bot{bot_token}/setChatMenuButton"
    menu_response = requests.post(menu_url, json={"menu_button": {"type": "commands"}}, timeout=15)
    menu_response.raise_for_status()


def _delete_message(bot_token: str, chat_id: int, message_id: int) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    response = requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=15)
    response.raise_for_status()


def _schedule_delete(bot_token: str, chat_id: int, message_id: int, delay_seconds: int = AUTO_DELETE_SECONDS) -> None:
    def _runner():
        try:
            time.sleep(max(1, int(delay_seconds)))
            _delete_message(bot_token, chat_id, message_id)
        except Exception:
            return

    threading.Thread(target=_runner, daemon=True).start()


def _normalize_command(text: str) -> List[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    if raw.startswith("/"):
        raw = raw[1:]
    alias_map = {
        "faon": "fa on",
        "faoff": "fa off",
        "fas": "fa s",
        "fap": "fa p",
        "far": "fa r",
        "fam": "fa m",
    }
    lowered = raw.lower()
    for alias, replacement in alias_map.items():
        if lowered == alias:
            raw = replacement
            break
        if lowered.startswith(alias + " "):
            raw = replacement + raw[len(alias):]
            break
    raw = raw.replace("@", " @")
    return [part.strip() for part in raw.split() if part.strip()]


def _extract_message_context(message: Dict[str, Any]) -> Dict[str, Any]:
    chat = message.get("chat", {}) if isinstance(message.get("chat", {}), dict) else {}
    sender = message.get("from", {}) if isinstance(message.get("from", {}), dict) else {}
    return {
        "chat_id": int(chat.get("id", 0) or 0),
        "chat_type": str(chat.get("type", "") or "").strip().lower(),
        "sender_id": int(sender.get("id", 0) or 0),
        "message_id": int(message.get("message_id", 0) or 0),
        "text": str(message.get("text", "") or ""),
    }


def _normalize_mention_tokens(tokens: List[str]) -> List[str]:
    normalized: List[str] = []
    for item in tokens:
        raw = str(item or "").strip()
        if not raw:
            continue
        username = raw[1:] if raw.startswith("@") else raw
        username = username.strip()
        if not username:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]{2,64}", username):
            continue
        mention = f"@{username}"
        if mention not in normalized:
            normalized.append(mention)
    return normalized


def handle_command(text: str, sender_id: int, config: Dict[str, Any]) -> Optional[str]:
    tokens = _normalize_command(text)
    if not tokens:
        return None
    if tokens[0].lower() != "fa":
        return None

    allowed_sender_ids = set(int(x) for x in config.get("allowed_sender_ids", []))
    if allowed_sender_ids and sender_id and sender_id not in allowed_sender_ids:
        return "❌ 仅指定管理员可使用该命令"

    bot_token = str(config.get("bot_token", "") or "").strip()
    primary_chat_id = int(config.get("chat_ids", [0])[0] or 0) if config.get("chat_ids") else 0
    if not allowed_sender_ids and bot_token and primary_chat_id and sender_id:
        try:
            if not _is_admin(bot_token, primary_chat_id, sender_id):
                return "❌ 仅群管理员可使用该命令"
        except Exception:
            return "❌ 管理员身份校验失败，请稍后再试"

    if len(tokens) == 1:
        mentions = " ".join(config.get("mention_users", [])) or "未设置"
        status = "ON" if bool(config.get("enable", False)) else "OFF"
        chat_ids_text = " / ".join(str(x) for x in config.get("chat_ids", [])) or "未设置"
        return (
            "📡 盘口播报提醒配置\n\n"
            f"开关：{status}\n"
            f"通知群ID：{chat_ids_text}\n"
            f"命令管理员ID：{', '.join(str(x) for x in config.get('allowed_sender_ids', [])) or '未设置'}\n"
            f"连大连小阈值：{config.get('streak_threshold', 4)}\n"
            f"配对规律阈值：{config.get('pair_trigger_consecutive', 3)}\n"
            f"周期播报：{'ON' if bool(config.get('report_enable', True)) else 'OFF'}\n"
            f"周期播报间隔：{config.get('report_interval', 10)}\n"
            f"@名单：{mentions}"
        )

    sub = tokens[1].lower()
    if sub in {"on", "off"}:
        config["enable"] = sub == "on"
        save_config(config)
        return f"✅ 已{'开启' if config['enable'] else '关闭'}盘口播报提醒"

    if sub == "s" and len(tokens) >= 3:
        config["streak_threshold"] = max(1, int(tokens[2]))
        save_config(config)
        return f"✅ 连大连小提醒阈值已设置为 {config['streak_threshold']}"

    if sub == "p" and len(tokens) >= 3:
        config["pair_trigger_consecutive"] = max(1, int(tokens[2]))
        save_config(config)
        return f"✅ 配对规律提醒阈值已设置为 {config['pair_trigger_consecutive']}"

    if sub == "r" and len(tokens) >= 3:
        arg = tokens[2].lower()
        if arg in {"on", "off"}:
            config["report_enable"] = arg == "on"
            save_config(config)
            return f"✅ 已{'开启' if config['report_enable'] else '关闭'}周期统计播报"
        config["report_interval"] = max(1, int(tokens[2]))
        save_config(config)
        return f"✅ 盘口统计播报间隔已设置为 {config['report_interval']}"

    if sub == "m":
        mention_users = list(config.get("mention_users", []))
        if len(tokens) == 2:
            return "📡 当前@名单\n\n" + ("\n".join(mention_users) if mention_users else "未设置")
        action = tokens[2]
        payload = _normalize_mention_tokens(tokens[3:])
        if action == "+":
            merged = mention_users + [item for item in payload if item not in mention_users]
            config["mention_users"] = merged
            save_config(config)
            return "✅ 已添加@名单\n\n" + ("\n".join(payload) if payload else "未添加任何用户")
        if action == "-":
            config["mention_users"] = [item for item in mention_users if item not in payload]
            save_config(config)
            return "✅ 已删除@名单\n\n" + ("\n".join(payload) if payload else "未删除任何用户")

    return (
        "📡 fa 命令说明\n\n"
        "支持私聊机器人或在通知群里执行以下配置命令。\n\n"
        "fa\n"
        "fa on / fa off\n"
        "fa s 4\n"
        "fa p 3\n"
        "fa r on / fa r off\n"
        "fa r 10\n"
        "fa m\n"
        "fa m + @user1 @user2\n"
        "fa m - @user1\n"
        "配置文件需填写：bot_token / chat_ids / allowed_sender_ids"
    )


def _command_reply_ttl(reply_text: str) -> int:
    text = str(reply_text or "")
    if "命令说明" in text:
        return 12
    return 5


def process_command_message(message: Dict[str, Any], config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ctx = _extract_message_context(message)
    chat_ids = set(int(x) for x in config.get("chat_ids", []) if str(x).strip())
    is_private = ctx["chat_type"] == "private"
    is_notify_group = ctx["chat_id"] in chat_ids if ctx["chat_id"] else False
    if not is_private and not is_notify_group:
        return None
    reply = handle_command(ctx["text"], ctx["sender_id"], config)
    if not reply:
        return None
    return {
        "reply_chat_id": ctx["chat_id"],
        "reply_text": reply,
        "reply_ttl": _command_reply_ttl(reply),
        "request_chat_id": ctx["chat_id"],
        "request_message_id": ctx["message_id"],
    }


def process_group_message(message: Dict[str, Any], config: Dict[str, Any], state: Dict[str, Any]) -> List[AlertEvent]:
    text = str(message.get("text", "") or "")
    history = parse_market_history(text)
    if not history:
        return []
    updated = update_market_state(state, history)
    if not updated:
        return []
    return evaluate_alerts(state, config, history)


def process_market_history_snapshot(history: List[int]) -> int:
    normalized_history = [int(x) for x in history if int(x) in {0, 1}]
    if not normalized_history:
        return 0

    with RUNTIME_LOCK:
        config = load_config()
        if not bool(config.get("enable", False)):
            return 0

        try:
            bot_token, chat_ids = validate_runtime_config(config, require_chat_ids=True)
        except ValueError as exc:
            logger.warning("盘口播报提醒配置无效：%s", exc)
            return 0

        state = load_state()
        if not update_market_state(state, normalized_history[-2000:]):
            save_state(state)
            return 0

        events = evaluate_alerts(state, config, normalized_history[-2000:])
        sent_count = 0
        for event in events:
            for chat_id in chat_ids:
                try:
                    last_message_ids = state.setdefault("last_message_ids", {})
                    per_event = last_message_ids.setdefault(event.event_type, {})
                    if not isinstance(per_event, dict):
                        per_event = {}
                        last_message_ids[event.event_type] = per_event
                    previous_message_id = int(per_event.get(str(chat_id), 0) or 0)
                    if previous_message_id > 0:
                        try:
                            _delete_message(bot_token, chat_id, previous_message_id)
                        except requests.RequestException:
                            pass
                    response = _send_text(bot_token, chat_id, event.message, parse_mode=event.parse_mode)
                    message_id = int(response.get("result", {}).get("message_id", 0) or 0)
                    if message_id > 0:
                        per_event[str(chat_id)] = message_id
                        _schedule_delete(bot_token, chat_id, message_id, AUTO_DELETE_SECONDS)
                    sent_count += 1
                except requests.RequestException as exc:
                    logger.warning("盘口播报提醒发送失败：chat_id=%s error=%s", chat_id, exc)

        save_state(state)
        return sent_count


def run_forever(sleep_seconds: int = 3) -> None:
    config = load_config()
    state = load_state()
    bot_token, _ = validate_runtime_config(config, require_chat_ids=False)
    _ensure_bot_menu(bot_token)

    while True:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            response = requests.get(
                url,
                params={
                    "offset": int(state.get("last_update_id", 0) or 0),
                    "timeout": 20,
                    "allowed_updates": ["message"],
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            updates = payload.get("result", []) if isinstance(payload, dict) else []
            for update in updates:
                update_id = int(update.get("update_id", 0) or 0)
                state["last_update_id"] = update_id + 1
                message = update.get("message", {}) if isinstance(update.get("message", {}), dict) else {}
                command_result = process_command_message(message, config)
                if command_result:
                    response = _send_text(
                        bot_token,
                        int(command_result["reply_chat_id"]),
                        str(command_result["reply_text"]),
                    )
                    reply_message_id = int(response.get("result", {}).get("message_id", 0) or 0)
                    if reply_message_id > 0:
                        _schedule_delete(
                            bot_token,
                            int(command_result["reply_chat_id"]),
                            reply_message_id,
                            int(command_result.get("reply_ttl", 5) or 5),
                        )
                    request_message_id = int(command_result.get("request_message_id", 0) or 0)
                    request_chat_id = int(command_result.get("request_chat_id", 0) or 0)
                    if request_message_id > 0 and request_chat_id != 0:
                        try:
                            _schedule_delete(bot_token, request_chat_id, request_message_id, 5)
                        except Exception:
                            pass
                save_state(state)
            time.sleep(max(1, int(sleep_seconds)))
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 404:
                raise RuntimeError(
                    "盘口播报提醒 bot_token 无效或已失效，请检查 market_broadcast_alert_config.json 中是否填写了完整 token（格式：数字:密钥）"
                ) from exc
            logger.warning("盘口播报提醒请求失败：%s", exc)
            time.sleep(max(3, int(sleep_seconds)))
        except requests.RequestException as exc:
            logger.warning("盘口播报提醒网络异常：%s", exc)
            time.sleep(max(3, int(sleep_seconds)))


if __name__ == "__main__":
    run_forever()
