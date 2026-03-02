"""
zq_multiuser.py - 多用户版本核心逻辑
版本: 2.4.3
日期: 2026-02-21
功能: 多用户押注、结算、命令处理
"""

import logging
import asyncio
import json
import os
import random
import requests
import aiohttp
import time
import math
from collections import Counter
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from user_manager import UserContext
from typing import Dict, Any, List
import constants
from update_manager import (
    get_current_repo_info,
    list_version_catalog,
    reback_to_version,
    resolve_systemd_service_name,
    restart_process,
    update_to_version,
)

# 日志配置
logger = logging.getLogger('zq_multiuser')
logger.setLevel(logging.DEBUG)

file_handler = TimedRotatingFileHandler('bot.log', when='midnight', interval=1, backupCount=7, encoding='utf-8')
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - [%(user_id)s/%(event)s] - %(message)s - [%(data)s]',
    datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)

# 自动统计推送节奏：每 10 局一次，保留 10 分钟后自动删除
AUTO_STATS_INTERVAL_ROUNDS = 10
AUTO_STATS_DELETE_DELAY_SECONDS = 600

# 风控节奏：以最近 40 笔实盘胜率为基础，结合连输深度做分层暂停。
RISK_WINDOW_BETS = 40
RISK_BASE_TRIGGER_WINS = 15          # 15/40=37.5%
RISK_BASE_TRIGGER_STREAK_NEEDED = 2   # 连续2次命中基础风控才触发暂停
RISK_RECOVERY_WINS = 19              # >45% => 至少 19/40
RISK_RECOVERY_PASS_NEEDED = 2         # 连续2次满足恢复条件才重置风险周期

# 深度风控触发节奏（不占基础风控预算）：
# 每连输 3 局触发一次；首次触发上限更高，后续触发保持保守暂停。
RISK_DEEP_TRIGGER_INTERVAL = 3
RISK_DEEP_FIRST_MAX_PAUSE_ROUNDS = 5
RISK_DEEP_NEXT_MAX_PAUSE_ROUNDS = 3
RISK_BASE_MAX_PAUSE_ROUNDS = 10

# 基础风控预算：同一基础风控周期累计暂停不超过10局（深度风控不占用）
RISK_PAUSE_TOTAL_CAP_ROUNDS = 10
RISK_PAUSE_MODEL_TIMEOUT_SEC = 3.5
AI_KEY_WARNING_TEXT = "⚠️ 大模型AI key 失效/缺失，请更新 key！！！"

# 高倍入场质量门控（目标：尽量减少进入第5手以后）
ENTRY_GUARD_STEP3_MIN_CONF = 72
ENTRY_GUARD_STEP3_PAUSE_ROUNDS = 2
ENTRY_GUARD_STEP4_MIN_CONF = 75
ENTRY_GUARD_STEP4_PAUSE_ROUNDS = 3
ENTRY_GUARD_STEP4_ALLOWED_TAGS = {"DRAGON_CANDIDATE", "SINGLE_JUMP", "SYMMETRIC_WRAP"}


def log_event(level, module, event, message=None, **kwargs):
    # 兼容旧调用: log_event(level, event, message, user_id, data)
    if message is None:
        message = event
        event = module
        module = 'zq'
    data = ', '.join(f'{k}={v}' for k, v in kwargs.items())
    user_id = kwargs.get('user_id', 0)
    # 使用 'mod' 而不是 'module'，因为 'module' 是 logging 的保留字段
    logger.log(level, message, extra={'user_id': str(user_id), 'mod': module, 'event': event, 'data': data})


# 格式化数字
def format_number(num):
    """与 master 版一致：使用千分位格式。"""
    return f"{int(num):,}"


def _sync_fund_from_account_when_insufficient(rt: Dict[str, Any], required_amount: int = 0) -> bool:
    """
    仅在“资金不足”场景触发的修正：
    若当前菠菜资金不足，且账户余额更高，则把菠菜资金同步为账户余额。
    """
    try:
        fund = int(rt.get("gambling_fund", 0) or 0)
        balance = int(rt.get("account_balance", 0) or 0)
        need = max(0, int(required_amount or 0))
    except (TypeError, ValueError):
        return False

    threshold = max(1, need)
    if fund < threshold and balance > fund:
        rt["gambling_fund"] = balance
        return True
    return False


def heal_stale_pending_bets(user_ctx: UserContext) -> Dict[str, Any]:
    """
    启动时自愈历史挂单：
    - 仅允许“最后一笔且 runtime.bet=True”保持 result=None（真实待结算）
    - 其他 result=None 一律标记为“异常未结算”，避免历史统计与资金核对长期受污染
    """
    state = user_ctx.state
    rt = state.runtime
    logs = state.bet_sequence_log if isinstance(state.bet_sequence_log, list) else []
    if not logs:
        return {"count": 0, "items": []}

    pending_active = bool(rt.get("bet", False))
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    healed_items: List[str] = []

    for idx, item in enumerate(logs):
        if not isinstance(item, dict):
            continue
        if item.get("result") is not None:
            continue

        is_last = (idx == len(logs) - 1)
        if is_last and pending_active:
            # 正常待结算，不处理
            continue

        item["result"] = "异常未结算"
        if item.get("profit") is None:
            item["profit"] = 0
        item["heal_time"] = now_text
        item["heal_note"] = "startup_auto_heal_pending_bet"
        healed_items.append(str(item.get("bet_id") or f"index:{idx}"))

    healed_count = len(healed_items)
    if healed_count > 0:
        rt["pending_bet_heal_total"] = int(rt.get("pending_bet_heal_total", 0) or 0) + healed_count
        rt["pending_bet_last_heal_count"] = healed_count
        rt["pending_bet_last_heal_at"] = now_text

    return {"count": healed_count, "items": healed_items}


def _normalize_ai_keys(ai_cfg: Dict[str, Any]) -> List[str]:
    """统一读取 ai api_keys，兼容旧字段 api_key。"""
    if not isinstance(ai_cfg, dict):
        return []
    raw = ai_cfg.get("api_keys", ai_cfg.get("api_key", []))
    if isinstance(raw, str):
        key = raw.strip()
        return [key] if key else []
    if isinstance(raw, list):
        keys: List[str] = []
        for item in raw:
            text = str(item).strip()
            if text:
                keys.append(text)
        return keys
    return []


def _mask_api_key(key: str) -> str:
    text = str(key or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}***{text[-4:]}"


def _looks_like_ai_key_issue(error_text: str) -> bool:
    text = str(error_text or "").lower()
    if not text:
        return False

    # 明确排除非鉴权问题，避免误判。
    non_auth_signals = ("rate limit", "429", "timeout", "connection", "network")
    if any(sig in text for sig in non_auth_signals):
        return False

    auth_signals = (
        "401",
        "unauthorized",
        "authentication",
        "invalid api key",
        "api key is invalid",
        "invalid token",
        "bad api key",
        "incorrect api key",
        "expired",
        "forbidden",
    )
    return any(sig in text for sig in auth_signals)


def _mark_ai_key_issue(rt: Dict[str, Any], reason: str):
    rt["ai_key_issue_active"] = True
    rt["ai_key_issue_reason"] = str(reason or "")[:200]


def _clear_ai_key_issue(rt: Dict[str, Any]):
    rt["ai_key_issue_active"] = False
    rt["ai_key_issue_reason"] = ""


def _build_ai_key_warning_message(rt: Dict[str, Any]) -> str:
    reason = str(rt.get("ai_key_issue_reason", "")).strip()
    reason_line = f"\n原因：{reason}" if reason else ""
    return (
        f"{AI_KEY_WARNING_TEXT}\n"
        f"当前模型：{rt.get('current_model_id', 'unknown')}{reason_line}\n"
        "请在管理员窗口执行：`apikey set <新key>`"
    )


def get_software_version_text() -> str:
    """返回软件版本展示：tag(hash)。"""
    try:
        info = get_current_repo_info()
        short_commit = info.get("short_commit", "") or "unknown"
        tag = info.get("current_tag", "") or info.get("nearest_tag", "")
        if tag:
            return f"{tag}({short_commit})"
        return short_commit
    except Exception:
        return "unknown"


# 仪表盘格式化 - 与master版本保持一致
def format_dashboard(user_ctx: UserContext) -> str:
    """生成并返回仪表盘信息 - 与master版本format_dashboard一致"""
    state = user_ctx.state
    rt = state.runtime
    
    # 显示近期40次结果（由近及远）
    reversed_data = ["✅" if x == 1 else "❌" for x in state.history[-40:][::-1]]
    mes = f"""📊 **近期 40 次结果**（由近及远）\n✅：大（1）  ❌：小（0）\n{os.linesep.join(
        " ".join(map(str, reversed_data[i:i + 10])) 
        for i in range(0, len(reversed_data), 10)
    )}\n\n———————————————\n🎯 **策略设定**\n"""
    mes += f"🔢 **软件版本：{get_software_version_text()}**\n"
    mes += f"🤖 **模型 API：{rt.get('current_model_id', 'unknown')}**\n"
    preset_name = rt.get("current_preset_name", "none")
    preset_params = (
        f"{rt.get('continuous', 1)} {rt.get('lose_stop', 13)} "
        f"{rt.get('lose_once', 3.0)} {rt.get('lose_twice', 2.1)} "
        f"{rt.get('lose_three', 2.05)} {rt.get('lose_four', 2.0)} {rt.get('initial_amount', 500)}"
    )
    mes += f"📋 **预设名称：{preset_name}**\n"
    mes += f"🤖 **预设参数：{preset_params}**\n"
    mes += f"💰 **初始金额：{rt.get('initial_amount', 500)}**\n⏹ **押注 {rt.get('lose_stop', 13)} 次停止**\n"
    mes += f"💥 **炸 {rt.get('explode', 5)} 次，暂停 {rt.get('stop', 3)} 局**\n📚 **押注倍率：{rt.get('lose_once', 3.0)} / {rt.get('lose_twice', 2.1)} / {rt.get('lose_three', 2.05)} / {rt.get('lose_four', 2.0)}**\n\n"
    
    # 余额显示逻辑 - 与master一致
    balance_status = rt.get('balance_status', 'ok')
    account_balance = rt.get('account_balance', 0)
    
    if balance_status == "auth_failed":
        balance_str = "⚠️ Cookie 失效"
    elif balance_status == "network_error":
        balance_str = "⚠️ 网络错误"
    elif account_balance == 0 and balance_status == "unknown":
        balance_str = "⏳ 获取中..."
    else:
        balance_str = f"{account_balance / 10000:.2f} 万"
        
    mes += f"💰 **账户余额：{balance_str}**\n"
    # 防止资金显示为负数
    display_fund = max(0, rt.get('gambling_fund', 0))
    mes += f"💰 **菠菜余额：{display_fund / 10000:.2f} 万**\n📈 **盈利目标：{rt.get('profit', 1000000) / 10000:.2f} 万，暂停 {rt.get('profit_stop', 5)} 局**\n"
    mes += f"📈 **本轮盈利：{rt.get('period_profit', 0) / 10000:.2f} 万**\n📈 **总盈利：{rt.get('earnings', 0) / 10000:.2f} 万**\n\n"
    
    win_total = rt.get('win_total', 0)
    total = rt.get('total', 0)
    if win_total > 0 or total > 0:
        win_rate = (win_total / total * 100) if total > 0 else 0.00
        mes += f"🎯 **押注次数：{total}**\n🏆 **胜率：{win_rate:.2f}%**\n💰 **收益：{format_number(rt.get('earnings', 0))}**"
    
    return mes


def get_bet_status_text(rt: Dict[str, Any]) -> str:
    """统一押注状态展示。"""
    if rt.get("manual_pause", False):
        return "手动暂停"
    if not rt.get("switch", True):
        return "已关闭"
    if rt.get("bet_on", False):
        return "运行中"
    return "已暂停"


# 消息分发规则表（与 master 一致）
MESSAGE_ROUTING_TABLE = {
    "win": {"channels": ["admin", "priority"], "priority": True},
    "explode": {"channels": ["admin", "priority"], "priority": True},
    "lose_streak": {"channels": ["admin", "priority"], "priority": True},
    "lose_end": {"channels": ["admin", "priority"], "priority": True},
    "fund_pause": {"channels": ["admin", "priority"], "priority": True},
    "goal_pause": {"channels": ["admin", "priority"], "priority": True},
    "risk_pause": {"channels": ["admin"], "priority": False},
    "risk_summary": {"channels": ["admin", "priority"], "priority": True},
    "pause": {"channels": ["admin"], "priority": False},
    "resume": {"channels": ["admin"], "priority": False},
    "settle": {"channels": ["admin"], "priority": False},
    "dashboard": {"channels": ["admin"], "priority": False},
    "info": {"channels": ["admin"], "priority": False},
    "warning": {"channels": ["admin"], "priority": False},
    "error": {"channels": ["admin", "priority"], "priority": True},
}


def _strip_account_prefix(text: str) -> str:
    """管理员消息统一移除账号前缀，与 master 行为一致。"""
    if text is None:
        return ""
    raw = str(text)
    normalized = raw.lstrip()
    if not normalized.startswith("【账号："):
        return raw
    lines = normalized.splitlines()
    if len(lines) <= 1:
        return ""
    return "\n".join(lines[1:]).lstrip("\n")


def _ensure_account_prefix(text: str, account_prefix: str) -> str:
    """重点渠道消息统一补充账号前缀。"""
    content = _strip_account_prefix(text)
    if not content:
        return account_prefix
    return f"{account_prefix}\n{content}"


def _iter_targets(target):
    if isinstance(target, (list, tuple, set)):
        return [item for item in target if item not in (None, "")]
    if target in (None, ""):
        return []
    return [target]


def _resolve_admin_chat(user_ctx: UserContext):
    notification = user_ctx.config.notification if isinstance(user_ctx.config.notification, dict) else {}
    admin_chat = notification.get("admin_chat")
    if admin_chat in (None, ""):
        admin_chat = user_ctx.config.groups.get("admin_chat")
    if isinstance(admin_chat, str):
        text = admin_chat.strip()
        if text.lstrip("-").isdigit():
            try:
                return int(text)
            except Exception:
                return admin_chat
    return admin_chat


async def _post_form_async(url: str, payload: dict, timeout: int = 5):
    """在异步上下文中安全发送 form 请求，避免阻塞事件循环。"""
    return await asyncio.to_thread(requests.post, url, data=payload, timeout=timeout)


async def _post_json_async(url: str, payload: dict, timeout: int = 5):
    """在异步上下文中安全发送 json 请求，避免阻塞事件循环。"""
    return await asyncio.to_thread(requests.post, url, json=payload, timeout=timeout)


async def send_message_v2(
    client,
    msg_type: str,
    message: str,
    user_ctx: UserContext,
    global_config: dict,
    parse_mode: str = "markdown",
    title=None,
    desp=None
):
    """新版统一消息发送函数（多用户版）- 严格按路由表分发。"""
    routing = MESSAGE_ROUTING_TABLE.get(msg_type)
    if routing is None:
        error = f"未定义消息路由: {msg_type}"
        log_event(logging.ERROR, 'send_msg', '消息路由缺失', user_id=user_ctx.user_id, data=error)
        raise ValueError(error)

    channels = routing.get("channels", [])
    account_name = user_ctx.config.name.strip()
    account_prefix = f"【账号：{account_name}】"
    admin_message = _strip_account_prefix(message)
    # 重点通道（IYUU/TG Bot）统一带账号前缀；管理员通道统一不带前缀。
    priority_message = _ensure_account_prefix(message, account_prefix)
    priority_desp = _ensure_account_prefix(desp if desp is not None else message, account_prefix)

    sent_message = None
    if "admin" in channels or "all" in channels:
        try:
            admin_chat = _resolve_admin_chat(user_ctx)
            if admin_chat:
                # 修复：多用户分支 - 返回管理员消息对象，确保仪表盘/统计可被后续刷新删除。
                sent_message = await client.send_message(admin_chat, admin_message, parse_mode=parse_mode)
        except Exception as e:
            log_event(logging.ERROR, 'send_msg', '发送管理员消息失败', user_id=user_ctx.user_id, data=str(e))

    if "priority" in channels or "all" in channels:
        iyuu_cfg = user_ctx.config.notification.get("iyuu", {})
        if iyuu_cfg.get("enable"):
            try:
                final_title = title or f"菠菜机器人 {account_name} 通知"
                payload = {"text": final_title, "desp": priority_desp}
                iyuu_url = iyuu_cfg.get("url")
                if not iyuu_url:
                    token = iyuu_cfg.get("token")
                    iyuu_url = f"https://iyuu.cn/{token}.send" if token else None
                if iyuu_url:
                    await _post_form_async(iyuu_url, payload, timeout=5)
            except Exception as e:
                log_event(logging.ERROR, 'send_msg', 'IYUU通知失败', user_id=user_ctx.user_id, data=str(e))

        tg_bot_cfg = user_ctx.config.notification.get("tg_bot", {})
        if tg_bot_cfg.get("enable"):
            try:
                bot_token = tg_bot_cfg.get("bot_token")
                chat_id = tg_bot_cfg.get("chat_id")
                if bot_token and chat_id:
                    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    payload = {"chat_id": chat_id, "text": priority_message}
                    await _post_json_async(url, payload, timeout=5)
            except Exception as e:
                log_event(logging.ERROR, 'send_msg', 'TG Bot通知失败', user_id=user_ctx.user_id, data=str(e))

    return sent_message


# 兼容旧接口
async def send_message(
    client,
    to: str,
    message: str,
    user_ctx: UserContext,
    global_config: dict,
    parse_mode: str = "markdown",
    title=None,
    desp=None,
    notify_type: str = "info"
):
    msg_type_map = {
        "profit": "win",
        "explode": "explode",
        "lose_streak": "lose_streak",
        "profit_recovery": "lose_end",
        "info": "info",
    }
    msg_type = msg_type_map.get(notify_type, "info")
    if to not in ("admin", "all", "priority", "iyuu", "tgbot"):
        log_event(logging.WARNING, 'send_msg', '旧接口to参数无效，已按路由表处理', user_id=user_ctx.user_id, data=f"to={to}, type={msg_type}")
        to = "admin"

    if to == "admin":
        return await send_message_v2(client, "info", message, user_ctx, global_config, parse_mode, title, desp)
    if to == "all":
        return await send_message_v2(client, msg_type, message, user_ctx, global_config, parse_mode, title, desp)

    # priority/iyuu/tgbot 兼容：仅走重点渠道
    account_name = user_ctx.config.name.strip()
    account_prefix = f"【账号：{account_name}】"
    priority_message = _ensure_account_prefix(message, account_prefix)
    priority_desp = _ensure_account_prefix(desp if desp is not None else message, account_prefix)
    if to in ("priority", "iyuu"):
        iyuu_cfg = user_ctx.config.notification.get("iyuu", {})
        if iyuu_cfg.get("enable"):
            final_title = title or f"菠菜机器人 {account_name} 通知"
            payload = {"text": final_title, "desp": priority_desp}
            iyuu_url = iyuu_cfg.get("url")
            if not iyuu_url:
                token = iyuu_cfg.get("token")
                iyuu_url = f"https://iyuu.cn/{token}.send" if token else None
            if iyuu_url:
                await _post_form_async(iyuu_url, payload, timeout=5)
    if to in ("priority", "tgbot"):
        tg_bot_cfg = user_ctx.config.notification.get("tg_bot", {})
        if tg_bot_cfg.get("enable"):
            bot_token = tg_bot_cfg.get("bot_token")
            chat_id = tg_bot_cfg.get("chat_id")
            if bot_token and chat_id:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                payload = {"chat_id": chat_id, "text": priority_message}
                await _post_json_async(url, payload, timeout=5)
    return None


async def send_to_admin(client, message: str, user_ctx: UserContext, global_config: dict):
    return await send_message_v2(client, "info", message, user_ctx, global_config)


async def _send_transient_admin_notice(
    client,
    user_ctx: UserContext,
    global_config: dict,
    message: str,
    ttl_seconds: int = 120,
    attr_name: str = "transient_notice_message",
):
    """
    发送“短时说明通知”（用于暂停结束/恢复等状态提示）：
    - 刷新式保留最后一条
    - 到期自动删除，减少消息堆积
    """
    old_message = getattr(user_ctx, attr_name, None)
    if old_message:
        await cleanup_message(client, old_message)
    sent = await send_to_admin(client, message, user_ctx, global_config)
    if sent:
        setattr(user_ctx, attr_name, sent)
        chat_id = getattr(sent, "chat_id", None)
        msg_id = getattr(sent, "id", None)
        if chat_id is not None and msg_id is not None and ttl_seconds > 0:
            asyncio.create_task(delete_later(client, chat_id, msg_id, ttl_seconds))
    return sent


# ==================== V10 M-SMP 核心算法函数 ====================

def calculate_trend_gap(history, window=100):
    """
    计算趋势缺口：最近N期内"大"和"小"偏离50/50均衡线的数值
    返回: {
        'big_ratio': 大占比,
        'small_ratio': 小占比,
        'deviation_score': 标准差/偏离度,
        'gap': 向均值靠拢的缺口(正=缺大, 负=缺小),
        'regression_target': 统计学理论预测目标(0或1)
    }
    """
    if len(history) < window:
        window = len(history)
    
    recent = history[-window:]
    big_count = sum(recent)
    small_count = window - big_count
    
    big_ratio = big_count / window if window > 0 else 0.5
    small_ratio = small_count / window if window > 0 else 0.5
    
    deviation_score = abs(big_ratio - 0.5) * 2
    
    gap = (window / 2) - big_count
    
    regression_target = 1 if big_count < small_count else 0
    
    return {
        'big_ratio': round(big_ratio, 3),
        'small_ratio': round(small_ratio, 3),
        'deviation_score': round(deviation_score, 3),
        'gap': int(gap),
        'regression_target': regression_target,
        'big_count': big_count,
        'small_count': small_count
    }


def extract_pattern_features(history):
    """
    提取形态特征：自动检测单跳、长龙、对称环绕等状态
    返回: {
        'pattern_tag': 形态标签,
        'tail_streak_len': 尾部连龙长度,
        'tail_streak_char': 尾部连龙字符(0/1),
        'is_alternating': 是否单跳模式,
        'is_symmetric': 是否对称环绕
    }
    """
    if not history or len(history) < 3:
        return {
            'pattern_tag': 'INSUFFICIENT_DATA',
            'tail_streak_len': 0,
            'tail_streak_char': None,
            'is_alternating': False,
            'is_symmetric': False
        }
    
    seq_str = ''.join(['1' if x == 1 else '0' for x in history])
    
    tail_char = seq_str[-1]
    tail_streak_len = 1
    for i in range(len(seq_str) - 2, -1, -1):
        if seq_str[i] == tail_char:
            tail_streak_len += 1
        else:
            break
    
    is_alternating = False
    if len(seq_str) >= 6:
        recent_6 = seq_str[-6:]
        if recent_6 in ['010101', '101010']:
            is_alternating = True
    
    is_symmetric = False
    if len(seq_str) >= 5:
        recent_5 = seq_str[-5:]
        if recent_5 == recent_5[::-1]:
            is_symmetric = True
    
    if tail_streak_len >= 5:
        pattern_tag = 'LONG_DRAGON'
    elif tail_streak_len >= 3:
        pattern_tag = 'DRAGON_CANDIDATE'
    elif is_alternating:
        pattern_tag = 'SINGLE_JUMP'
    elif is_symmetric:
        pattern_tag = 'SYMMETRIC_WRAP'
    else:
        pattern_tag = 'CHAOS_SWITCH'
    
    return {
        'pattern_tag': pattern_tag,
        'tail_streak_len': tail_streak_len,
        'tail_streak_char': int(tail_char),
        'is_alternating': is_alternating,
        'is_symmetric': is_symmetric
    }


def fallback_prediction(history):
    """
    天眼兜底机制：如果AI异常，强行维持50:50概率
    缺哪个补哪个，绝不暂停！
    """
    if not history:
        return 1
    
    window = min(40, len(history))
    recent = history[-window:]
    big_count = sum(recent)
    small_count = window - big_count
    
    prediction = 1 if big_count < small_count else 0
    
    log_event(logging.WARNING, 'predict_v10', '天眼兜底触发', 
              user_id=0, data=f'big={big_count}, small={small_count}, fallback={prediction}')
    
    return prediction


def parse_analysis_result_insight(resp_text, default_prediction=1):
    """
    天眼模式：解析AI输出，绝不返回暂停
    只返回0或1，confidence和reason
    """
    try:
        cleaned = str(resp_text).replace('```json', '').replace('```', '').strip()
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:].strip()
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start >= 0 and end > start:
            cleaned = cleaned[start:end + 1]
        resp_json = json.loads(cleaned)
        
        prediction = resp_json.get('prediction', default_prediction)
        if isinstance(prediction, str):
            prediction = 1 if prediction.upper() in ['1', 'B', 'BIG', '大'] else 0
        prediction = int(prediction)
        if prediction not in [0, 1]:
            prediction = default_prediction
        
        confidence = int(resp_json.get('confidence', 50))
        confidence = max(0, min(100, confidence))
        
        reason = resp_json.get('reason', resp_json.get('logic', '天眼分析'))
        
        return {
            'prediction': prediction,
            'confidence': confidence,
            'reason': reason
        }
    except Exception as e:
        return {
            'prediction': default_prediction,
            'confidence': 50,
            'reason': f'解析兜底:{str(e)[:20]}'
        }


# V10 预测函数 - M-SMP架构
async def predict_next_bet_v10(user_ctx: UserContext, global_config: dict, current_round: int = 1) -> int:
    """
    V10 深度量化博弈版：多策略模拟预测（M-SMP）架构
    核心逻辑：多策略人格模拟博弈，强制输出0或1，绝不暂停！
    """
    state = user_ctx.state
    rt = state.runtime
    history = state.history
    
    try:
        # ========== 第一步：构建三维历史快照（交易员终端感） ==========
        
        # 1.1 短期精确抖动（20局）
        short_term_20 = history[-20:] if len(history) >= 20 else history[:]
        short_str = "".join(['1' if x == 1 else '0' for x in short_term_20])
        
        # 1.2 中期暗趋势（50局）
        medium_term_50 = history[-50:] if len(history) >= 50 else history[:]
        medium_str = "".join(['1' if x == 1 else '0' for x in medium_term_50])
        
        # 1.3 长期大周期回归（100局）
        long_term_100 = history[-100:] if len(history) >= 100 else history[:]
        long_term_gap = round(sum(long_term_100) / len(long_term_100), 3) if long_term_100 else 0.5
        
        # 1.4 趋势缺口计算
        trend_gap = calculate_trend_gap(history, window=100)
        big_cnt = trend_gap['big_count']
        small_cnt = trend_gap['small_count']
        gap = trend_gap['gap']
        
        # 1.5 形态特征
        pattern_features = extract_pattern_features(history)
        pattern_tag = pattern_features['pattern_tag']
        tail_streak_len = pattern_features['tail_streak_len']
        tail_streak_char = pattern_features['tail_streak_char']
        
        # 1.6 模式标记
        lose_count = rt.get('lose_count', 0)
        entropy_tag = "Pattern_Breaking" if lose_count > 2 else "Stability"
        
        # ========== 第二步：构建交易员终端数据负载 ==========
        
        payload = {
            "current_status": {
                "martingale_step": lose_count + 1,
                "total_profit_to_date": rt.get('earnings', 0),
                "entropy_tag": entropy_tag
            },
            "history_views": {
                "short_term_20": short_str,
                "medium_term_50": medium_str,
                "long_term_gap": long_term_gap,
                "big_count_100": big_cnt,
                "small_count_100": small_cnt
            },
            "pattern_analysis": {
                "tag": pattern_tag,
                "tail_streak_len": tail_streak_len,
                "tail_streak_char": tail_streak_char,
                "gap": f"{gap:+d}"
            }
        }
        
        # ========== 第三步：深度博弈推理Prompt（M-SMP架构） ==========
        
        current_model_id = rt.get('current_model_id', 'qwen3-coder-plus')
        
        prompt = f"""[System Instruction]
你是专门破解博弈陷阱的量化交易员。在任何情况下，你的预测严禁输出"NONE"、"SKIP"或建议观望。

[Context Reasoning Flow]
请按顺序执行以下深度推理步骤：

1. 趋势派模型（Trend Following）:
检查当前是否有活跃长龙（Streak > 5）与短线惯性，评估顺行阻力与逆行阻力。

2. 数学均衡派模型（Mean Reversion）:
分析过去100局分布，判断当前是否出现显著均值回归压力，但不要机械逆势。

3. 形态锁定（Sequential Match）:
在历史片段中寻找相似尾部结构，识别惯性延续或衰竭拐点。

4. 长连顺势加权（新增软偏置）:
- 当尾部连数 >= 4：提高顺势方案权重（更倾向顺着当前方向下注）。
- 当尾部连数 >= 5：默认顺势，除非“强反转证据”至少满足2条才可逆势。
- 强反转证据示例：
  A) |gap| >= 8 且明确指向反向修复；
  B) long_term_gap 极端（>=0.60 或 <=0.40）且短期20局同向过热（>=15/20）；
  C) LONG_DRAGON 且尾部连数 >= 6，同时短期结构出现耗竭信号。
- 若倍投压力第5手及以上，逆势需要更高把握；同等证据下优先顺势。

[Data Evidence]
短期20局: {short_str}
中期50局: {medium_str}
长期100局大占比: {long_term_gap}
当前形态: {pattern_tag} (尾部{tail_streak_len}连{'大' if tail_streak_char==1 else '小'})
大数缺口: {gap:+d} (正=缺大, 负=缺小)
倍投压力: 第{lose_count + 1}次 ({entropy_tag})

[Final Choice]
当趋势与回归冲突时，不要“一刀切逆势”；请先比较证据强度：
- 证据接近或不充分：顺势优先；
- 证据明显支持反转：允许逆势，并在 reasoning 中说明触发了哪两条强证据。

你必须给出一个自信得分。但无论分值多低，prediction 只能选 0 或 1。

[Response Format]
必须且只能输出如下 JSON：
{{"logic": "50字内分析证据流", "reasoning": "你是顺风追龙还是逆风阻杀龙的原因", "confidence": 1-100, "prediction": 0或1}}

记住：系统已废除暂停机制，你必须给出0或1！"""

        messages = [
            {'role': 'system', 'content': '你是专门破解博弈陷阱的量化交易员，只输出纯JSON，严禁解释性文本，严禁输出NONE或SKIP。'},
            {'role': 'user', 'content': prompt}
        ]
        
        log_event(logging.INFO, 'predict_v10', f'M-SMP模式调用: {current_model_id}', 
                  user_id=user_ctx.user_id, data=f'形态:{pattern_tag} 缺口:{gap:+d} 压力:{lose_count + 1}次')
        
        # ========== 第四步：调用模型与多层兜底 ==========

        model_used = True
        try:
            configured_keys = _normalize_ai_keys(user_ctx.config.ai if isinstance(user_ctx.config.ai, dict) else {})
            if not configured_keys:
                raise Exception("AI_KEY_MISSING")

            result = await user_ctx.get_model_manager().call_model(
                current_model_id,
                messages,
                temperature=0.1,
                max_tokens=500
            )
            if not result['success']:
                raise Exception(f"Model Error: {result['error']}")

            _clear_ai_key_issue(rt)
            
            default_pred = trend_gap['regression_target']
            final_result = parse_analysis_result_insight(result['content'], default_prediction=default_pred)
            
        except Exception as model_error:
            model_used = False
            err_text = str(model_error)
            if "AI_KEY_MISSING" in err_text:
                _mark_ai_key_issue(rt, "未配置可用 api_keys")
            elif _looks_like_ai_key_issue(err_text):
                _mark_ai_key_issue(rt, err_text)
            log_event(logging.WARNING, 'predict_v10', '模型调用失败，统计兜底', 
                      user_id=user_ctx.user_id, data=err_text)
            final_result = {
                'prediction': trend_gap['regression_target'],
                'confidence': 50,
                'reason': '模型异常，统计回归兜底'
            }
        
        # ========== 第五步：结果强制校验与记录 ==========
        
        prediction = final_result['prediction']
        confidence = final_result['confidence']
        reason = final_result.get('reason', final_result.get('logic', '深度分析'))
        
        if prediction not in [0, 1]:
            prediction = trend_gap['regression_target']
            confidence = 50
            reason = '强制校正：统计回归'
        
        # 构建预测信息
        rt["last_predict_info"] = (
            f"M-SMP/{pattern_tag} | {reason} | 信:{confidence}% | "
            f"缺口:{gap:+d} | 回归:{trend_gap['regression_target']}"
        )
        rt["last_predict_tag"] = pattern_tag
        rt["last_predict_confidence"] = int(confidence)
        rt["last_predict_source"] = "model" if model_used else "fallback"
        rt["last_predict_reason"] = reason
        
        # 审计日志
        audit_log = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "round": current_round,
            "mode": "M-SMP",
            "input_payload": payload,
            "output": final_result,
            "model_id": current_model_id,
        }
        rt["last_logic_audit"] = json.dumps(audit_log, ensure_ascii=False, indent=2)
        
        # 写入用户目录下的decisions.log
        user_dir = user_ctx.user_dir
        decisions_log_path = os.path.join(user_dir, "decisions.log")
        try:
            with open(decisions_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit_log, ensure_ascii=False) + "\n")
        except Exception as e:
            log_event(logging.WARNING, 'predict_v10', '写入decisions.log失败', 
                      user_id=user_ctx.user_id, data=str(e))
        
        # 记录预测
        state.predictions.append(prediction)
        
        log_event(logging.INFO, 'predict_v10', 'M-SMP预测完成', 
                  user_id=user_ctx.user_id, data=f'pred={prediction}, conf={confidence}, pattern={pattern_tag}')
        
        return prediction
        
    except Exception as e:
        log_event(logging.ERROR, 'predict_v10', 'M-SMP异常，最终保底', 
                  user_id=user_ctx.user_id, data=str(e))
        
        recent_20 = history[-20:] if len(history) >= 20 else history
        recent_sum = sum(recent_20)
        fallback = 0 if recent_sum >= len(recent_20) / 2 else 1
        
        rt["last_predict_info"] = f"M-SMP终极保底 | 强制预测:{fallback}"
        rt["last_predict_tag"] = "FALLBACK"
        rt["last_predict_confidence"] = 0
        rt["last_predict_source"] = "hard_fallback"
        rt["last_predict_reason"] = "M-SMP异常终极保底"
        state.predictions.append(fallback)
        return fallback


# 押注处理
async def process_bet_on(client, event, user_ctx: UserContext, global_config: dict):
    state = user_ctx.state
    rt = state.runtime

    timing_cfg = _read_timing_config(global_config)
    prompt_wait_sec = timing_cfg["prompt_wait_sec"]
    predict_timeout_sec = timing_cfg["predict_timeout_sec"]
    click_interval_sec = timing_cfg["click_interval_sec"]
    click_timeout_sec = timing_cfg["click_timeout_sec"]

    # 固定长等待会错过下注窗口，改为轻量等待回调按钮就绪。
    if not getattr(event, "reply_markup", None) and prompt_wait_sec > 0:
        await asyncio.sleep(prompt_wait_sec)

    text = event.message.message

    if not rt.get("switch", True):
        log_event(logging.INFO, 'bet_on', 'off 命令触发，预测及下注路径已关闭', user_id=user_ctx.user_id)
        if rt.get("bet", False):
            await send_to_admin(client, "押注已关闭，无法执行", user_ctx, global_config)
            rt["bet"] = False
            user_ctx.save_state()
        return

    if rt.get("manual_pause", False):
        await _clear_pause_countdown_notice(client, user_ctx)
        if rt.get("bet", False):
            rt["bet"] = False
            user_ctx.save_state()
        log_event(logging.DEBUG, 'bet_on', '手动暂停中，跳过押注', user_id=user_ctx.user_id)
        return

    stop_count = int(rt.get("stop_count", 0))
    if stop_count > 0:
        rt["stop_count"] = stop_count - 1
        if rt["stop_count"] == 0:
            await _clear_pause_countdown_notice(client, user_ctx)
            if rt.get("manual_pause", False):
                rt["bet"] = False
                rt["bet_on"] = False
                rt["mode_stop"] = True
                rt["flag"] = True
                user_ctx.save_state()
                await _send_transient_admin_notice(
                    client,
                    user_ctx,
                    global_config,
                    "⏸️ 自动暂停倒计时结束\n当前处于手动暂停，保持暂停状态\n如需恢复请发送：resume",
                    ttl_seconds=90,
                    attr_name="pause_transition_message",
                )
                return

            rt["bet"] = True
            rt["bet_on"] = True
            rt["mode_stop"] = True
            rt["flag"] = True
            rt["pause_resume_pending"] = True
            rt["pause_resume_pending_reason"] = str(rt.get("pause_countdown_reason", "自动暂停")).strip() or "自动暂停"
            user_ctx.save_state()
        else:
            await _refresh_pause_countdown_notice(
                client,
                user_ctx,
                global_config,
                remaining_rounds=max(int(rt["stop_count"]) - 1, 0),
            )
            user_ctx.save_state()
            log_event(logging.INFO, 'bet_on', '暂停中跳过押注', user_id=user_ctx.user_id, data=f"stop_count={rt['stop_count']}")
            return

    # 修复：多用户分支 - 更稳健解析历史串（支持换行/多空格），尽量回填更多历史。
    try:
        import re
        history_match = re.search(r"\[0\s*小\s*1\s*大\]([\s\S]*)", text)
        if history_match:
            history_str = history_match.group(1)
            new_history = [int(x) for x in re.findall(r"(?<!\d)[01](?!\d)", history_str)]
            if new_history and len(new_history) >= len(state.history):
                state.history = new_history[-2000:]
    except Exception as e:
        log_event(logging.WARNING, 'bet_on', '解析历史数据失败', user_id=user_ctx.user_id, data=str(e))

    # 修复：对齐master分支 - 历史不足40局也允许继续押注（预测函数已具备短历史兜底）。
    if len(state.history) < 40:
        log_event(logging.INFO, 'bet_on', '历史数据低于40局，继续执行押注', user_id=user_ctx.user_id, data=f'len={len(state.history)}')

    # 自动风控暂停：基础风控(40局窗口) + 深度风控(每3连输里程碑)。
    # 同一已结算快照不重复触发，避免重复暂停。
    next_sequence = int(rt.get("bet_sequence_count", 0)) + 1
    settled_count = _count_settled_bets(state)
    snapshot_count = int(rt.get("risk_pause_snapshot_count", -1))
    pause_acc_rounds = int(rt.get("risk_pause_acc_rounds", 0))

    skip_same_snapshot = (snapshot_count == settled_count)
    risk_pause = {} if skip_same_snapshot else _evaluate_auto_risk_pause(state, rt, next_sequence)

    cycle_active = bool(rt.get("risk_pause_cycle_active", False))
    recovery_passes = int(rt.get("risk_pause_recovery_passes", 0))
    base_hit_streak = int(rt.get("risk_base_hit_streak", 0))

    # 风险周期恢复判定：最近40笔胜率>45% 连续2次才重置预算。
    if not skip_same_snapshot and risk_pause:
        if risk_pause.get("base_trigger", False):
            base_hit_streak += 1
        else:
            base_hit_streak = 0
        rt["risk_base_hit_streak"] = base_hit_streak

        if risk_pause.get("recovery_hit", False):
            if cycle_active:
                recovery_passes += 1
            else:
                recovery_passes = 0
        else:
            recovery_passes = 0
        rt["risk_pause_recovery_passes"] = recovery_passes

    if cycle_active and recovery_passes >= RISK_RECOVERY_PASS_NEEDED:
        rt["risk_pause_cycle_active"] = False
        rt["risk_pause_acc_rounds"] = 0
        rt["risk_pause_snapshot_count"] = -1
        rt["risk_pause_recovery_passes"] = 0
        rt["risk_base_hit_streak"] = 0
        rt["risk_pause_priority_notified"] = False
        log_event(
            logging.INFO,
            "risk_pause",
            "风控周期恢复，已重置暂停预算",
            user_id=user_ctx.user_id,
            data=f"wins>45% pass={RISK_RECOVERY_PASS_NEEDED}",
        )
        pause_acc_rounds = 0
        cycle_active = False

    # 深度风控已迁移到结算阶段触发（输单结果出来即触发），下注入口不再重复触发深度风控。

    # 基础风控：40局<=37.5% 且连续2次命中后才触发，使用10局基础预算。
    if risk_pause.get("base_trigger", False) and base_hit_streak >= RISK_BASE_TRIGGER_STREAK_NEEDED:
        remain_pause_budget = max(0, RISK_PAUSE_TOTAL_CAP_ROUNDS - pause_acc_rounds)
        rt["risk_pause_cycle_active"] = True
        rt["risk_pause_snapshot_count"] = settled_count

        if remain_pause_budget <= 0:
            warn_msg = (
                "⚠️ 基础风控暂停已达上限\n"
                f"基础风控累计暂停已达 {RISK_PAUSE_TOTAL_CAP_ROUNDS} 局，本局继续下注。\n"
                "动作：保留当前倍投进度，后续按新结算数据继续评估风控。"
            )
            await send_to_admin(client, warn_msg, user_ctx, global_config)
            user_ctx.save_state()
            log_event(
                logging.INFO,
                'bet_on',
                '基础风控暂停达上限，放行下注',
                user_id=user_ctx.user_id,
                data=f'settled_count={settled_count}, cap={RISK_PAUSE_TOTAL_CAP_ROUNDS}'
            )
        else:
            layer_cap = int(RISK_BASE_MAX_PAUSE_ROUNDS)
            max_allow_rounds = max(1, min(layer_cap, remain_pause_budget))
            model_eval = {
                **risk_pause,
                "level": "BASE",
                "level_label": "基础风控",
            }
            model_pause_rounds, model_reason, model_source = await _suggest_pause_rounds_by_model(
                user_ctx,
                model_eval,
                max_pause=max_allow_rounds,
            )
            pause_rounds = max(1, min(max_allow_rounds, int(model_pause_rounds)))
            _enter_pause(rt, pause_rounds, "基础风控暂停")
            rt["risk_pause_acc_rounds"] = pause_acc_rounds + pause_rounds
            rt["risk_pause_snapshot_count"] = settled_count
            rt["risk_pause_block_hits"] = int(rt.get("risk_pause_block_hits", 0)) + 1
            rt["risk_pause_block_rounds"] = int(rt.get("risk_pause_block_rounds", 0)) + pause_rounds
            user_ctx.save_state()

            wins = risk_pause.get("wins", 0)
            total = risk_pause.get("total", RISK_WINDOW_BETS)
            win_rate = risk_pause.get("win_rate", 0.0) * 100
            reason_text = "、".join(risk_pause.get("reasons", [])) or "盘面波动风控"
            resume_hint = _build_pause_resume_hint(rt)
            pause_msg = (
                "⛔ 自动风控暂停（已生效）\n"
                "触发层级：基础风控\n"
                f"触发原因：{reason_text}\n"
                f"最近{total}笔胜率：{wins}/{total}（{win_rate:.1f}%）\n"
                f"触发点：第 {next_sequence} 手下注前\n"
                f"模型建议：{model_pause_rounds} 局（来源：{model_source}）\n"
                f"本次暂停：{pause_rounds} 局（连续命中 {base_hit_streak}/{RISK_BASE_TRIGGER_STREAK_NEEDED}，基础预算累计 {rt.get('risk_pause_acc_rounds', 0)}/{RISK_PAUSE_TOTAL_CAP_ROUNDS}）\n"
                f"模型依据：{model_reason}\n"
                "暂停期间：保留当前倍投进度，不会重置首注\n"
                f"{resume_hint}"
            )

            # 刷新式提示：管理员窗口仅保留最后一条风控暂停提示。
            if hasattr(user_ctx, "risk_pause_message") and user_ctx.risk_pause_message:
                await cleanup_message(client, user_ctx.risk_pause_message)

            user_ctx.risk_pause_message = await send_to_admin(client, pause_msg, user_ctx, global_config)
            await _refresh_pause_countdown_notice(
                client,
                user_ctx,
                global_config,
                remaining_rounds=pause_rounds,
            )
            rt["risk_pause_priority_notified"] = True

            log_event(
                logging.INFO,
                'bet_on',
                '触发自动风控暂停',
                user_id=user_ctx.user_id,
                data=(
                    f"wins={wins}/{total}, wr={win_rate:.2f}%, "
                    f"next_seq={next_sequence}, pause_rounds={pause_rounds}, "
                    "level=BASE, "
                    f"pause_acc={rt.get('risk_pause_acc_rounds', 0)}"
                ),
            )
            return

    bet_amount = calculate_bet_amount(rt)
    if bet_amount <= 0:
        if not rt.get("limit_stop_notified", False):
            lose_stop = int(rt.get("lose_stop", 13))
            mes = (
                "⚠️ 已达到预设连投上限，已自动暂停\n"
                f"当前预设最多连投：{lose_stop} 手\n"
                "可等待新轮次或切换预设后继续"
            )
            await send_to_admin(client, mes, user_ctx, global_config)
            rt["limit_stop_notified"] = True
        rt["bet"] = False
        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        user_ctx.save_state()
        return
    rt["limit_stop_notified"] = False

    if not is_fund_available(user_ctx, bet_amount):
        if _sync_fund_from_account_when_insufficient(rt, bet_amount):
            log_event(
                logging.INFO,
                'bet_on',
                '资金不足触发资金同步',
                user_id=user_ctx.user_id,
                data=f"need={bet_amount}, fund={rt.get('gambling_fund', 0)}, account={rt.get('account_balance', 0)}",
            )
            user_ctx.save_state()

        if not is_fund_available(user_ctx, bet_amount):
            if not rt.get("fund_pause_notified", False):
                display_fund = max(0, rt.get("gambling_fund", 0))
                mes = f"**菠菜资金不足，已暂停押注**\n当前剩余：{display_fund / 10000:.2f} 万\n请使用 `gf [金额]` 恢复"
                await send_message_v2(
                    client,
                    "fund_pause",
                    mes,
                    user_ctx,
                    global_config,
                    title=f"菠菜机器人 {user_ctx.config.name} 资金风控暂停",
                    desp=mes,
                )
                rt["fund_pause_notified"] = True
            rt["bet"] = False
            rt["bet_on"] = False
            rt["mode_stop"] = True
            _clear_lose_recovery_tracking(rt)
            user_ctx.save_state()
            return
    rt["fund_pause_notified"] = False

    if not (rt.get("bet_on", False) or rt.get("mode_stop", True)):
        log_event(logging.DEBUG, 'bet_on', '押注已暂停', user_id=user_ctx.user_id)
        return

    if not event.reply_markup:
        rt["bet"] = False
        user_ctx.save_state()
        return

    log_event(logging.INFO, 'bet_on', '开始押注', user_id=user_ctx.user_id)
    try:
        rt["last_predict_info"] = "初始化预测"
        rt["last_predict_source"] = "unknown"
        rt["last_predict_confidence"] = 0
        rt["last_predict_tag"] = ""
        try:
            prediction = await asyncio.wait_for(
                predict_next_bet_v10(user_ctx, global_config),
                timeout=predict_timeout_sec,
            )
        except asyncio.TimeoutError:
            prediction = None
            rt["last_predict_info"] = "预测超时 - 本局不下注"
            rt["last_predict_source"] = "timeout"
            rt["last_predict_tag"] = "TIMEOUT"
            rt["last_predict_confidence"] = 0
            log_event(
                logging.WARNING,
                'bet_on',
                '预测超时，本局放弃下注',
                user_id=user_ctx.user_id,
                timeout=predict_timeout_sec,
            )
            timeout_gate = {
                "blocked": True,
                "gate_name": "模型可用性门控（超时）",
                "pause_rounds": 1,
                "reason_text": f"模型响应超过 {predict_timeout_sec:.1f}s，风险过高，跳过本局",
                "source": "timeout",
                "tag": "TIMEOUT",
                "confidence": 0,
                "wins": risk_pause.get("wins", 0),
                "total": risk_pause.get("total", 0),
                "win_rate": risk_pause.get("win_rate", 0.0),
            }
            await _apply_entry_gate_pause(client, user_ctx, global_config, timeout_gate, next_sequence)
            return

        if prediction in (-1, None):
            rt["last_predict_info"] = "预测结果无效 - 本局不下注"
            rt["last_predict_source"] = "invalid"
            invalid_gate = {
                "blocked": True,
                "gate_name": "模型可用性门控（无效结果）",
                "pause_rounds": 1,
                "reason_text": "模型返回结果无效，跳过本局",
                "source": "invalid",
                "tag": str(rt.get("last_predict_tag", "") or "UNKNOWN"),
                "confidence": int(rt.get("last_predict_confidence", 0) or 0),
                "wins": risk_pause.get("wins", 0),
                "total": risk_pause.get("total", 0),
                "win_rate": risk_pause.get("win_rate", 0.0),
            }
            await _apply_entry_gate_pause(client, user_ctx, global_config, invalid_gate, next_sequence)
            return

        predict_source = str(rt.get("last_predict_source", "")).lower().strip()
        if predict_source in ("", "unknown"):
            # 兼容测试桩/旧逻辑：返回了有效 prediction 但未写入来源时，按模型成功处理。
            predict_source = "model"
            rt["last_predict_source"] = "model"

        if predict_source in {"timeout", "fallback", "hard_fallback", "invalid"}:
            non_model_gate = {
                "blocked": True,
                "gate_name": "模型可用性门控（异常回退）",
                "pause_rounds": 1,
                "reason_text": "当前预测来自回退通道，信号不稳定，跳过本局",
                "source": predict_source,
                "tag": str(rt.get("last_predict_tag", "") or "UNKNOWN"),
                "confidence": int(rt.get("last_predict_confidence", 0) or 0),
                "wins": risk_pause.get("wins", 0),
                "total": risk_pause.get("total", 0),
                "win_rate": risk_pause.get("win_rate", 0.0),
            }
            await _apply_entry_gate_pause(client, user_ctx, global_config, non_model_gate, next_sequence)
            return

        quality_gate = _evaluate_entry_quality_gate(rt, risk_pause, next_sequence)
        if quality_gate.get("blocked", False):
            await _apply_entry_gate_pause(client, user_ctx, global_config, quality_gate, next_sequence)
            return

        if rt.get("ai_key_issue_active", False):
            await send_to_admin(client, _build_ai_key_warning_message(rt), user_ctx, global_config)

        rt["bet_amount"] = int(bet_amount)
        direction = "大" if prediction == 1 else "小"
        direction_en = "big" if prediction == 1 else "small"
        buttons = constants.BIG_BUTTON if prediction == 1 else constants.SMALL_BUTTON
        combination = constants.find_combination(rt["bet_amount"], buttons)

        if not combination:
            rt["bet"] = False
            log_event(logging.WARNING, 'bet_on', '未找到金额组合', user_id=user_ctx.user_id, data=f"amount={rt['bet_amount']}")
            user_ctx.save_state()
            return

        for amount in combination:
            button_data = buttons.get(amount)
            if button_data is not None:
                await asyncio.wait_for(
                    _click_bet_button_with_recover(client, event, user_ctx, button_data),
                    timeout=click_timeout_sec,
                )
                await asyncio.sleep(click_interval_sec)

        rt["bet"] = True
        rt["total"] = rt.get("total", 0) + 1
        rt["bet_sequence_count"] = rt.get("bet_sequence_count", 0) + 1
        rt["bet_type"] = 1 if prediction == 1 else 0
        rt["bet_on"] = True
        rt["fund_pause_notified"] = False
        rt["limit_stop_notified"] = False

        bet_id = generate_bet_id(user_ctx)
        state.bet_sequence_log.append({
            "bet_id": bet_id,
            "sequence": rt.get("bet_sequence_count", 0),
            "direction": direction_en,
            "amount": rt["bet_amount"],
            "result": None,
            "profit": 0,
            "lose_stop": rt.get("lose_stop", 13),
            "profit_target": rt.get("profit", 1000000)
        })
        state.bet_sequence_log = state.bet_sequence_log[-5000:]

        bet_report = generate_mobile_bet_report(
            state.history,
            direction,
            rt["bet_amount"],
            rt.get("bet_sequence_count", 1),
            bet_id
        )
        message = await send_to_admin(client, bet_report, user_ctx, global_config)
        asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
        if message:
            asyncio.create_task(delete_later(client, message.chat_id, message.id, 100))

        # 仅在“暂停后首次真正下单”时发送恢复说明，避免倒计时结束后反复刷“恢复押注”。
        if rt.get("pause_resume_pending", False):
            reason_text = str(rt.get("pause_resume_pending_reason", "自动暂停")).strip() or "自动暂停"
            resume_msg = (
                "✅ 恢复押注（已执行）\n"
                f"恢复原因：{reason_text} 倒计时结束\n"
                f"当前动作：已执行第 {rt.get('bet_sequence_count', 1)} 手，方向 {direction}，金额 {format_number(rt['bet_amount'])}\n"
                "提示：若盘面仍触发风控，会再次自动暂停"
            )
            await _send_transient_admin_notice(
                client,
                user_ctx,
                global_config,
                resume_msg,
                ttl_seconds=120,
                attr_name="pause_transition_message",
            )
            rt["pause_resume_pending"] = False
            rt["pause_resume_pending_reason"] = ""

        rt["current_bet_seq"] = int(rt.get("current_bet_seq", 1)) + 1
        user_ctx.save_state()
    except Exception as e:
        if _is_invalid_callback_message_error(e):
            log_event(logging.WARNING, 'bet_on', '下注窗口失效，已跳过本轮', user_id=user_ctx.user_id, data=str(e))
            await send_to_admin(client, "本轮下注窗口已失效，已自动跳过。", user_ctx, global_config)
        else:
            log_event(logging.ERROR, 'bet_on', '押注失败', user_id=user_ctx.user_id, data=str(e))
            await send_to_admin(client, f"押注出错: {e}", user_ctx, global_config)


# 结算处理
async def cleanup_message(client, message_ref):
    """安全地删除指定消息对象。"""
    if not message_ref:
        return
    try:
        await message_ref.delete()
        return
    except Exception:
        pass
    try:
        chat_id = getattr(message_ref, "chat_id", None)
        msg_id = getattr(message_ref, "id", None)
        if chat_id is not None and msg_id is not None:
            await client.delete_messages(chat_id, msg_id)
    except Exception:
        pass


async def process_red_packet(client, event, user_ctx: UserContext, global_config: dict):
    """处理红包消息，尝试领取。"""
    sender_id = getattr(event, "sender_id", None)
    zq_bot = user_ctx.config.groups.get("zq_bot")
    zq_bot_targets = {str(item) for item in _iter_targets(zq_bot)}
    if zq_bot_targets and str(sender_id) not in zq_bot_targets:
        return

    text = (getattr(event, "raw_text", None) or getattr(event, "text", None) or "").strip()
    if not text:
        return

    reply_markup = getattr(event, "reply_markup", None)
    rows = getattr(reply_markup, "rows", None) if reply_markup else None
    if not rows:
        return

    red_keywords = ("红包", "领取", "抢红包", "red", "packet", "hongbao", "claim")
    game_keywords = ("游戏", "对战", "闯关", "开局", "竞猜", "匹配", "挑战", "start game")
    lower_text = text.lower()

    callback_buttons = []
    red_button_candidates = []
    for row_idx, row in enumerate(rows):
        for btn_idx, btn in enumerate(getattr(row, "buttons", None) or []):
            btn_data = getattr(btn, "data", None)
            if not btn_data:
                continue
            btn_text = str(getattr(btn, "text", "") or "")
            try:
                data_text = btn_data.decode("utf-8", errors="ignore") if isinstance(btn_data, (bytes, bytearray)) else str(btn_data)
            except Exception:
                data_text = str(btn_data)

            text_l = btn_text.lower()
            data_l = data_text.lower()
            callback_buttons.append((row_idx, btn_idx, btn_data, text_l, data_l))

            if any(k in text_l for k in red_keywords) or any(k in data_l for k in red_keywords):
                red_button_candidates.append((row_idx, btn_idx, btn_data, text_l, data_l))

    if not callback_buttons:
        return

    has_red_text = ("灵石" in text and "红包" in text) or any(k in lower_text for k in ("抢红包", "领取红包"))
    has_game_hint = any(k in lower_text for k in game_keywords)

    # 仅处理明确红包消息；若是游戏提示且没有红包信号，直接忽略
    if not has_red_text and not red_button_candidates:
        return
    if has_game_hint and not has_red_text and not red_button_candidates:
        return

    # 优先红包候选按钮，否则回退第一个可点击按钮（兼容旧脚本）
    target_row_idx, target_btn_idx, button_data, _, _ = (
        red_button_candidates[0] if red_button_candidates else callback_buttons[0]
    )

    log_event(
        logging.INFO,
        "red_packet",
        "检测到红包按钮消息",
        user_id=user_ctx.user_id,
        msg_id=getattr(event, "id", None),
    )

    from telethon.tl import functions as tl_functions
    import re

    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            try:
                await event.click(target_row_idx, target_btn_idx)
            except Exception:
                await event.click(button_data)
            await asyncio.sleep(1)

            response = await client(
                tl_functions.messages.GetBotCallbackAnswerRequest(
                    peer=event.chat_id,
                    msg_id=event.id,
                    data=button_data,
                )
            )
            response_msg = getattr(response, "message", "") or ""

            if "已获得" in response_msg:
                bonus_match = re.search(r"已获得\s*(\d+)\s*灵石", response_msg)
                bonus = bonus_match.group(1) if bonus_match else "未知数量"
                mes = f"🎉 抢到红包{bonus}灵石！"
                log_event(
                    logging.INFO,
                    "red_packet",
                    "领取成功",
                    user_id=user_ctx.user_id,
                    bonus=bonus,
                )
                await send_to_admin(client, mes, user_ctx, global_config)
                return

            if any(flag in response_msg for flag in ("不能重复领取", "来晚了", "领过")):
                mes = "⚠️ 抢到红包，但是没有获取到灵石数量！"
                log_event(
                    logging.INFO,
                    "red_packet",
                    "红包已领取或过期",
                    user_id=user_ctx.user_id,
                    response=response_msg,
                )
                await send_to_admin(client, mes, user_ctx, global_config)
                return

            log_event(
                logging.WARNING,
                "red_packet",
                "红包领取回复未知，准备重试",
                user_id=user_ctx.user_id,
                attempt=attempt + 1,
                response=response_msg[:80],
            )
        except Exception as e:
            log_event(
                logging.WARNING,
                "red_packet",
                "尝试领取红包失败",
                user_id=user_ctx.user_id,
                attempt=attempt + 1,
                error=str(e),
            )

        if attempt < max_attempts - 1:
            await asyncio.sleep(1)

    log_event(
        logging.WARNING,
        "red_packet",
        "多次尝试后未成功领取红包",
        user_id=user_ctx.user_id,
        msg_id=getattr(event, "id", None),
    )


def is_fund_available(user_ctx: UserContext, bet_amount: int = 0) -> bool:
    """检查资金是否充足（与 master 版语义一致：需同时满足余额>0且>=本次下注金额）。"""
    rt = user_ctx.state.runtime
    gambling_fund = rt.get("gambling_fund", 0)
    return gambling_fund > 0 and gambling_fund >= bet_amount


def _is_invalid_callback_message_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "message id is invalid",
        "getbotcallbackanswerrequest",
        "can't do that operation on such message",
        "messageidinvaliderror",
    )
    return any(marker in text for marker in markers)


async def _find_latest_bet_prompt_message(client, event, user_ctx: UserContext):
    """回溯最近可点击的下注提示消息，用于 message id 失效时恢复。"""
    zq_bot = user_ctx.config.groups.get("zq_bot")
    zq_bot_targets = {str(item) for item in _iter_targets(zq_bot)}
    hints = ("[近 40 次结果]", "由近及远", "0 小 1 大")

    try:
        async for msg in client.iter_messages(event.chat_id, limit=20):
            if zq_bot_targets and str(getattr(msg, "sender_id", None)) not in zq_bot_targets:
                continue
            if not getattr(msg, "reply_markup", None):
                continue
            raw = (getattr(msg, "message", None) or getattr(msg, "raw_text", None) or "").strip()
            if any(hint in raw for hint in hints):
                return msg
    except Exception as e:
        log_event(logging.DEBUG, "bet_on", "回溯下注提示消息失败", user_id=user_ctx.user_id, error=str(e))
    return None


async def _click_bet_button_with_recover(client, event, user_ctx: UserContext, button_data):
    """点击下注按钮；若原消息失效，则回溯最新下注提示消息重试。"""
    try:
        await event.click(button_data)
        return
    except Exception as e:
        if not _is_invalid_callback_message_error(e):
            raise

    latest_msg = await _find_latest_bet_prompt_message(client, event, user_ctx)
    if latest_msg is None:
        raise RuntimeError("下注窗口失效且未找到可用的最新下注消息")

    await latest_msg.click(button_data)
    log_event(
        logging.WARNING,
        "bet_on",
        "原下注消息失效，已使用最新消息重试按钮点击",
        user_id=user_ctx.user_id,
        src_msg=getattr(event, "id", None),
        retry_msg=getattr(latest_msg, "id", None),
    )


def _read_timing_config(global_config: dict) -> dict:
    """读取下注时序参数，提供安全兜底。"""
    cfg = global_config.get("betting") if isinstance(global_config.get("betting"), dict) else {}

    def _to_float(name: str, default: float, minimum: float, maximum: float) -> float:
        raw = cfg.get(name, default)
        try:
            val = float(raw)
        except Exception:
            return default
        return max(minimum, min(maximum, val))

    return {
        "prompt_wait_sec": _to_float("prompt_wait_sec", 1.2, 0.0, 5.0),
        "predict_timeout_sec": _to_float("predict_timeout_sec", 8.0, 1.0, 30.0),
        "click_interval_sec": _to_float("click_interval_sec", 0.45, 0.05, 2.0),
        "click_timeout_sec": _to_float("click_timeout_sec", 6.0, 1.0, 20.0),
    }


def calculate_bet_amount(rt: dict) -> int:
    """按 master 逻辑计算本局下注金额。"""
    win_count = rt.get("win_count", 0)
    lose_count = rt.get("lose_count", 0)
    initial_amount = int(rt.get("initial_amount", 500))
    lose_stop = int(rt.get("lose_stop", 13))
    lose_once = float(rt.get("lose_once", 3))
    lose_twice = float(rt.get("lose_twice", 2.1))
    lose_three = float(rt.get("lose_three", 2.1))
    lose_four = float(rt.get("lose_four", 2.05))

    if win_count >= 0 and lose_count == 0:
        return constants.closest_multiple_of_500(initial_amount)

    if (lose_count + 1) > lose_stop:
        return 0

    base_amount = int(rt.get("bet_amount", initial_amount))
    if lose_count == 1:
        target = base_amount * lose_once
    elif lose_count == 2:
        target = base_amount * lose_twice
    elif lose_count == 3:
        target = base_amount * lose_three
    else:
        target = base_amount * lose_four

    # 与 master 一致：补 1% 安全边际
    return constants.closest_multiple_of_500(target + target * 0.01)


def _build_pause_resume_hint(rt: dict) -> str:
    """构建“暂停结束后会做什么”的提示。"""
    next_sequence = int(rt.get("bet_sequence_count", 0)) + 1
    next_amount = int(calculate_bet_amount(rt) or 0)
    if next_amount > 0:
        return f"恢复后动作：继续第 {next_sequence} 手，预计下注 {format_number(next_amount)}"
    return f"恢复后动作：继续第 {next_sequence} 手"


def _evaluate_entry_quality_gate(rt: dict, risk_pause: dict, next_sequence: int) -> dict:
    """
    高倍入场质量门控：
    - 第3手：至少满足最低置信度，避免在弱信号下继续放大
    - 第4手：更严格，且限制标签白名单
    """
    if next_sequence not in (3, 4):
        return {"blocked": False}

    source = str(rt.get("last_predict_source", "unknown")).lower()
    tag = str(rt.get("last_predict_tag", "")).strip().upper()
    confidence = int(rt.get("last_predict_confidence", 0) or 0)
    total = int(risk_pause.get("total", 0))
    wins = int(risk_pause.get("wins", 0))
    win_rate = (wins / total) if total > 0 else 0.0

    reasons = []
    pause_rounds = ENTRY_GUARD_STEP3_PAUSE_ROUNDS
    gate_name = "第3手质量门控"

    if source != "model":
        reasons.append("本局预测未拿到稳定模型结果（超时/异常）")

    if next_sequence == 3:
        if confidence < ENTRY_GUARD_STEP3_MIN_CONF:
            reasons.append(f"置信度 {confidence}% < {ENTRY_GUARD_STEP3_MIN_CONF}%")
    elif next_sequence == 4:
        gate_name = "第4手强风控门控"
        pause_rounds = ENTRY_GUARD_STEP4_PAUSE_ROUNDS
        if confidence < ENTRY_GUARD_STEP4_MIN_CONF:
            reasons.append(f"置信度 {confidence}% < {ENTRY_GUARD_STEP4_MIN_CONF}%")
        if tag not in ENTRY_GUARD_STEP4_ALLOWED_TAGS:
            reasons.append(f"标签 {tag or 'UNKNOWN'} 不在白名单")
        if total >= RISK_WINDOW_BETS and win_rate < 0.45:
            reasons.append(f"最近40笔胜率仅 {wins}/{total}（{win_rate * 100:.1f}%）")

    if reasons:
        return {
            "blocked": True,
            "gate_name": gate_name,
            "pause_rounds": pause_rounds,
            "reason_text": "；".join(reasons),
            "source": source,
            "tag": tag or "UNKNOWN",
            "confidence": confidence,
            "wins": wins,
            "total": total,
            "win_rate": win_rate,
        }
    return {"blocked": False}


async def _apply_entry_gate_pause(
    client,
    user_ctx: UserContext,
    global_config: dict,
    gate: dict,
    next_sequence: int,
) -> None:
    """统一发送高倍入场门控暂停提示。"""
    rt = user_ctx.state.runtime
    pause_rounds = max(1, int(gate.get("pause_rounds", 1)))
    _enter_pause(rt, pause_rounds, gate.get("gate_name", "高倍入场门控"))
    user_ctx.save_state()

    pause_msg = (
        "⛔ 高倍入场暂停（已生效）\n"
        f"触发点：第 {next_sequence} 手下注前\n"
        f"触发类型：{gate.get('gate_name', '高倍入场门控')}\n"
        f"当前信号：标签 {gate.get('tag', 'UNKNOWN')} | 置信度 {gate.get('confidence', 0)}% | 来源 {gate.get('source', 'unknown')}\n"
        f"最近胜率：{gate.get('wins', 0)}/{gate.get('total', 0)}（{gate.get('win_rate', 0.0) * 100:.1f}%）\n"
        f"未通过条件：{gate.get('reason_text', '信号质量不足')}\n"
        f"本次暂停：{pause_rounds} 局\n"
        "暂停期间：保留当前倍投进度，不会重置首注\n"
        f"{_build_pause_resume_hint(rt)}"
    )

    if hasattr(user_ctx, "risk_pause_message") and user_ctx.risk_pause_message:
        await cleanup_message(client, user_ctx.risk_pause_message)
    user_ctx.risk_pause_message = await send_to_admin(client, pause_msg, user_ctx, global_config)
    await _refresh_pause_countdown_notice(
        client,
        user_ctx,
        global_config,
        remaining_rounds=pause_rounds,
    )

def _get_recent_settled_outcomes(state, window: int = RISK_WINDOW_BETS) -> list:
    """提取最近 N 笔已结算结果（赢=1，输=0）。"""
    if window <= 0:
        return []
    outcomes = []
    for entry in reversed(state.bet_sequence_log):
        result = entry.get("result")
        if result == "赢":
            outcomes.append(1)
        elif result == "输":
            outcomes.append(0)
        if len(outcomes) >= window:
            break
    outcomes.reverse()
    return outcomes


def _count_settled_bets(state) -> int:
    """统计已结算押注笔数（赢/输）。"""
    count = 0
    for entry in state.bet_sequence_log:
        result = entry.get("result")
        if result in ("赢", "输"):
            count += 1
    return count


def _fallback_pause_rounds(level: str, wins: int, total: int, lose_count: int, max_pause: int) -> int:
    """模型不可用时的暂停局数兜底。"""
    max_pause = max(1, int(max_pause))
    if total <= 0:
        return min(1, max_pause)

    win_rate = wins / total
    if str(level).startswith("DEEP"):
        if lose_count >= 9:
            base = 2
        elif lose_count >= 6:
            base = 2
        else:
            base = 3
        return max(1, min(max_pause, base))

    # BASE：根据40局胜率分层
    if win_rate <= 0.30:
        base = 4
    elif win_rate <= 0.35:
        base = 3
    else:
        base = 2
    return max(1, min(max_pause, base))


def _parse_pause_rounds_response(raw_text: str, max_pause: int) -> tuple:
    """解析模型返回的暂停建议，返回 (pause_rounds|None, reason)。"""
    if not raw_text:
        return None, ""

    max_pause = max(1, int(max_pause))
    candidates = [raw_text.strip()]
    # 兼容模型返回前后包裹说明文字的情况
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw_text[start:end + 1].strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if not isinstance(data, dict):
                continue
            pause_raw = data.get("pause_rounds", data.get("pause", data.get("rounds")))
            if pause_raw is None:
                continue
            pause_rounds = int(float(str(pause_raw).strip()))
            pause_rounds = max(1, min(max_pause, pause_rounds))
            reason = str(data.get("reason", "")).strip()
            return pause_rounds, reason
        except Exception:
            continue

    return None, ""


async def _suggest_pause_rounds_by_model(
    user_ctx: UserContext,
    risk_eval: dict,
    max_pause: int,
) -> tuple:
    """调用大模型给出暂停局数建议，失败时自动降级到统计兜底。"""
    state = user_ctx.state
    rt = state.runtime
    current_model_id = rt.get("current_model_id")
    wins = int(risk_eval.get("wins", 0))
    total = int(risk_eval.get("total", 0))
    lose_count = int(risk_eval.get("lose_count", 0))
    level = str(risk_eval.get("level", "BASE"))

    fallback_rounds = _fallback_pause_rounds(level, wins, total, lose_count, max_pause)
    fallback_reason = "模型异常，统计兜底"
    if not current_model_id:
        return fallback_rounds, fallback_reason, "fallback"

    recent_tail = risk_eval.get("recent_outcomes", [])[-12:]
    recent_text = "".join(str(x) for x in recent_tail) if recent_tail else "NA"
    prompt = f"""你是一个只负责风险暂停局数的控制器。必须只输出JSON。

当前风控层级：{risk_eval.get('level_label', level)}
最近{total}笔胜率：{wins}/{total}（{risk_eval.get('win_rate', 0.0) * 100:.1f}%）
当前连输：{lose_count}
下一手：第{risk_eval.get('next_sequence', 1)}手
最近12笔结算(赢1输0)：{recent_text}

请给出暂停建议，范围必须在 1 到 {max_pause} 之间。
输出格式：
{{"pause_rounds": 1-{max_pause}之间整数, "reason": "20字内"}}
"""

    messages = [
        {"role": "system", "content": "你是交易风控引擎，只返回JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]

    try:
        result = await asyncio.wait_for(
            user_ctx.get_model_manager().call_model(current_model_id, messages, temperature=0.0, max_tokens=120),
            timeout=RISK_PAUSE_MODEL_TIMEOUT_SEC,
        )
        if not result.get("success"):
            raise RuntimeError(str(result.get("error", "unknown")))

        rounds, reason = _parse_pause_rounds_response(result.get("content", ""), max_pause=max_pause)
        if rounds is None:
            raise ValueError("pause_rounds parse failed")
        reason = reason or "模型建议"
        return rounds, reason, "model"
    except Exception as e:
        log_event(
            logging.WARNING,
            "risk_pause",
            "风控暂停模型建议失败，使用统计兜底",
            user_id=user_ctx.user_id,
            error=str(e),
            level=level,
        )
        return fallback_rounds, fallback_reason, "fallback"


def _get_deep_triggered_milestones(rt: dict) -> list:
    """读取并规范化已触发的深度风控里程碑。"""
    raw = rt.get("risk_deep_triggered_milestones", [])
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        items = []

    normalized = []
    for item in items:
        try:
            normalized.append(int(item))
        except Exception:
            continue
    return sorted(set(normalized))


def _evaluate_auto_risk_pause(state, rt: dict, next_sequence: int) -> dict:
    """
    评估自动风控状态（基础风控 + 深度风控里程碑）。
    基础风控：最近40笔胜率阈值触发（连续命中由外层控制）
    深度风控：连输每达到 3 的倍数档位时触发（每档同一连输周期仅触发一次）
    """
    outcomes = _get_recent_settled_outcomes(state, RISK_WINDOW_BETS)
    total = len(outcomes)
    wins = int(sum(outcomes))
    win_rate = wins / total if total > 0 else 0.0
    lose_count = int(rt.get("lose_count", 0))
    base_window_ready = total >= RISK_WINDOW_BETS
    base_trigger = base_window_ready and wins <= RISK_BASE_TRIGGER_WINS
    recovery_hit = base_window_ready and wins >= RISK_RECOVERY_WINS

    triggered_milestones = _get_deep_triggered_milestones(rt)
    deep_milestone = 0
    deep_level_cap = 0
    lose_stop = max(1, int(rt.get("lose_stop", 13)))
    if lose_count >= RISK_DEEP_TRIGGER_INTERVAL and lose_count < lose_stop:
        current_milestone = (lose_count // RISK_DEEP_TRIGGER_INTERVAL) * RISK_DEEP_TRIGGER_INTERVAL
        if current_milestone > 0 and current_milestone not in triggered_milestones:
            deep_milestone = current_milestone
            if current_milestone == RISK_DEEP_TRIGGER_INTERVAL:
                deep_level_cap = int(RISK_DEEP_FIRST_MAX_PAUSE_ROUNDS)
            else:
                deep_level_cap = int(RISK_DEEP_NEXT_MAX_PAUSE_ROUNDS)

    reasons = []
    if base_trigger:
        reasons.append("最近40笔胜率<=37.5%")
    if deep_milestone > 0:
        reasons.append(f"连输达到{deep_milestone}局档位（每3局触发）")

    return {
        "triggered": bool(base_trigger or deep_milestone > 0),
        "wins": wins,
        "total": total,
        "win_rate": win_rate,
        "next_sequence": next_sequence,
        "lose_count": lose_count,
        "base_window_ready": base_window_ready,
        "base_trigger": base_trigger,
        "recovery_hit": recovery_hit,
        "deep_trigger": deep_milestone > 0,
        "deep_milestone": deep_milestone,
        "deep_level_cap": deep_level_cap,
        "deep_triggered_milestones": triggered_milestones,
        "reasons": reasons,
        "recent_outcomes": outcomes[-20:],
    }


def _apply_auto_risk_pause(rt: dict, pause_rounds: int) -> None:
    """
    执行自动风控暂停。
    说明：stop_count 在下注入口每轮先减1，设为 (暂停局数+1) 才能真正停满指定局数。
    """
    pause_rounds = max(1, int(pause_rounds))
    internal_stop_count = pause_rounds + 1

    rt["stop_count"] = max(int(rt.get("stop_count", 0)), internal_stop_count)
    rt["bet_on"] = False
    rt["bet"] = False
    rt["mode_stop"] = False


def _enter_pause(rt: dict, pause_rounds: int, reason: str) -> int:
    """
    统一暂停入口：写入暂停状态 + 倒计时上下文。
    返回规范化后的暂停局数。
    """
    rounds = max(1, int(pause_rounds))
    _apply_auto_risk_pause(rt, rounds)
    _set_pause_countdown_context(rt, reason, rounds)
    return rounds


def _set_pause_countdown_context(rt: dict, reason: str, pause_rounds: int) -> None:
    """写入统一暂停倒计时上下文（手动暂停不使用该机制）。"""
    rounds = max(1, int(pause_rounds))
    rt["pause_countdown_active"] = True
    rt["pause_countdown_reason"] = str(reason or "自动暂停")
    rt["pause_countdown_total_rounds"] = rounds
    rt["pause_countdown_last_remaining"] = -1


async def _clear_pause_countdown_notice(client, user_ctx: UserContext) -> None:
    """清理暂停倒计时消息与上下文。"""
    rt = user_ctx.state.runtime
    if hasattr(user_ctx, "pause_countdown_message") and user_ctx.pause_countdown_message:
        await cleanup_message(client, user_ctx.pause_countdown_message)
        user_ctx.pause_countdown_message = None
    rt["pause_countdown_active"] = False
    rt["pause_countdown_reason"] = ""
    rt["pause_countdown_total_rounds"] = 0
    rt["pause_countdown_last_remaining"] = -1


async def _refresh_pause_countdown_notice(
    client,
    user_ctx: UserContext,
    global_config: dict,
    remaining_rounds: int = None,
) -> None:
    """刷新式推送暂停倒计时通知。"""
    rt = user_ctx.state.runtime
    if rt.get("manual_pause", False):
        return
    if not rt.get("pause_countdown_active", False):
        return

    total_rounds = int(rt.get("pause_countdown_total_rounds", 0))
    if total_rounds <= 0:
        return

    if remaining_rounds is None:
        remaining_rounds = int(rt.get("stop_count", 0))
    remaining_rounds = max(0, min(total_rounds, int(remaining_rounds)))

    if remaining_rounds <= 0:
        return

    last_remaining = int(rt.get("pause_countdown_last_remaining", -1))
    if (
        last_remaining == remaining_rounds
        and hasattr(user_ctx, "pause_countdown_message")
        and user_ctx.pause_countdown_message
    ):
        return

    reason = str(rt.get("pause_countdown_reason", "自动暂停")).strip() or "自动暂停"
    progress_rounds = max(0, total_rounds - remaining_rounds)
    resume_hint = _build_pause_resume_hint(rt)
    countdown_msg = (
        "⏸️⏸️ 暂停倒计时提醒（自动）⏸️⏸️\n\n"
        f"📌 暂停原因：{reason}\n"
        "🧱 当前状态：暂停中，本局不会下注\n"
        f"🔢 倒计时：{remaining_rounds} 局\n"
        f"📊 暂停进度：{progress_rounds}/{total_rounds}\n"
        f"🔄 {resume_hint}\n"
        "ℹ️ 若恢复时仍不满足风控门槛，会再次自动暂停"
    )

    if hasattr(user_ctx, "pause_countdown_message") and user_ctx.pause_countdown_message:
        await cleanup_message(client, user_ctx.pause_countdown_message)
    user_ctx.pause_countdown_message = await send_to_admin(client, countdown_msg, user_ctx, global_config)
    rt["pause_countdown_last_remaining"] = remaining_rounds


async def _trigger_deep_risk_pause_after_settle(
    client,
    user_ctx: UserContext,
    global_config: dict,
    risk_pause: dict,
    next_sequence: int,
    settled_count: int,
) -> bool:
    """在结算阶段触发深度风控暂停（连输里程碑），命中后立即通知。"""
    rt = user_ctx.state.runtime
    if not risk_pause.get("deep_trigger", False):
        return False

    deep_milestone = int(risk_pause.get("deep_milestone", 0))
    deep_cap = int(risk_pause.get("deep_level_cap", 3))
    if deep_milestone <= 0 or deep_cap <= 0:
        return False

    level_label = f"深度风控（{deep_milestone}连输档）"
    model_eval = {
        **risk_pause,
        "level": f"DEEP_{deep_milestone}",
        "level_label": level_label,
    }
    model_pause_rounds, model_reason, model_source = await _suggest_pause_rounds_by_model(
        user_ctx,
        model_eval,
        max_pause=deep_cap,
    )
    pause_rounds = max(1, min(deep_cap, int(model_pause_rounds)))
    _enter_pause(rt, pause_rounds, f"深度风控暂停（{deep_milestone}连输档）")
    rt["risk_pause_snapshot_count"] = settled_count
    rt["risk_pause_block_hits"] = int(rt.get("risk_pause_block_hits", 0)) + 1
    rt["risk_pause_block_rounds"] = int(rt.get("risk_pause_block_rounds", 0)) + pause_rounds

    deep_triggered = _get_deep_triggered_milestones(rt)
    if deep_milestone not in deep_triggered:
        deep_triggered.append(deep_milestone)
    rt["risk_deep_triggered_milestones"] = sorted(set(int(x) for x in deep_triggered))

    wins = risk_pause.get("wins", 0)
    total = risk_pause.get("total", 0)
    win_rate = risk_pause.get("win_rate", 0.0) * 100
    reason_text = "、".join(risk_pause.get("reasons", [])) or f"连输达到{deep_milestone}档位"
    resume_hint = _build_pause_resume_hint(rt)
    pause_msg = (
        "⛔ 自动风控暂停（已生效）\n"
        f"触发层级：{level_label}\n"
        f"触发原因：{reason_text}\n"
        f"最近{total}笔胜率：{wins}/{total}（{win_rate:.1f}%）\n"
        f"触发点：第 {next_sequence} 手下注前\n"
        f"模型建议：{model_pause_rounds} 局（来源：{model_source}）\n"
        f"本次暂停：{pause_rounds} 局（该层上限 {deep_cap}，不占基础预算）\n"
        f"模型依据：{model_reason}\n"
        "暂停期间：保留当前倍投进度，不会重置首注\n"
        f"{resume_hint}"
    )

    if hasattr(user_ctx, "risk_pause_message") and user_ctx.risk_pause_message:
        await cleanup_message(client, user_ctx.risk_pause_message)
    user_ctx.risk_pause_message = await send_to_admin(client, pause_msg, user_ctx, global_config)
    await _refresh_pause_countdown_notice(
        client,
        user_ctx,
        global_config,
        remaining_rounds=pause_rounds,
    )
    rt["risk_pause_priority_notified"] = True
    user_ctx.save_state()

    log_event(
        logging.INFO,
        "settle",
        "结算阶段触发深度风控暂停",
        user_id=user_ctx.user_id,
        data=(
            f"milestone={deep_milestone}, next_seq={next_sequence}, "
            f"pause_rounds={pause_rounds}, source={model_source}"
        ),
    )
    return True


async def _handle_goal_pause_after_settle(
    client,
    user_ctx: UserContext,
    global_config: dict,
) -> bool:
    """
    统一处理“炸号/盈利达成”触发的暂停。
    仅做结构收敛，不改变原有阈值与重置语义。
    """
    state = user_ctx.state
    rt = state.runtime

    explode_count = int(rt.get("explode_count", 0))
    explode = int(rt.get("explode", 5))
    period_profit = int(rt.get("period_profit", 0))
    profit_target = int(rt.get("profit", 1000000))

    if not (explode_count >= explode or period_profit >= profit_target):
        return False

    if not rt.get("flag", True):
        return False
    rt["flag"] = False

    notify_type = "explode" if explode_count >= explode else "profit"
    log_event(logging.INFO, 'settle', '触发通知', user_id=user_ctx.user_id, data=f'type={notify_type}')

    if notify_type == "profit":
        date_str = datetime.now().strftime("%m月%d日")
        current_round_str = f"{datetime.now().strftime('%Y%m%d')}_{rt.get('current_round', 1)}"
        round_bet_count = sum(
            1 for entry in state.bet_sequence_log
            if str(entry.get("bet_id", "")).startswith(current_round_str)
        )
        win_msg = (
            f"😄📈 {date_str}第 {rt.get('current_round', 1)} 轮 赢了\n"
            f"收益：{period_profit / 10000:.2f} 万\n"
            f"共下注：{round_bet_count} 次"
        )
        await send_message_v2(client, "win", win_msg, user_ctx, global_config)
    else:
        explode_msg = f"**💥 本轮炸了**\n收益：{period_profit / 10000:.2f} 万"
        await send_message_v2(client, "explode", explode_msg, user_ctx, global_config)

    configured_stop_rounds = int(rt.get("stop", 3) if notify_type == "explode" else rt.get("profit_stop", 5))
    pause_reason = "炸号保护暂停" if notify_type == "explode" else "盈利达成暂停"
    _enter_pause(rt, configured_stop_rounds, pause_reason)
    rt["bet_sequence_count"] = 0

    if period_profit >= profit_target:
        rt["current_round"] = int(rt.get("current_round", 1)) + 1
        rt["current_bet_seq"] = 1

    rt["explode_count"] = 0
    rt["period_profit"] = 0
    rt["lose_count"] = 0
    rt["win_count"] = 0
    rt["bet_amount"] = int(rt.get("initial_amount", 500))
    _clear_lose_recovery_tracking(rt)

    resume_hint = _build_pause_resume_hint(rt)
    pause_msg = (
        "⏸️ 目标暂停（已生效）\n"
        f"原因：{'被炸保护' if notify_type == 'explode' else '盈利达成'}\n"
        f"本次暂停：{configured_stop_rounds} 局\n"
        "暂停期间：保留策略状态，等待倒计时结束\n"
        f"{resume_hint}"
    )
    log_event(
        logging.INFO,
        'settle',
        '暂停押注',
        user_id=user_ctx.user_id,
        data=f'type={notify_type}, stop_count={configured_stop_rounds}'
    )
    await send_message_v2(
        client,
        "goal_pause",
        pause_msg,
        user_ctx,
        global_config,
        title=f"菠菜机器人 {user_ctx.config.name} {'炸号' if notify_type == 'explode' else '盈利'}暂停",
        desp=pause_msg,
    )
    await _refresh_pause_countdown_notice(
        client,
        user_ctx,
        global_config,
        remaining_rounds=configured_stop_rounds,
    )
    return True


def count_consecutive(history):
    """统计连续出现次数 - 与master版本一致"""
    result_counts = {"大": {}, "小": {}}
    if not history:
        return result_counts
    
    current_streak = 1
    for i in range(1, len(history)):
        if history[i] == history[i-1]:
            current_streak += 1
        else:
            key = "大" if history[i-1] == 1 else "小"
            result_counts[key][current_streak] = result_counts[key].get(current_streak, 0) + 1
            current_streak = 1
    
    key = "大" if history[-1] == 1 else "小"
    result_counts[key][current_streak] = result_counts[key].get(current_streak, 0) + 1
    
    return result_counts


def count_lose_streaks(bet_sequence_log):
    """统计连输次数 - 与master版本一致"""
    lose_streaks = {}
    current_streak = 0
    
    for entry in bet_sequence_log:
        profit = entry.get("profit", 0)
        if profit < 0:
            current_streak += 1
        else:
            if current_streak > 0:
                lose_streaks[current_streak] = lose_streaks.get(current_streak, 0) + 1
            current_streak = 0
    
    if current_streak > 0:
        lose_streaks[current_streak] = lose_streaks.get(current_streak, 0) + 1
    
    return lose_streaks


def _clear_lose_recovery_tracking(rt: dict) -> None:
    """清理连输回补跟踪状态，避免跨轮次残留导致误发“连输已终止”消息。"""
    rt["lose_notify_pending"] = False
    rt["lose_start_info"] = {}


def _is_valid_lose_range(start_round, start_seq, end_round, end_seq) -> bool:
    """校验连输区间是否有效（起点不晚于终点）。"""
    try:
        sr = int(start_round)
        ss = int(start_seq)
        er = int(end_round)
        es = int(end_seq)
    except Exception:
        return False

    if sr > er:
        return False
    if sr == er and ss > es:
        return False
    return True


def generate_bet_id(user_ctx: UserContext) -> str:
    """生成押注 ID（与 master 逻辑一致：按天重置轮次）。"""
    rt = user_ctx.state.runtime
    current_date = datetime.now().strftime("%Y%m%d")
    if current_date != rt.get("last_reset_date", ""):
        rt["current_round"] = 1
        rt["current_bet_seq"] = 1
        rt["last_reset_date"] = current_date
    return f"{current_date}_{rt.get('current_round', 1)}_{rt.get('current_bet_seq', 1)}"


def format_bet_id(bet_id):
    """将押注 ID 转换为直观格式，如 '3月14日第 1 轮第 12 次'。"""
    try:
        date_str, round_num, seq_num = str(bet_id).split('_')
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        return f"{month}月{day}日第 {round_num} 轮第 {seq_num} 次"
    except Exception:
        return str(bet_id)


def get_settle_position(state, rt):
    """
    获取当前结算对应的轮次与序号。
    优先用当前结算 bet_id，回退到 current_bet_seq - 1。
    """
    settle_round = int(rt.get("current_round", 1))
    settle_seq = max(1, int(rt.get("current_bet_seq", 1)) - 1)
    if state.bet_sequence_log:
        last_bet_id = str(state.bet_sequence_log[-1].get("bet_id", ""))
        import re
        match = re.match(r"^\d{8}_(\d+)_(\d+)$", last_bet_id)
        if match:
            settle_round = int(match.group(1))
            settle_seq = int(match.group(2))
    return settle_round, settle_seq


def _format_recent_binary(history: list, window: int) -> str:
    """
    格式化最近 N 局结果为二进制字符串
    与 master 版本 _format_recent_binary 一致
    """
    if len(history) < window:
        window = len(history)
    if window <= 0:
        return ""
    recent = history[-window:]
    return "".join(str(x) for x in recent)


def _get_current_streak(history: list):
    """返回当前连串长度与方向（与 master 一致）。"""
    if not history:
        return 0, "大"
    tail = history[-1]
    streak = 1
    for value in reversed(history[:-1]):
        if value == tail:
            streak += 1
        else:
            break
    return streak, ("大" if tail == 1 else "小")


def _compact_reason_text(reason: str, max_len: int = 96) -> str:
    """压缩风控原因，避免在通知里输出超长分析（与 master 一致）。"""
    if not reason:
        return "策略风控触发"
    first_line = str(reason).splitlines()[0].strip()
    return first_line if len(first_line) <= max_len else first_line[: max_len - 1] + "…"


def generate_mobile_bet_report(
    history: list,
    direction: str,
    amount: int,
    sequence_count: int,
    bet_id: str = ""
) -> str:
    """生成简短押注执行报告（与 master 一致）。"""
    streak_len, streak_side = _get_current_streak(history)
    return (
        "🎯 押注执行\n"
        f"方向: {direction}\n"
        f"金额: {format_number(amount)}\n"
        f"连押: 第 {sequence_count} 次\n"
        f"当前连{streak_side}: {streak_len}"
    )


def generate_mobile_pause_report(
    history: list,
    pause_reason: str,
    confidence: float = None,
    entropy: float = None
) -> str:
    """生成简短风控暂停报告（与 master 一致）。"""
    streak_len, streak_side = _get_current_streak(history)
    reason_text = _compact_reason_text(pause_reason)
    w5 = _format_recent_binary(history, 5)
    w10 = _format_recent_binary(history, 10)
    w40 = _format_recent_binary(history, 40)

    lines = [
        "⛔ 风控暂停",
        f"原因: {reason_text}",
    ]
    if confidence is not None:
        lines.append(f"置信度: {confidence}%")
    if entropy is not None:
        lines.append(f"熵值: {entropy:.2f}")
    lines.extend(
        [
            f"近5局: {w5}",
            f"近10局: {w10}",
            f"近40局: {w40}",
            f"当前连{streak_side}: {streak_len}",
            "动作: 暂停下注，继续观察",
        ]
    )
    return "\n".join(lines)


async def process_settle(client, event, user_ctx: UserContext, global_config: dict):
    """处理押注结算 - 与master版本zq_settle完全一致，包括连输告警、回补播报、资金安全等"""
    state = user_ctx.state
    rt = state.runtime
    
    text = event.message.message
    
    try:
        import re
        match = re.search(r"已结算: 结果为 (\d+) (大|小)", text)
        if not match:
            log_event(logging.DEBUG, 'settle', '未匹配到结算消息', user_id=user_ctx.user_id, data='action=跳过')
            return

        settle_msg_id = int(getattr(event, "id", 0) or 0)
        last_settle_msg_id = int(rt.get("last_settle_message_id", 0) or 0)
        if settle_msg_id > 0 and settle_msg_id == last_settle_msg_id:
            log_event(logging.INFO, 'settle', '重复结算消息，已跳过', user_id=user_ctx.user_id, data=f'msg_id={settle_msg_id}')
            return
        if settle_msg_id > 0:
            rt["last_settle_message_id"] = settle_msg_id
        
        result_num = int(match.group(1))
        result_type = match.group(2)
        is_big = (result_type == "大")
        result = 1 if is_big else 0

        # 账户余额刷新前置：确保本轮结算/告警/仪表盘使用最新余额，
        # 避免消息里出现“上一轮余额”的体感延迟。
        try:
            balance = await fetch_balance(user_ctx)
            rt["account_balance"] = balance
            rt["balance_status"] = "success"
        except Exception as e:
            log_event(
                logging.WARNING,
                'settle',
                '获取账户余额失败，使用默认值',
                user_id=user_ctx.user_id,
                data=str(e),
            )
            rt["balance_status"] = "network_error"

        if rt.get("open_ydx", False):
            monitor_targets = _iter_targets(user_ctx.config.groups.get("monitor", []))
            for monitor_target in monitor_targets:
                try:
                    await client.send_message(monitor_target, "/ydx")
                except Exception as e:
                    log_event(
                        logging.WARNING,
                        'settle',
                        '发送/ydx失败',
                        user_id=user_ctx.user_id,
                        data=f'target={monitor_target}, error={str(e)}'
                    )
        
        # 更新历史记录
        state.history.append(result)
        state.history = state.history[-2000:]
        
        log_event(logging.INFO, 'settle', '更新历史记录', 
                  user_id=user_ctx.user_id, data=f'result={result}, history_len={len(state.history)}')
        
        # 实时监控：每10局计算准确率
        if len(state.history) >= 10 and len(state.history) % 10 == 0:
            recent_acc = sum(1 for h, p in zip(state.history[-10:], state.predictions[-10:]) if h == p) / 10 * 100
            log_event(logging.INFO, 'model_monitor', '最近10局准确率', 
                      user_id=user_ctx.user_id, data=f'accuracy={recent_acc:.2f}%')
        
        result_text = None
        direction = None
        profit = 0
        result_amount = 0
        lose_end_payload = None
        
        async def _apply_settle_fund_safety_guard() -> None:
            """资金安全闸门（结算后执行，避免未结算订单被提前清空）。"""
            if not is_fund_available(user_ctx):
                if _sync_fund_from_account_when_insufficient(rt, 1):
                    log_event(
                        logging.INFO,
                        'settle',
                        '资金耗尽前触发资金同步',
                        user_id=user_ctx.user_id,
                        data=f"fund={rt.get('gambling_fund', 0)}, account={rt.get('account_balance', 0)}",
                    )
                    user_ctx.save_state()

                if not is_fund_available(user_ctx):
                    if hasattr(user_ctx, 'dashboard_message') and user_ctx.dashboard_message:
                        await cleanup_message(client, user_ctx.dashboard_message)
                    display_fund = max(0, rt.get("gambling_fund", 0))
                    mes = f"**菠菜资金耗尽，已暂停押注**\n当前剩余：{display_fund / 10000:.2f} 万\n请使用 `gf [金额]` 恢复"
                    log_event(logging.WARNING, 'settle', '资金耗尽暂停',
                              user_id=user_ctx.user_id, data=f'fund={rt.get("gambling_fund", 0)}')
                    if not rt.get("fund_pause_notified", False):
                        await send_message_v2(
                            client,
                            "fund_pause",
                            mes,
                            user_ctx,
                            global_config,
                            title=f"菠菜机器人 {user_ctx.config.name} 资金风控暂停",
                            desp=mes,
                        )
                        rt["fund_pause_notified"] = True
                    rt["bet"] = False
                    rt["bet_on"] = False
                    rt["mode_stop"] = True
                else:
                    rt["fund_pause_notified"] = False
                return

            next_bet_amount = calculate_bet_amount(rt)
            if next_bet_amount > 0 and not is_fund_available(user_ctx, next_bet_amount):
                if _sync_fund_from_account_when_insufficient(rt, next_bet_amount):
                    log_event(
                        logging.INFO,
                        'settle',
                        '资金不足前触发资金同步',
                        user_id=user_ctx.user_id,
                        data=(
                            f"need={next_bet_amount}, fund={rt.get('gambling_fund', 0)}, "
                            f"account={rt.get('account_balance', 0)}"
                        ),
                    )
                    user_ctx.save_state()

                if not is_fund_available(user_ctx, next_bet_amount):
                    if not rt.get("fund_pause_notified", False):
                        display_fund = max(0, rt.get("gambling_fund", 0))
                        mes = (
                            f"**菠菜资金不足，已暂停押注**\n"
                            f"当前剩余：{display_fund / 10000:.2f} 万\n"
                            "请使用 `gf [金额]` 恢复"
                        )
                        await send_message_v2(
                            client,
                            "fund_pause",
                            mes,
                            user_ctx,
                            global_config,
                            title=f"菠菜机器人 {user_ctx.config.name} 资金风控暂停",
                            desp=mes,
                        )
                        rt["fund_pause_notified"] = True
                    rt["bet"] = False
                    rt["bet_on"] = False
                    rt["mode_stop"] = True
                else:
                    rt["fund_pause_notified"] = False
            else:
                rt["fund_pause_notified"] = False

        if rt.get("bet", False):
                try:
                    if state.bet_sequence_log and state.bet_sequence_log[-1].get("result") in ("赢", "输"):
                        # 异常兜底：如果最后一笔已结算但 bet 标记未清理，防止重复发送“押注结果”。
                        rt["bet"] = False
                        user_ctx.save_state()
                        log_event(logging.WARNING, 'settle', '检测到已结算下注，跳过重复结算', user_id=user_ctx.user_id)
                        return

                    prediction = int(rt.get("bet_type", -1))
                    win = (is_big and prediction == 1) or (not is_big and prediction == 0)
                    bet_amount = int(rt.get("bet_amount", 500))
                    profit = int(bet_amount * 0.99) if win else -bet_amount
                    settle_round, settle_seq = get_settle_position(state, rt)
                    
                    # 记录连输状态用于回补播报
                    old_lose_count = rt.get("lose_count", 0)
                    
                    direction = "大" if prediction == 1 else "小"
                    result_text = "赢" if win else "输"
                    # 一笔下注只允许被结算一次；后续重复结算消息不再重复记账。
                    rt["bet"] = False
                    state.bet_type_history.append(prediction)
                    rt["gambling_fund"] = rt.get("gambling_fund", 0) + profit
                    rt["earnings"] = rt.get("earnings", 0) + profit
                    rt["period_profit"] = rt.get("period_profit", 0) + profit
                    rt["win_total"] = rt.get("win_total", 0) + (1 if win else 0)
                    rt["win_count"] = rt.get("win_count", 0) + 1 if win else 0
                    rt["lose_count"] = rt.get("lose_count", 0) + 1 if not win else 0
                    rt["status"] = 1 if win else 0
                    if win:
                        # 结束本轮连输后，重置深度风控里程碑触发记录
                        rt["risk_deep_triggered_milestones"] = []
                        rt["risk_pause_level1_hit"] = False
                    
                    # 连输逻辑处理
                    if not win:
                        # 如果连输刚开始（第1次），记录起始信息
                        if rt.get("lose_count", 0) == 1:
                            # 新一轮连输起点，清理旧里程碑，防止深度风控误判为“已触发”
                            rt["risk_deep_triggered_milestones"] = []
                            _clear_lose_recovery_tracking(rt)
                            rt["lose_start_info"] = {
                                "round": settle_round,
                                "seq": settle_seq,
                                "fund": rt.get("gambling_fund", 0) + bet_amount
                            }
                        
                        # 达到告警阈值，标记为待发送状态
                        warning_lose_count = rt.get("warning_lose_count", 3)
                        if rt.get("lose_count", 0) >= warning_lose_count:
                            rt["lose_notify_pending"] = True
                            log_event(logging.INFO, 'settle', '达到连输告警阈值', 
                                      user_id=user_ctx.user_id, data=f'lose_count={rt.get("lose_count", 0)}')
                            
                            # --- 连输实时告警逻辑 (Real-time Lose Streak Warning) ---
                            try:
                                total_losses = bet_amount
                                if rt.get("lose_count", 0) > 1 and state.bet_sequence_log:
                                    start_idx = max(0, len(state.bet_sequence_log) - rt.get("lose_count", 0) + 1)
                                    for entry in state.bet_sequence_log[start_idx:]:
                                        entry_profit = entry.get('profit')
                                        if entry_profit is not None and isinstance(entry_profit, (int, float)) and entry_profit < 0:
                                            total_losses += abs(entry_profit)

                                date_str = datetime.now().strftime("%m月%d日")
                                bet_dir_str = "大" if prediction == 1 else "小"
                                preset_name = rt.get("current_preset_name", "none")
                                lose_count = int(rt.get("lose_count", 0))
                                warn_msg = (
                                    f"⚠️⚠️  {lose_count} 连输告警 ⚠️⚠️\n\n"
                                    f"🔢 {date_str} 第 {settle_round} 轮第 {settle_seq} 次：\n"
                                    f"📋 预设名称：{preset_name}\n"
                                    f"😀 连续押注：{rt.get('bet_sequence_count', 0)} 次\n"
                                    f"⚡️ 押注方向：{bet_dir_str}\n"
                                    f"💵 押注本金：{format_number(bet_amount)}\n"
                                    f"💰 累计损失：{format_number(total_losses)}\n"
                                    f"💰 账户余额：{rt.get('account_balance', 0) / 10000:.2f} 万\n"
                                    f"💰 菠菜余额：{rt.get('gambling_fund', 0) / 10000:.2f} 万"
                                )

                                log_event(
                                    logging.WARNING,
                                    'settle',
                                    '触发连输实时告警',
                                    user_id=user_ctx.user_id,
                                    data=f'lose_count={rt.get("lose_count", 0)}, total_loss={total_losses}'
                                )

                                # 刷新式提示：管理员窗口仅保留最后一条连输告警消息。
                                if hasattr(user_ctx, "lose_streak_message") and user_ctx.lose_streak_message:
                                    await cleanup_message(client, user_ctx.lose_streak_message)

                                user_ctx.lose_streak_message = await send_message_v2(
                                    client,
                                    "lose_streak",
                                    warn_msg,
                                    user_ctx,
                                    global_config,
                                    title=f"菠菜机器人 {user_ctx.config.name} 连输告警",
                                    desp=warn_msg
                                )
                            except Exception as e:
                                log_event(logging.ERROR, 'settle', '连输实时告警构建失败', user_id=user_ctx.user_id, data=str(e))
                                fallback_msg = (
                                    f"⚠️ 连输告警(数据异常) ⚠️\n"
                                    f"连输：{rt.get('lose_count', 0)} 次\n"
                                    f"错误：{str(e)[:50]}"
                                )
                                await send_message_v2(client, "lose_streak", fallback_msg, user_ctx, global_config)
                    
                    # 连输终止处理（赢了）
                    if win and rt.get("lose_notify_pending", False):
                        try:
                            warning_lose_count = int(rt.get("warning_lose_count", 3))
                            lose_start_info = rt.get("lose_start_info", {})
                            start_round = lose_start_info.get("round", "?")
                            start_seq = lose_start_info.get("seq", "?")
                            end_round = settle_round
                            end_seq = settle_seq
                            total_profit = rt.get("gambling_fund", 0) - lose_start_info.get("fund", rt.get("gambling_fund", 0))

                            if (
                                int(old_lose_count) >= warning_lose_count
                                and _is_valid_lose_range(start_round, start_seq, end_round, end_seq)
                            ):
                                continuous_count = max(int(rt.get("bet_sequence_count", 0)), old_lose_count + 1)
                                lose_end_payload = {
                                    "start_round": start_round,
                                    "start_seq": start_seq,
                                    "end_round": end_round,
                                    "end_seq": end_seq,
                                    "lose_count": old_lose_count,
                                    "continuous_count": continuous_count,
                                    "total_profit": total_profit,
                                }
                            else:
                                log_event(
                                    logging.WARNING,
                                    'settle',
                                    '跳过异常连输终止通知',
                                    user_id=user_ctx.user_id,
                                    data=(
                                        f"old_lose_count={old_lose_count}, warning={warning_lose_count}, "
                                        f"start={start_round}-{start_seq}, end={end_round}-{end_seq}"
                                    ),
                                )
                        except Exception as e:
                            log_event(logging.ERROR, 'settle', '连输终止通知异常', 
                                      user_id=user_ctx.user_id, data=str(e))
                        
                        # 重置状态
                        _clear_lose_recovery_tracking(rt)
                    elif win:
                        # 防御式清理：赢单但不存在有效连输链，清理可能遗留的待回补状态。
                        _clear_lose_recovery_tracking(rt)
                    
                    log_event(logging.INFO, 'settle', '结算结果', 
                              user_id=user_ctx.user_id, data=f'result={result_text}, profit={profit}, fund={rt.get("gambling_fund", 0)}')
                    
                    user_ctx.save_state()
                    
                    # 更新押注日志（存储在 state 中，不是 rt 中）
                    if state.bet_sequence_log:
                        state.bet_sequence_log[-1]["result"] = result_text
                        state.bet_sequence_log[-1]["profit"] = profit
                    
                    result_amount = format_number(int(bet_amount * 0.99) if win else bet_amount)
                    last_bet_id = state.bet_sequence_log[-1].get("bet_id", "") if state.bet_sequence_log else ""
                    bet_id = format_bet_id(last_bet_id) if last_bet_id else f"{datetime.now().strftime('%m月%d日')}第 {rt.get('current_round', 1)} 轮第 {rt.get('current_bet_seq', 1)} 次"
                    
                    mes = f"🔢 **{bet_id}押注结果：**\n"
                    mes += f"😀 连续押注：{rt.get('bet_sequence_count', 0)} 次\n"
                    mes += f"⚡ 押注方向：{direction}\n"
                    mes += f"💵 押注本金：{format_number(bet_amount)}\n"
                    mes += f"📉 输赢结果：{result_text} {result_amount}\n"
                    mes += f"🎲 开奖结果：{result_type}\n"
                    mes += f"🤖 预测依据：{rt.get('last_predict_info', 'N/A')}"
                    
                    log_event(logging.INFO, 'settle', '发送结算通知', 
                              user_id=user_ctx.user_id, data=f'bet_id={bet_id}')
                    await send_to_admin(client, mes, user_ctx, global_config)

                    # 深度风控在结算阶段即时触发：每3连输命中后，立即评估并下发暂停通知。
                    if not win:
                        try:
                            next_sequence = int(rt.get("bet_sequence_count", 0)) + 1
                            settled_count = _count_settled_bets(state)
                            risk_pause_eval = _evaluate_auto_risk_pause(state, rt, next_sequence)
                            if (
                                int(rt.get("lose_count", 0)) % int(RISK_DEEP_TRIGGER_INTERVAL) == 0
                                and int(rt.get("lose_count", 0)) < int(rt.get("lose_stop", 13))
                                and not risk_pause_eval.get("deep_trigger", False)
                            ):
                                log_event(
                                    logging.INFO,
                                    'settle',
                                    '深度风控本应触发但被跳过',
                                    user_id=user_ctx.user_id,
                                    data=(
                                        f"lose_count={rt.get('lose_count', 0)}, "
                                        f"lose_stop={rt.get('lose_stop', 13)}, "
                                        f"triggered={rt.get('risk_deep_triggered_milestones', [])}"
                                    ),
                                )
                            await _trigger_deep_risk_pause_after_settle(
                                client,
                                user_ctx,
                                global_config,
                                risk_pause_eval,
                                next_sequence,
                                settled_count,
                            )
                        except Exception as risk_e:
                            log_event(
                                logging.WARNING,
                                'settle',
                                '结算阶段触发深度风控失败',
                                user_id=user_ctx.user_id,
                                data=str(risk_e),
                            )
                    
                    if win or rt.get("lose_count", 0) >= rt.get("lose_stop", 13):
                        rt["bet_sequence_count"] = 0
                        rt["bet_amount"] = int(rt.get("initial_amount", 500))
                        
                except Exception as e:
                    log_event(logging.ERROR, 'settle', '结算失败', 
                              user_id=user_ctx.user_id, data=str(e))
                    await send_to_admin(client, f"结算出错: {e}", user_ctx, global_config)

        # 先结算，再做资金闸门，避免出现“账户余额已变动，但菠菜资金未记账”的时序问题。
        await _apply_settle_fund_safety_guard()
        
        # 每5局保存一次状态
        if len(state.history) % 5 == 0:
            user_ctx.save_state()
            log_event(logging.DEBUG, 'settle', '定期保存状态', 
                      user_id=user_ctx.user_id, data=f'history_len={len(state.history)}')
        
        # 炸和盈利触发统一暂停流程（消息与暂停入口统一）
        await _handle_goal_pause_after_settle(client, user_ctx, global_config)
        
        # 历史记录统计通知
        if hasattr(user_ctx, 'dashboard_message') and user_ctx.dashboard_message:
            await cleanup_message(client, user_ctx.dashboard_message)
        
        current_total = int(rt.get("total", 0))
        last_stats_total = int(rt.get("stats_last_report_total", 0))
        if (
            len(state.history) > 5
            and current_total > 0
            and current_total % AUTO_STATS_INTERVAL_ROUNDS == 0
            and current_total != last_stats_total
        ):
            windows = [1000, 500, 200, 100]
            stats = {"连大": [], "连小": [], "连输": []}
            all_ns = set()
            
            for window in windows:
                history_window = state.history[-window:]
                bet_types = state.bet_type_history[-len(history_window):] if len(state.bet_type_history) > 0 else []
                result_counts = count_consecutive(history_window)
                bet_sequence_log = state.bet_sequence_log[-window:]
                lose_streaks = count_lose_streaks(bet_sequence_log)
                
                stats["连大"].append(result_counts["大"])
                stats["连小"].append(result_counts["小"])
                stats["连输"].append(lose_streaks)
                
                all_ns.update(result_counts["大"].keys())
                all_ns.update(result_counts["小"].keys())
                all_ns.update(lose_streaks.keys())
            
            mes = "```"
            for category in ["连大", "连小", "连输"]:
                mes += "最近局数“连大、连小、连输”统计\n\n"
                mes += f"{category}\n"
                mes += "================================\n"
                mes += "类别 | 1000|  500  |200 | 100|\n"
                mes += "--------------------------------\n"
                sorted_ns = sorted(all_ns, reverse=True)
                for n in sorted_ns:
                    if any(n in stats[category][i] for i in range(len(windows))):
                        mes += f" {str(n).center(2)}  |"
                        for i in range(len(windows)):
                            count = stats[category][i].get(n, 0)
                            value = str(count) if count > 0 else "-"
                            mes += f" {value.center(3)} |"
                        mes += "\n"
                mes += "\n"
            mes += "```"
            
            log_event(
                logging.INFO,
                'settle',
                '发送历史记录统计通知',
                user_id=user_ctx.user_id,
                data=f'interval={AUTO_STATS_INTERVAL_ROUNDS}, ttl={AUTO_STATS_DELETE_DELAY_SECONDS}, total={current_total}'
            )
            stats_message = await send_to_admin(client, mes, user_ctx, global_config)
            user_ctx.stats_message = stats_message
            rt["stats_last_report_total"] = current_total
            if stats_message:
                asyncio.create_task(
                    delete_later(
                        client,
                        stats_message.chat_id,
                        stats_message.id,
                        AUTO_STATS_DELETE_DELAY_SECONDS
                    )
                )
        
        # 每 100 局输出一次风控暂停阶段总结，并同步到重点通道（IYUU/TG Bot）。
        current_total = int(rt.get("total", 0))
        last_report_total = int(rt.get("risk_pause_last_100_report_total", 0))
        if current_total > 0 and current_total % 100 == 0 and current_total != last_report_total:
            block_start = max(1, current_total - 99)
            block_end = current_total
            block_hits = int(rt.get("risk_pause_block_hits", 0))
            block_rounds = int(rt.get("risk_pause_block_rounds", 0))
            win_total = int(rt.get("win_total", 0))
            overall_wr = (win_total / current_total * 100) if current_total > 0 else 0.0

            summary_msg = (
                "📌 风控暂停阶段总结（每100局）\n"
                f"🔢 区间：第 {block_start} ~ {block_end} 局\n"
                f"⛔ 风控暂停触发次数：{block_hits}\n"
                f"⏸ 累计暂停局数：{block_rounds}\n"
                f"🏆 当前总胜率：{overall_wr:.2f}%（{win_total}/{current_total}）\n"
                f"💰 总盈利：{format_number(rt.get('earnings', 0))}\n"
                f"💰 账户余额：{rt.get('account_balance', 0) / 10000:.2f} 万\n"
                f"💰 菠菜资金：{rt.get('gambling_fund', 0) / 10000:.2f} 万"
            )

            await send_message_v2(
                client,
                "risk_summary",
                summary_msg,
                user_ctx,
                global_config,
                title=f"菠菜机器人 {user_ctx.config.name} 风控暂停100局总结",
                desp=summary_msg,
            )
            log_event(
                logging.INFO,
                'settle',
                '发送风控暂停100局总结',
                user_id=user_ctx.user_id,
                data=f'block={block_start}-{block_end}, hits={block_hits}, pause_rounds={block_rounds}'
            )
            rt["risk_pause_last_100_report_total"] = current_total
            rt["risk_pause_block_hits"] = 0
            rt["risk_pause_block_rounds"] = 0

        # 连输终止播报延后到结算数据写入后，避免与结算通知时序错位
        if lose_end_payload:
            date_str = datetime.now().strftime("%m月%d日")
            start_round = lose_end_payload.get("start_round", "?")
            start_seq = lose_end_payload.get("start_seq", "?")
            end_round = lose_end_payload.get("end_round", "?")
            end_seq = lose_end_payload.get("end_seq", "?")
            lose_count = int(lose_end_payload.get("lose_count", 0))
            if str(start_round) == str(end_round):
                range_text = f"{date_str} 第 {start_round} 轮第 {start_seq} 次 至 第 {end_seq} 次"
            else:
                range_text = f"{date_str} 第 {start_round} 轮第 {start_seq} 次 至 第 {end_round} 轮第 {end_seq} 次"

            rec_msg = (
                f"✅✅  {lose_count} 连输已终止！✅✅\n\n"
                f"🔢 {range_text}\n"
                f"📋 预设名称：{rt.get('current_preset_name', 'none')}\n"
                f"😀 连续押注：{lose_end_payload.get('continuous_count', lose_count + 1)} 次\n"
                f"⚠️本局连输： {lose_count} 次\n"
                f"💰 本局盈利： {format_number(lose_end_payload.get('total_profit', 0))}\n"
                f"💰 账户余额：{rt.get('account_balance', 0) / 10000:.2f} 万\n"
                f"💰 菠菜资金剩余：{rt.get('gambling_fund', 0) / 10000:.2f} 万"
            )
            if hasattr(user_ctx, "lose_streak_message") and user_ctx.lose_streak_message:
                await cleanup_message(client, user_ctx.lose_streak_message)
                user_ctx.lose_streak_message = None
            await send_message_v2(client, "lose_end", rec_msg, user_ctx, global_config)
            log_event(
                logging.INFO,
                'settle',
                '触发连输终止通知',
                user_id=user_ctx.user_id,
                data=(
                    f"lose_count={lose_end_payload.get('lose_count', 0)}, "
                    f"start={start_round}-{start_seq}, end={end_round}-{end_seq}, "
                    f"total_profit={lose_end_payload.get('total_profit', 0)}"
                ),
            )
        
        # 发送仪表盘
        dashboard = format_dashboard(user_ctx)
        log_event(logging.INFO, 'settle', '发送仪表盘', user_id=user_ctx.user_id)
        user_ctx.dashboard_message = await send_to_admin(client, dashboard, user_ctx, global_config)
        
        # 保存状态
        user_ctx.save_state()
        
    except Exception as e:
        log_event(logging.ERROR, 'settle', '结算处理失败', 
                  user_id=user_ctx.user_id, data=str(e))


# 用户命令处理
async def delete_later(client, chat_id, message_id, delay=10):
    """延迟指定秒数后删除消息。"""
    await asyncio.sleep(delay)
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception:
        pass


async def handle_model_command_multiuser(event, args, user_ctx: UserContext, global_config: dict):
    """处理 model 命令 - 与master版本handle_model_command一致"""
    rt = user_ctx.state.runtime
    sub_cmd = args[0] if args else "list"
    
    # 兼容 "model id list" 和 "model id XX"
    if sub_cmd == "id":
        if len(args) < 2:
            sub_cmd = "list"
        elif args[1] == "list":
            sub_cmd = "list"
        else:
            sub_cmd = "select"
            args = ["select", args[1]]

    if sub_cmd == "list":
        models = user_ctx.config.ai.get("models", {})
        msg = "**可用模型列表**\n"
        idx = 1
        current_model_id = rt.get("current_model_id", "")
        
        for k, m in models.items():
            if m.get("enabled", True):
                status = "✅"
                current = "👈 当前" if m.get('model_id') == current_model_id else ""
                msg += f"{idx}. `{m.get('model_id', 'unknown')}` {status} {current}\n"
                idx += 1
        await event.reply(msg)
        
    elif sub_cmd in ["select", "use", "switch"]:
        if len(args) < 2:
            await event.reply("请指定模型ID或编号，例如: `model select 1` 或 `model select qwen3-coder-plus`")
            return
            
        target_id = args[1]
        models = user_ctx.config.ai.get("models", {})
        
        # 支持数字编号选择
        if target_id.isdigit():
            idx = int(target_id)
            enabled_models = [m for m in models.values() if m.get("enabled", True)]
            if 1 <= idx <= len(enabled_models):
                target_id = enabled_models[idx-1].get('model_id', '')
            else:
                await event.reply(f"❌ 编号 {idx} 无效")
                return
        
        # 验证模型是否存在
        model_exists = any(m.get('model_id') == target_id for m in models.values() if m.get("enabled"))
        if not model_exists:
            await event.reply(f"❌ 模型 `{target_id}` 不存在或未启用")
            return
            
        await event.reply(f"🔄 正在切换模型 `{target_id}`...")
        
        # 切换模型
        rt["current_model_id"] = target_id
        user_ctx.save_state()
        
        success_msg = (
            f"✅ **模型切换成功**\n"
            f"🤖 **当前模型**: `{target_id}`\n"
            f"🔗 **连接状态**: 🟢 正常\n"
            f"🧠 **算法模式**: V10 (已激活)"
        )
        await event.reply(success_msg)
        log_event(logging.INFO, 'model', '切换模型', user_id=user_ctx.user_id, model=target_id)
            
    elif sub_cmd == "reload":
        await event.reply("🔄 重新加载模型配置...")
        try:
            user_ctx.reload_user_config()
            model_mgr = user_ctx.get_model_manager()
            model_mgr.load_models()
            models = model_mgr.list_models()
            enabled_count = sum(
                1
                for provider_models in models.values()
                for model in provider_models
                if model.get("enabled", True)
            )
            log_event(logging.INFO, 'model', '重新加载模型', user_id=user_ctx.user_id, enabled=enabled_count)
            await event.reply(f"✅ 模型配置已重新加载（可用模型：{enabled_count}）")
        except Exception as e:
            log_event(logging.ERROR, 'model', '重载模型配置失败', user_id=user_ctx.user_id, error=str(e))
            await event.reply(f"❌ 模型配置重载失败：{str(e)[:120]}")
    else:
        await event.reply("未知命令。用法:\n`model list`\n`model select <id>`\n`model reload`")


async def handle_apikey_command_multiuser(event, args, user_ctx: UserContext):
    """处理 apikey 命令：show/set/add/del/test。"""
    rt = user_ctx.state.runtime
    sub_cmd = (args[0].lower() if args else "show")
    ai_cfg = user_ctx.config.ai if isinstance(user_ctx.config.ai, dict) else {}
    keys = _normalize_ai_keys(ai_cfg)

    if sub_cmd in ("show", "list", "ls"):
        if not keys:
            await event.reply(
                "当前未配置任何 AI key。\n"
                "请执行：`apikey set <新key>`"
            )
            return
        lines = ["🔐 当前账号 AI key 列表（已脱敏）"]
        for idx, key in enumerate(keys, 1):
            lines.append(f"{idx}. `{_mask_api_key(key)}`")
        lines.append("\n用法：`apikey set <key>` / `apikey add <key>` / `apikey del <序号>` / `apikey test`")
        await event.reply("\n".join(lines))
        return

    if sub_cmd in ("set", "add"):
        if len(args) < 2:
            await event.reply(f"用法：`apikey {sub_cmd} <新key>`")
            return

        new_key = str(args[1]).strip()
        if not new_key:
            await event.reply("❌ key 不能为空")
            return

        if sub_cmd == "set":
            updated_keys = [new_key]
        else:
            updated_keys = list(keys)
            if new_key in updated_keys:
                await event.reply("⚠️ 该 key 已存在，无需重复添加")
                return
            updated_keys.append(new_key)

        new_ai = dict(ai_cfg)
        new_ai["api_keys"] = updated_keys
        new_ai.pop("api_key", None)
        try:
            config_path = user_ctx.update_ai_config(new_ai)
            _clear_ai_key_issue(rt)
            user_ctx.save_state()
            model_mgr = user_ctx.get_model_manager()
            model_mgr.load_models()
            await event.reply(
                f"✅ AI key 已更新并写入配置\n"
                f"文件：`{os.path.basename(config_path)}`\n"
                f"当前 key 数量：{len(updated_keys)}"
            )
        except Exception as e:
            log_event(logging.ERROR, 'apikey', '写入 key 失败', user_id=user_ctx.user_id, error=str(e))
            await event.reply(f"❌ 更新失败：{str(e)[:160]}")
        return

    if sub_cmd in ("del", "rm", "remove"):
        if len(args) < 2:
            await event.reply("用法：`apikey del <序号>`")
            return
        try:
            idx = int(str(args[1]).strip())
        except ValueError:
            await event.reply("❌ 序号必须是整数")
            return

        if idx < 1 or idx > len(keys):
            await event.reply(f"❌ 序号超出范围，当前 key 数量：{len(keys)}")
            return

        updated_keys = list(keys)
        updated_keys.pop(idx - 1)
        new_ai = dict(ai_cfg)
        new_ai["api_keys"] = updated_keys
        new_ai.pop("api_key", None)
        try:
            config_path = user_ctx.update_ai_config(new_ai)
            if not updated_keys:
                _mark_ai_key_issue(rt, "管理员删除了全部 key")
            user_ctx.save_state()
            await event.reply(
                f"✅ 已删除第 {idx} 个 key 并写入配置\n"
                f"文件：`{os.path.basename(config_path)}`\n"
                f"剩余 key 数量：{len(updated_keys)}"
            )
        except Exception as e:
            log_event(logging.ERROR, 'apikey', '删除 key 失败', user_id=user_ctx.user_id, error=str(e))
            await event.reply(f"❌ 删除失败：{str(e)[:160]}")
        return

    if sub_cmd in ("test", "check"):
        model_id = rt.get("current_model_id", "qwen3-coder-plus")
        try:
            result = await user_ctx.get_model_manager().validate_model(model_id)
            if result.get("success"):
                _clear_ai_key_issue(rt)
                user_ctx.save_state()
                await event.reply(
                    f"✅ 模型测试成功\n"
                    f"模型：`{model_id}`\n"
                    f"延迟：{result.get('latency', '-') }ms"
                )
            else:
                err = str(result.get("error", "unknown"))
                if _looks_like_ai_key_issue(err):
                    _mark_ai_key_issue(rt, err)
                    user_ctx.save_state()
                await event.reply(
                    f"❌ 模型测试失败\n"
                    f"模型：`{model_id}`\n"
                    f"错误：{err[:180]}"
                )
        except Exception as e:
            await event.reply(f"❌ 测试失败：{str(e)[:180]}")
        return

    await event.reply(
        "未知命令。用法：\n"
        "`apikey show`\n"
        "`apikey set <key>`\n"
        "`apikey add <key>`\n"
        "`apikey del <序号>`\n"
        "`apikey test`"
    )


async def process_user_command(client, event, user_ctx: UserContext, global_config: dict):
    """处理用户命令。"""
    state = user_ctx.state
    rt = state.runtime
    presets = user_ctx.presets
    
    text = event.raw_text.strip()
    if not text:
        return

    my = text.split()
    if not my:
        return

    raw_cmd = str(my[0]).strip()
    if not raw_cmd:
        return

    # 仅解析“命令形态”文本，避免把通知正文(⚠️/🔢/📊开头)当成未知命令。
    # 兼容 `/help` 与中文命令别名 `暂停/恢复`。
    normalized_cmd = raw_cmd[1:] if raw_cmd.startswith("/") else raw_cmd
    if not normalized_cmd:
        return

    allowed_cn_cmds = {"暂停", "恢复"}
    is_ascii_cmd = (
        normalized_cmd[0].isalpha()
        and all(ch.isalnum() or ch in {"_", "-"} for ch in normalized_cmd)
    )
    if normalized_cmd not in allowed_cn_cmds and not is_ascii_cmd:
        return

    cmd = normalized_cmd.lower()
    
    safe_log_text = text[:50]
    if cmd in {"apikey", "ak"}:
        safe_log_text = f"{raw_cmd} ***"
    log_event(logging.INFO, 'user_cmd', '处理用户命令', user_id=user_ctx.user_id, data=safe_log_text)
    
    try:
        # ========== help命令 ==========
        if cmd == "help":
            mes = """**️ 命令列表 (Commands)**

**基础控制**
- `open` : 开启押注
- `off`  : 停止押注
- `pause` : 仅暂停当前账号押注（不影响其他账号）
- `resume` : 恢复当前账号押注
- `st [预设名]` : 启动预设并自动测算 (例: `st yc`)

**参数设置**
- `gf [金额]` : 设置本金 (例: `gf 1000000`)
- `set [炸] [赢] [停] [盈停]` : 设置风控参数
  (例: `set 5 1000000 3 5` -> 炸5次, 赢100w, 停3局, 盈停5局)
- `warn [次数]` : 设置连输告警阈值 (例: `warn 2`)
- `wlc [次数]` : `warn` 的简写命令

**模型与策略**
- `model [list|select|reload]` : 模型管理 (例: `model select 1`)
- `apikey [show|set|add|del|test]` : 管理当前账号 AI key (`ak` 同义)
- `ms [模式]` : 切换模式 (0:反投, 1:预测, 2:追投)

**测算功能**
- `yc [预设名]` : 测算预设策略盈利 (例: `yc yc05`)
- `yc [参数...]` : 自定义参数测算 (例: `yc 1 13 3 2.1 2.1 2.05 500`)

**数据管理**
- `res tj` : 重置统计数据
- `res state` : 清空历史与状态
- `res bet` : 重置押注策略
- `explain` : 查看AI决策解释
- `stats` : 查看连大、连小、连输统计
- `balance` : 查询账户余额
- `xx` : 清理配置群中“我发送的消息”

**发布更新**
- `ver` : 查看版本概览（最近3个Tag + 最近3个Commit）
- `update [版本|提交]` : 更新到指定版本（留空默认最新）
- `reback [版本|提交]` : 回退到指定版本
- `restart` : 重启当前进程

**预设管理**
- `ys [名] ...` : 保存预设
- `yss` : 查看所有预设
- `yss dl [名]` : 删除预设

**多用户管理**
- `users` : 查看当前用户状态
- `status` : 查看仪表盘
"""
            log_event(logging.INFO, 'user_cmd', '显示帮助', user_id=user_ctx.user_id)
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return
        
        # open - 开启押注 - 与master一致
        if cmd == "open":
            rt["switch"] = True
            rt["open_ydx"] = True
            rt["bet"] = False
            rt["bet_on"] = True
            rt["mode_stop"] = True
            rt["manual_pause"] = False
            _clear_lose_recovery_tracking(rt)
            user_ctx.save_state()
            mes = "押注已启动"
            message = await send_to_admin(client, mes, user_ctx, global_config)
            log_event(logging.INFO, 'user_cmd', '开启押注', user_id=user_ctx.user_id)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return
        
        # off - 停止押注 - 与master一致
        if cmd == "off":
            rt["switch"] = False
            rt["bet"] = False
            rt["open_ydx"] = False
            rt["bet_on"] = False
            rt["manual_pause"] = False
            _clear_lose_recovery_tracking(rt)
            user_ctx.save_state()
            mes = "押注已停止"
            message = await send_to_admin(client, mes, user_ctx, global_config)
            log_event(logging.INFO, 'user_cmd', '停止押注', user_id=user_ctx.user_id)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd == "xx":
            target_groups = []
            target_groups.extend(_iter_targets(user_ctx.config.groups.get("zq_group", [])))
            target_groups.extend(_iter_targets(user_ctx.config.groups.get("monitor", [])))

            # 去重并保持顺序
            unique_groups = []
            seen = set()
            for gid in target_groups:
                key = str(gid)
                if key in seen:
                    continue
                seen.add(key)
                unique_groups.append(gid)

            if not unique_groups:
                message = await send_to_admin(client, "未配置可清理的群组（zq_group/monitor）", user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
                return

            deleted_total = 0
            failed_groups = []
            scanned_groups = 0

            for gid in unique_groups:
                try:
                    msg_ids = [msg.id async for msg in client.iter_messages(gid, from_user="me", limit=500)]
                    scanned_groups += 1
                    if msg_ids:
                        await client.delete_messages(gid, msg_ids)
                        deleted_total += len(msg_ids)
                except Exception as e:
                    failed_groups.append(f"{gid}: {str(e)[:40]}")

            mes = (
                "群组消息已清理\n"
                f"扫描群组：{scanned_groups}\n"
                f"删除消息：{deleted_total}"
            )
            if failed_groups:
                mes += "\n失败群组：\n" + "\n".join(f"- {item}" for item in failed_groups[:5])

            log_event(
                logging.INFO,
                'user_cmd',
                '执行xx清理',
                user_id=user_ctx.user_id,
                groups=scanned_groups,
                deleted=deleted_total,
                failed=len(failed_groups),
            )
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 3))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return
        
        # pause/resume - 暂停/恢复押注（新增，master没有但有用）
        if cmd in ("pause", "暂停"):
            if rt.get("manual_pause", False):
                await send_to_admin(client, "⏸ 当前账号已是暂停状态", user_ctx, global_config)
                return
            await _clear_pause_countdown_notice(client, user_ctx)
            rt["bet_on"] = False
            rt["bet"] = False
            rt["mode_stop"] = True
            rt["manual_pause"] = True
            _clear_lose_recovery_tracking(rt)
            user_ctx.save_state()
            mes = "⏸ 已暂停当前账号押注"
            await send_to_admin(client, mes, user_ctx, global_config)
            log_event(logging.INFO, 'user_cmd', '暂停押注', user_id=user_ctx.user_id)
            return
        
        if cmd in ("resume", "恢复"):
            if not rt.get("switch", True):
                await send_to_admin(client, "当前为 off 状态，请先执行 `open`", user_ctx, global_config)
                return
            rt["bet_on"] = True
            rt["mode_stop"] = True
            rt["manual_pause"] = False
            user_ctx.save_state()
            mes = "▶️ 已恢复当前账号押注"
            await send_to_admin(client, mes, user_ctx, global_config)
            log_event(logging.INFO, 'user_cmd', '恢复押注', user_id=user_ctx.user_id)
            return
        
        # st - 启动预设 - 与master一致
        if cmd == "st" and len(my) > 1:
            preset_name = my[1]
            if preset_name in presets:
                preset = presets[preset_name]
                rt["continuous"] = int(preset[0])
                rt["lose_stop"] = int(preset[1])
                rt["lose_once"] = float(preset[2])
                rt["lose_twice"] = float(preset[3])
                rt["lose_three"] = float(preset[4])
                rt["lose_four"] = float(preset[5])
                rt["initial_amount"] = int(preset[6])
                rt["current_preset_name"] = preset_name
                rt["bet_amount"] = int(preset[6])
                rt["bet"] = False  # 修复：st命令不应直接设置bet=True
                rt["risk_deep_triggered_milestones"] = []
                rt["fund_pause_notified"] = False
                rt["limit_stop_notified"] = False
                _clear_lose_recovery_tracking(rt)
                user_ctx.save_state()
                
                mes = f"预设启动成功: {preset_name} ({preset[0]} {preset[1]} {preset[2]} {preset[3]} {preset[4]} {preset[5]} {preset[6]})"
                log_event(logging.INFO, 'user_cmd', '启动预设', user_id=user_ctx.user_id, preset=preset_name)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
                await yc_command_handler_multiuser(
                    client,
                    event,
                    [preset_name],
                    user_ctx,
                    global_config,
                    auto_trigger=True,
                )
            else:
                await send_to_admin(client, f"预设不存在: {preset_name}", user_ctx, global_config)
            return
        
        # stats - 查看连大、连小、连输统计
        if cmd == "stats":
            if len(state.history) < 10:
                await send_to_admin(client, "历史数据不足，无法生成统计", user_ctx, global_config)
                return
            
            windows = [1000, 500, 200, 100]
            stats = {"连大": [], "连小": [], "连输": []}
            all_ns = set()
            
            for window in windows:
                history_window = state.history[-window:]
                result_counts = count_consecutive(history_window)
                bet_sequence_log = state.bet_sequence_log[-window:]
                lose_streaks = count_lose_streaks(bet_sequence_log)
                
                stats["连大"].append(result_counts["大"])
                stats["连小"].append(result_counts["小"])
                stats["连输"].append(lose_streaks)
                
                all_ns.update(result_counts["大"].keys())
                all_ns.update(result_counts["小"].keys())
                all_ns.update(lose_streaks.keys())
            
            mes = "```\n最近局数“连大、连小、连输”统计\n\n"
            for category in ["连大", "连小", "连输"]:
                mes += f"{category}\n"
                mes += "================================\n"
                mes += "类别 | 1000|  500  |200 | 100|\n"
                mes += "--------------------------------\n"
                sorted_ns = sorted(all_ns, reverse=True)
                for n in sorted_ns:
                    if any(n in stats[category][i] for i in range(len(windows))):
                        mes += f" {str(n).center(2)}  |"
                        for i in range(len(windows)):
                            count = stats[category][i].get(n, 0)
                            value = str(count) if count > 0 else "-"
                            mes += f" {value.center(3)} |"
                        mes += "\n"
                mes += "\n"
            mes += "```"
            
            log_event(logging.INFO, 'user_cmd', '查看统计', user_id=user_ctx.user_id)
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 30))
            return
        
        # status - 查看仪表盘 - 与master一致
        if cmd == "status":
            dashboard = format_dashboard(user_ctx)
            message = await send_to_admin(client, dashboard, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return
        
        # ========== 参数设置命令 ==========
        # gf - 设置资金 - 与master一致
        if cmd == "gf":
            old_fund = rt.get("gambling_fund", 0)
            if len(my) == 1:
                rt["gambling_fund"] = rt.get("gambling_fund", 2000000)
                mes = f"菠菜资金已重置为 {rt['gambling_fund'] / 10000:.2f} 万"
            elif len(my) == 2:
                try:
                    new_fund = int(my[1])
                    if new_fund < 0:
                        mes = "菠菜资金不能设置为负数"
                    else:
                        account_balance = rt.get("account_balance", 0)
                        if new_fund > account_balance:
                            new_fund = account_balance
                            mes = f"设置的资金超过账户余额，已调整为 {new_fund / 10000:.2f} 万"
                        else:
                            mes = f"菠菜资金已设置为 {new_fund / 10000:.2f} 万"
                        rt["gambling_fund"] = new_fund
                except ValueError:
                    mes = "无效的金额格式，请输入整数"
            else:
                mes = "gf 命令格式错误：gf 或 gf [金额]"
            
            log_event(logging.INFO, 'user_cmd', '设置资金', user_id=user_ctx.user_id, mes=mes)
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            
            if rt.get("gambling_fund", 0) != old_fund:
                log_event(logging.INFO, 'user_cmd', '资金变更', user_id=user_ctx.user_id, 
                         old=old_fund, new=rt.get("gambling_fund", 0))
                await check_bet_status(client, user_ctx, global_config)
            return
        
        # set - 设置风控参数 - 与master一致
        if cmd == "set" and len(my) >= 5:
            try:
                rt["explode"] = int(my[1])
                rt["profit"] = int(my[2])
                rt["stop"] = int(my[3])
                rt["profit_stop"] = int(my[4])
                if len(my) > 5:
                    rt["stop_count"] = int(my[5])
                user_ctx.save_state()
                mes = f"设置成功: 炸{rt['explode']}次, 盈利{rt['profit']/10000:.2f}万, 暂停{rt['stop']}局, 盈停{rt['profit_stop']}局"
                log_event(logging.INFO, 'user_cmd', '设置参数', user_id=user_ctx.user_id,
                         explode=rt['explode'], profit=rt['profit'], stop=rt['stop'], profit_stop=rt['profit_stop'])
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            except ValueError:
                await send_to_admin(client, "参数格式错误，请输入整数", user_ctx, global_config)
            return

        # warn/wlc - 设置连输告警阈值 - 与master一致
        if cmd in ("warn", "wlc"):
            if len(my) > 1:
                try:
                    warning_count = int(my[1])
                    if warning_count < 1:
                        raise ValueError
                    rt["warning_lose_count"] = warning_count
                    user_ctx.save_state()
                    mes = f"连输告警阈值已设置为: {warning_count} 次"
                    log_event(logging.INFO, 'user_cmd', '设置连输告警阈值', user_id=user_ctx.user_id, warning_lose_count=warning_count)
                except ValueError:
                    mes = "❌ 参数错误：阈值必须是 >= 1 的整数。用法: warn <次数>"
            else:
                mes = (
                    f"当前连输告警阈值: {rt.get('warning_lose_count', 3)} 次\n"
                    "用法: warn <次数> 或 wlc <次数>"
                )
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return
        
        # model - 模型管理 - 使用与master一致的handle_model_command
        if cmd == "model":
            if len(my) == 2 and my[1].lower().startswith("v"):
                mes = "当前算法固定为 V10，无需切换。请使用 `model select <id>` 切换模型。"
                await event.reply(mes)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                return
            await handle_model_command_multiuser(event, my[1:], user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd in ("apikey", "ak"):
            await handle_apikey_command_multiuser(event, my[1:], user_ctx)
            # 防止 key 在命令消息中长期可见
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 3))
            return

        # ========== 发布更新命令 ==========
        if cmd in ("ver", "version"):
            result = await asyncio.to_thread(list_version_catalog, None, 3)
            if not result.get("success"):
                mes = f"❌ 版本查询失败：{result.get('error', 'unknown')}"
            else:
                current = result.get("current", {})
                current_short = current.get("short_commit", "unknown") or "unknown"
                current_tag_exact = current.get("current_tag", "") or ""
                nearest_tag = current.get("nearest_tag", "") or ""
                if current_tag_exact:
                    current_tag_display = current_tag_exact.upper()
                elif nearest_tag:
                    current_tag_display = f"无（最近: {nearest_tag}）"
                else:
                    current_tag_display = "无"

                remote_head = result.get("remote_head", {}) or {}
                remote_head_short = remote_head.get("short_commit", "-") or "-"
                remote_head_tag = result.get("remote_head_tag", "") or ""
                pending_tags = result.get("pending_tags", [])
                recent_tags = result.get("recent_tags", []) or []
                recent_commits = result.get("recent_commits", []) or []

                latest_tag_target = pending_tags[0] if pending_tags else ""
                if latest_tag_target:
                    latest_tag_line = f"{latest_tag_target}（复制 `update {latest_tag_target}`）"
                else:
                    latest_tag_line = "无（已是最新）"

                latest_commit_target = ""
                if remote_head_short not in {"", "-", "unknown"} and remote_head_short != current_short:
                    latest_commit_target = remote_head_short

                if latest_commit_target:
                    extra_tag_note = f" | Tag:{remote_head_tag}" if remote_head_tag else " | 未打Tag"
                    latest_commit_line = f"{latest_commit_target}{extra_tag_note}（复制 `update {latest_commit_target}`）"
                else:
                    latest_commit_line = "无（已是最新）"

                lines = [
                    "📦 版本信息概览",
                    f"当前 Tag：{current_tag_display}",
                    f"当前Commit：{current_short}",
                    f"最新 Tag：{latest_tag_line}",
                    f"最新Commit：{latest_commit_line}",
                    "",
                    "⚠️  操作提示：",
                    "- update <Tag版本号|Commit哈希>：更新到指定版本/提交",
                    "- reback <Tag版本号|Commit哈希>：回滚到指定版本/提交",
                    "- restart：重启应用",
                    "",
                    "🔖 最近 3 个正式版本（Tag，新→旧）",
                ]

                if recent_tags:
                    for idx, item in enumerate(recent_tags[:3], 1):
                        tag = item.get("tag", "")
                        date = item.get("date", "") or "-"
                        summary = item.get("summary", "") or "-"
                        lines.append(f"{idx}. {tag} | {date} | {summary}")
                else:
                    lines.append("1. 无")

                lines.extend(["", "💻 最近 3 个开发提交（Commit，新→旧）"])
                if recent_commits:
                    for idx, item in enumerate(recent_commits[:3], 1):
                        short_commit = item.get("short_commit", "") or "-"
                        date = item.get("date", "") or "-"
                        summary = item.get("summary", "") or "-"
                        suffix = "（当前提交）" if short_commit == current_short else ""
                        lines.append(f"{idx}. {short_commit} | {date} | {summary}{suffix}")
                else:
                    lines.append("1. 无")
                mes = "\n".join(lines)

            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return

        if cmd in ("update", "up", "upnow", "upref", "upcommit"):
            target_ref = my[1].strip() if len(my) > 1 else ""
            await send_to_admin(client, f"🔄 开始更新：{target_ref or 'latest'}", user_ctx, global_config)
            result = await asyncio.to_thread(update_to_version, None, target_ref)
            if result.get("success"):
                if result.get("no_change"):
                    await send_to_admin(client, f"✅ {result.get('message', '当前已是目标版本')}", user_ctx, global_config)
                else:
                    after = result.get("after", {})
                    resolved = result.get("resolved_target", "") or result.get("target_ref", target_ref or "latest")
                    mes = (
                        "✅ 更新成功\n"
                        f"目标：{resolved}\n"
                        f"当前：{after.get('display_version', after.get('short_commit', 'unknown'))}\n"
                        "请执行 `restart` 重启脚本使新版本生效"
                    )
                    await send_to_admin(client, mes, user_ctx, global_config)
            else:
                blocking_paths = result.get("blocking_paths", [])
                detail = result.get("detail", "")
                mes_lines = [f"❌ 更新失败：{result.get('error', 'unknown')}"]
                if blocking_paths:
                    mes_lines.append("阻塞文件：")
                    mes_lines.extend([f"- {path}" for path in blocking_paths[:10]])
                if detail:
                    mes_lines.append(f"详情：{detail[:200]}")
                await send_to_admin(client, "\n".join(mes_lines), user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd in ("reback", "rollback", "uprollback"):
            target_ref = my[1].strip() if len(my) > 1 else ""
            if not target_ref:
                await send_to_admin(client, "用法：`reback <版本号|commit|branch>`", user_ctx, global_config)
                return

            await send_to_admin(client, f"↩️ 开始回退到：{target_ref}", user_ctx, global_config)
            result = await asyncio.to_thread(reback_to_version, None, target_ref)
            if result.get("success"):
                after = result.get("after", {})
                resolved = result.get("resolved_target", target_ref)
                mes = (
                    "✅ 回退成功\n"
                    f"目标：{resolved}\n"
                    f"当前：{after.get('display_version', after.get('short_commit', 'unknown'))}\n"
                    "请执行 `restart` 重启脚本使回退生效"
                )
                await send_to_admin(client, mes, user_ctx, global_config)
            else:
                mes = f"❌ 回滚失败：{result.get('error', 'unknown')}"
                if result.get("detail"):
                    mes += f"\n详情：{str(result.get('detail'))[:200]}"
                await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd in ("restart", "reboot"):
            service_name = resolve_systemd_service_name()
            if service_name:
                mes = f"♻️ 收到重启指令，2 秒后通过 systemd 重启服务：{service_name}"
            else:
                mes = "♻️ 收到重启指令，2 秒后自动重启进程..."
            await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 3))
            asyncio.create_task(restart_process())
            return
        
        # ========== 数据管理命令 ==========
        # res - 重置命令 - 与master一致
        if cmd == "res":
            if len(my) > 1:
                if my[1] == "tj":
                    # 重置统计
                    rt["win_total"] = 0
                    rt["total"] = 0
                    rt["stats_last_report_total"] = 0
                    rt["earnings"] = 0
                    rt["period_profit"] = 0
                    rt["win_count"] = 0
                    rt["lose_count"] = 0
                    rt["bet_sequence_count"] = 0
                    rt["explode_count"] = 0
                    rt["bet_amount"] = int(rt.get("initial_amount", 500))
                    rt["risk_pause_acc_rounds"] = 0
                    rt["risk_pause_snapshot_count"] = -1
                    rt["risk_pause_cycle_active"] = False
                    rt["risk_pause_recovery_passes"] = 0
                    rt["risk_base_hit_streak"] = 0
                    rt["risk_pause_level1_hit"] = False
                    rt["risk_deep_triggered_milestones"] = []
                    _clear_lose_recovery_tracking(rt)
                    user_ctx.save_state()
                    mes = "统计数据已重置"
                    log_event(logging.INFO, 'user_cmd', '重置统计数据', user_id=user_ctx.user_id, action='completed')
                elif my[1] == "state":
                    # 重置状态
                    state.history = []
                    state.bet_type_history = []
                    rt["win_total"] = 0
                    rt["total"] = 0
                    rt["stats_last_report_total"] = 0
                    rt["earnings"] = 0
                    rt["period_profit"] = 0
                    rt["win_count"] = 0
                    rt["lose_count"] = 0
                    rt["bet_sequence_count"] = 0
                    rt["explode_count"] = 0
                    rt["bet_amount"] = int(rt.get("initial_amount", 500))
                    rt["risk_pause_acc_rounds"] = 0
                    rt["risk_pause_snapshot_count"] = -1
                    rt["risk_pause_cycle_active"] = False
                    rt["risk_pause_recovery_passes"] = 0
                    rt["risk_base_hit_streak"] = 0
                    rt["risk_pause_level1_hit"] = False
                    rt["risk_deep_triggered_milestones"] = []
                    _clear_lose_recovery_tracking(rt)
                    user_ctx.save_state()
                    mes = "状态文件已重置"
                    log_event(logging.INFO, 'user_cmd', '重置状态文件', user_id=user_ctx.user_id, action='completed')
                elif my[1] == "bet":
                    # 重置押注策略
                    rt["win_count"] = 0
                    rt["lose_count"] = 0
                    rt["bet_sequence_count"] = 0
                    rt["explode_count"] = 0
                    rt["bet_amount"] = int(rt.get("initial_amount", 500))
                    rt["bet"] = False
                    rt["bet_on"] = False
                    rt["stop_count"] = 0
                    rt["mark"] = True
                    rt["flag"] = True
                    rt["mode_stop"] = True
                    rt["manual_pause"] = False
                    rt["pause_count"] = 0
                    rt["current_bet_seq"] = 1
                    rt["risk_pause_acc_rounds"] = 0
                    rt["risk_pause_snapshot_count"] = -1
                    rt["risk_pause_cycle_active"] = False
                    rt["risk_pause_recovery_passes"] = 0
                    rt["risk_base_hit_streak"] = 0
                    rt["risk_pause_level1_hit"] = False
                    rt["risk_deep_triggered_milestones"] = []
                    _clear_lose_recovery_tracking(rt)
                    user_ctx.save_state()
                    mes = f"押注策略已重置: 初始金额={rt.get('initial_amount', 500)}"
                    log_event(logging.INFO, 'user_cmd', '重置押注策略', user_id=user_ctx.user_id, action='completed')
                else:
                    mes = "无效命令，正确格式：res tj 或 res state 或 res bet"
                    log_event(logging.WARNING, 'user_cmd', '无效重置命令', user_id=user_ctx.user_id, cmd=text)
            else:
                mes = "请指定重置类型：res tj / res state / res bet"
            
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return
        
        # explain - 查看AI决策解释 - 与master一致
        if cmd == "explain":
            last_logic_audit = rt.get("last_logic_audit", "")
            if last_logic_audit:
                log_event(logging.INFO, 'user_cmd', '查看决策解释', user_id=user_ctx.user_id)
                mes = f"🧠 **AI 深度思考归档：**\n```json\n{last_logic_audit}\n```"
                await send_to_admin(client, mes, user_ctx, global_config)
            else:
                await send_to_admin(client, "⚠️ 暂无 AI 决策记录 (需等待 V10 运行至少一次)", user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return
        
        # balance - 查询余额 - 与master一致
        if cmd == "balance":
            try:
                balance = await fetch_balance(user_ctx)
                rt["account_balance"] = balance
                user_ctx.save_state()
                mes = f"账户余额: {format_number(balance)}"
                await send_to_admin(client, mes, user_ctx, global_config)
                log_event(logging.INFO, 'user_cmd', '查询余额', user_id=user_ctx.user_id, balance=balance)
            except Exception as e:
                await send_to_admin(client, f"查询余额失败: {str(e)}", user_ctx, global_config)
            return
        
        # ========== 预设管理命令 ==========
        # ys - 保存预设 - 与master一致
        if cmd == "ys" and len(my) >= 9:
            try:
                preset_name = my[1]
                ys = [int(my[2]), int(my[3]), float(my[4]), float(my[5]), float(my[6]), float(my[7]), int(my[8])]
                presets[preset_name] = ys
                user_ctx.save_presets()
                rt["current_preset_name"] = preset_name
                user_ctx.save_state()
                mes = f"预设保存成功: {preset_name} ({ys[0]} {ys[1]} {ys[2]} {ys[3]} {ys[4]} {ys[5]} {ys[6]})"
                log_event(logging.INFO, 'user_cmd', '保存预设策略', user_id=user_ctx.user_id, preset=preset_name, params=ys)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            except (ValueError, IndexError) as e:
                await send_to_admin(client, f"预设格式错误: {e}", user_ctx, global_config)
            return
        
        # yss - 查看/删除预设 - 与master一致
        if cmd == "yss":
            if len(my) > 2 and my[1] == "dl":
                # 删除预设
                preset_name = my[2]
                if preset_name in presets:
                    del presets[preset_name]
                    user_ctx.save_presets()
                    mes = f"预设删除成功: {preset_name}"
                    log_event(logging.INFO, 'user_cmd', '删除预设', user_id=user_ctx.user_id, preset=preset_name)
                else:
                    mes = "删除失败：预设不存在或格式错误"
                    log_event(logging.WARNING, 'user_cmd', '删除预设失败', user_id=user_ctx.user_id, cmd=text)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            else:
                # 查看所有预设
                if len(presets) > 0:
                    max_key_length = max(len(str(k)) for k in presets.keys())
                    mes = "\n".join(f"'{k.ljust(max_key_length)}': {v}" for k, v in presets.items())
                    log_event(logging.INFO, 'user_cmd', '查看预设', user_id=user_ctx.user_id)
                else:
                    mes = "暂无预设"
                    log_event(logging.INFO, 'user_cmd', '暂无预设', user_id=user_ctx.user_id)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 60))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return
        
        # ========== 测算命令 ==========
        if cmd == "yc":
            # 测算命令 - 与master一致
            await yc_command_handler_multiuser(client, event, my[1:], user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return
        
        # ms - 切换模式 - 与master一致
        if cmd == "ms":
            if len(my) > 1:
                try:
                    mode = int(my[1])
                    mode_names = {0: "反投", 1: "预测", 2: "追投"}
                    if mode in mode_names:
                        rt["bet_mode"] = mode
                        user_ctx.save_state()
                        mes = f"模式已切换: {mode_names[mode]} ({mode})"
                        log_event(logging.INFO, 'user_cmd', '切换模式', user_id=user_ctx.user_id, mode=mode)
                    else:
                        mes = "无效模式: 0=反投, 1=预测, 2=追投"
                except ValueError:
                    mes = "模式必须是数字: 0, 1, 或 2"
            else:
                current_mode = rt.get("bet_mode", 1)
                mode_names = {0: "反投", 1: "预测", 2: "追投"}
                mes = f"当前模式: {mode_names.get(current_mode, '未知')} ({current_mode})\n用法: ms [0|1|2]"
            
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return
        
        # ========== 多用户管理命令 ==========
        # users - 查看所有用户
        if cmd == "users":
            # 获取当前用户信息
            user_info = f"👤 当前用户: {user_ctx.config.name} (ID: {user_ctx.user_id})\n"
            user_info += f"💰 菠菜资金: {format_number(rt.get('gambling_fund', 0))}\n"
            user_info += f"📊 状态: {get_bet_status_text(rt)}\n"
            user_info += f"🎯 预设: {rt.get('current_preset_name', '无')}\n"
            user_info += f"🤖 模型: {rt.get('current_model_id', 'default')}\n"
            user_info += f"📈 胜率: {rt.get('win_total', 0)}/{rt.get('total', 0)}"
            message = await send_to_admin(client, user_info, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 30))
            return
        
        # 未知命令
        log_event(logging.DEBUG, 'user_cmd', '未知命令', user_id=user_ctx.user_id, data=text[:50])
        message = await send_to_admin(client, f"未知命令: {cmd}\n输入 help 查看帮助", user_ctx, global_config)
        asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
        
    except Exception as e:
        log_event(logging.ERROR, 'user_cmd', '命令执行出错', user_id=user_ctx.user_id, error=str(e))
        await send_to_admin(client, f"命令执行出错: {e}", user_ctx, global_config)


async def check_bet_status(client, user_ctx: UserContext, global_config: dict):
    """检查押注状态 - 与master版本一致"""
    rt = user_ctx.state.runtime
    if rt.get("manual_pause", False):
        return
    next_bet_amount = calculate_bet_amount(rt)
    if next_bet_amount <= 0:
        rt["bet"] = False
        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        if not rt.get("limit_stop_notified", False):
            lose_stop = int(rt.get("lose_stop", 13))
            await send_to_admin(
                client,
                f"⚠️ 已达到预设连投上限（{lose_stop} 手），已保持暂停",
                user_ctx,
                global_config,
            )
            rt["limit_stop_notified"] = True
        user_ctx.save_state()
        return

    rt["limit_stop_notified"] = False
    if is_fund_available(user_ctx, next_bet_amount) and not rt.get("bet", False) and rt.get("switch", True) and rt.get("stop_count", 0) == 0:
        await _clear_pause_countdown_notice(client, user_ctx)
        # 这里只恢复“可下注状态”，不应提前标记为“已下注”。
        # bet=True 只能在真实点击下注成功后设置，避免结算时序误判。
        rt["bet"] = False
        rt["bet_on"] = True
        rt["mode_stop"] = True
        rt["pause_count"] = 0
        rt["fund_pause_notified"] = False
        user_ctx.save_state()
        mes = (
            "✅ 资金条件已满足，恢复可下注状态\n"
            f"当前资金：{rt.get('gambling_fund', 0) / 10000:.2f} 万\n"
            f"接续倍投金额：{format_number(next_bet_amount)}\n"
            "说明：本提示仅表示“可下注”，实际下注仍以盘口事件触发为准"
        )
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            mes,
            ttl_seconds=120,
            attr_name="status_transition_message",
        )
    elif not is_fund_available(user_ctx, next_bet_amount):
        if _sync_fund_from_account_when_insufficient(rt, next_bet_amount):
            log_event(
                logging.INFO,
                'status',
                '检查状态时资金不足触发资金同步',
                user_id=user_ctx.user_id,
                data=(
                    f"need={next_bet_amount}, fund={rt.get('gambling_fund', 0)}, "
                    f"account={rt.get('account_balance', 0)}"
                ),
            )
            user_ctx.save_state()

        if is_fund_available(user_ctx, next_bet_amount):
            await _clear_pause_countdown_notice(client, user_ctx)
            rt["bet"] = False
            rt["bet_on"] = True
            rt["mode_stop"] = True
            rt["pause_count"] = 0
            rt["fund_pause_notified"] = False
            user_ctx.save_state()
            mes = (
                "✅ 资金同步后已恢复可下注状态\n"
                f"当前资金：{rt.get('gambling_fund', 0) / 10000:.2f} 万\n"
                f"接续倍投金额：{format_number(next_bet_amount)}\n"
                "说明：本提示仅表示“可下注”，实际下注仍以盘口事件触发为准"
            )
            await _send_transient_admin_notice(
                client,
                user_ctx,
                global_config,
                mes,
                ttl_seconds=120,
                attr_name="status_transition_message",
            )
            return

        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        if not rt.get("fund_pause_notified", False):
            mes = "⚠️ 菠菜资金不足，已自动暂停押注"
            await send_message_v2(
                client,
                "fund_pause",
                mes,
                user_ctx,
                global_config,
                title=f"菠菜机器人 {user_ctx.config.name} 资金风控暂停",
                desp=mes,
            )
            rt["fund_pause_notified"] = True
        user_ctx.save_state()


def _parse_yc_params(args, presets):
    if not args:
        return None, None, (
            "📊 **测算功能**\n\n"
            "用法:\n"
            "`yc [预设名]` - 测算已有预设\n"
            "`yc [参数...]` - 自定义参数测算\n\n"
            "例: `yc yc05` 或 `yc 1 13 3 2.1 2.1 2.05 500`"
        )

    if args[0] in presets:
        preset = presets[args[0]]
        params = {
            "continuous": int(preset[0]),
            "lose_stop": int(preset[1]),
            "lose_once": float(preset[2]),
            "lose_twice": float(preset[3]),
            "lose_three": float(preset[4]),
            "lose_four": float(preset[5]),
            "initial_amount": int(preset[6]),
        }
        return params, args[0], None

    if len(args) >= 7:
        try:
            params = {
                "continuous": int(args[0]),
                "lose_stop": int(args[1]),
                "lose_once": float(args[2]),
                "lose_twice": float(args[3]),
                "lose_three": float(args[4]),
                "lose_four": float(args[5]),
                "initial_amount": int(args[6]),
            }
            return params, "自定义", None
        except ValueError:
            return None, None, "❌ 参数格式错误，请确保所有参数都是数字"

    return None, None, f"❌ 预设 `{args[0]}` 不存在，且参数不足7个"


def _calculate_yc_sequence(params):
    initial = max(0, int(params["initial_amount"]))
    lose_stop = max(1, int(params["lose_stop"]))
    table_steps = 15
    multipliers = [
        float(params["lose_once"]),
        float(params["lose_twice"]),
        float(params["lose_three"]),
        float(params["lose_four"]),
    ]
    max_single_bet_limit = 50_000_000
    start_streak = max(1, int(params["continuous"]))

    rows = []
    prev_bet = initial
    cumulative_loss = 0

    for i in range(table_steps):
        if i == 0:
            multiplier = 1.0
            bet = initial
        else:
            multiplier = multipliers[min(i - 1, 3)]
            bet = int(prev_bet * multiplier)

        if bet > max_single_bet_limit:
            bet = max_single_bet_limit

        cumulative_loss += bet
        profit_if_win = bet - (cumulative_loss - bet)
        rows.append(
            {
                "streak": start_streak + i,
                "multiplier": multiplier,
                "bet": bet,
                "profit_if_win": profit_if_win,
                "cumulative_loss": cumulative_loss,
            }
        )
        prev_bet = bet

    total_investment = rows[-1]["cumulative_loss"] if rows else 0
    max_bet = max((row["bet"] for row in rows), default=0)
    effective_rows = rows[:lose_stop]
    effective_streak = effective_rows[-1]["streak"] if effective_rows else start_streak
    effective_investment = effective_rows[-1]["cumulative_loss"] if effective_rows else 0
    effective_profit = effective_rows[-1]["profit_if_win"] if effective_rows else 0
    return {
        "rows": rows,
        "total_investment": total_investment,
        "max_bet": max_bet,
        "max_single_bet_limit": max_single_bet_limit,
        "start_streak": start_streak,
        "lose_stop": lose_stop,
        "table_steps": table_steps,
        "effective_rows": effective_rows,
        "effective_streak": effective_streak,
        "effective_investment": effective_investment,
        "effective_profit": effective_profit,
    }


def _build_yc_result_message(params, preset_name: str, current_fund: int, auto_trigger: bool) -> str:
    calc = _calculate_yc_sequence(params)
    rows = calc["rows"]
    effective_rows = calc["effective_rows"]
    effective_streak = calc["effective_streak"]
    effective_investment = calc["effective_investment"]
    effective_profit = calc["effective_profit"]
    max_single_bet_limit = calc["max_single_bet_limit"]

    def fmt_wan(value: int) -> str:
        return f"{value / 10000:,.1f}"

    def fmt_table_wan(value: int) -> str:
        wan = value / 10000
        if abs(wan) >= 1000:
            return f"{wan:,.0f}"
        return f"{wan:.1f}"

    header_line = "🔮 已根据当前预设自动测算\n" if auto_trigger else ""
    command_text = (
        f"{params['continuous']} {params['lose_stop']} "
        f"{params['lose_once']} {params['lose_twice']} {params['lose_three']} {params['lose_four']} {params['initial_amount']}"
    )

    fund_text = f"{fmt_wan(current_fund)}万" if current_fund > 0 else "未设置"
    cover_streak = 0
    cover_required = 0
    cover_profit = 0
    if current_fund > 0 and effective_rows:
        cover_rows = [row for row in effective_rows if row["cumulative_loss"] <= current_fund]
        if cover_rows:
            cover_row = cover_rows[-1]
            cover_streak = int(cover_row["streak"])
            cover_required = int(cover_row["cumulative_loss"])
            cover_profit = int(cover_row["profit_if_win"])
    elif effective_rows:
        cover_streak = int(effective_streak)
        cover_required = int(effective_investment)
        cover_profit = int(effective_profit)

    lines = []
    if header_line:
        lines.append(header_line.rstrip("\n"))
    lines.append("```")
    lines.extend(
        [
            "🎯 策略参数",
            f"预设名称：{preset_name}",
            f"菠菜资金：{fund_text}",
            f"策略命令: {command_text}",
            f"🏁 起始连数: {params['continuous']}",
            f"🔢 下注次数: {params['lose_stop']}次",
            f"💰 首注金额: {fmt_wan(int(params['initial_amount']))}万",
            f"💰 单注上限: {max_single_bet_limit / 10000:,.0f}万",
            "",
            "🎯 策略总结:",
            f"菠菜资金：{fund_text}",
            f"资金最多连数: {cover_streak}连",
            f"{cover_streak}连所需本金: {fmt_wan(cover_required)}万",
            f"{cover_streak}连获得盈利: {fmt_wan(cover_profit)}万",
            "",
            "连数|倍率|下注| 盈利 |所需本金",
            "---|----|------|------|------",
        ]
    )

    for row in rows:
        multiplier_text = f"{row['multiplier']:.2f}".rstrip("0")
        if multiplier_text.endswith("."):
            multiplier_text += "0"
        row_text = (
            f"{str(row['streak']).center(3)}|"
            f"{multiplier_text.center(4)}|"
            f"{fmt_table_wan(row['bet']).center(6)}|"
            f"{fmt_table_wan(row['profit_if_win']).center(6)}|"
            f"{fmt_table_wan(row['cumulative_loss']).center(6)}"
        )
        lines.append(row_text)

    lines.append("```")
    return "\n".join(lines)


async def yc_command_handler_multiuser(
    client,
    event,
    args,
    user_ctx: UserContext,
    global_config: dict,
    auto_trigger: bool = False,
):
    """处理 yc 测算命令，支持 st 切换预设后自动触发。"""
    presets = user_ctx.presets
    rt = user_ctx.state.runtime

    params, preset_name, error_msg = _parse_yc_params(args, presets)
    if error_msg:
        await send_to_admin(client, error_msg, user_ctx, global_config)
        return

    result_msg = _build_yc_result_message(
        params=params,
        preset_name=preset_name,
        current_fund=int(rt.get("gambling_fund", 0)),
        auto_trigger=auto_trigger,
    )
    await send_to_admin(client, result_msg, user_ctx, global_config)
    log_event(
        logging.INFO,
        'yc',
        '测算完成',
        user_id=user_ctx.user_id,
        preset=preset_name,
        auto_trigger=auto_trigger,
    )


async def fetch_balance(user_ctx: UserContext) -> int:
    zhuque = user_ctx.config.zhuque
    cookie = zhuque.get("cookie", "")
    csrf_token = zhuque.get("csrf_token", "") or zhuque.get("x_csrf", "")
    api_url = zhuque.get("api_url", "https://zhuque.in/api/user/getInfo?")
    
    if not cookie or not csrf_token:
        return 0
    
    headers = {
        "Cookie": cookie,
        "X-Csrf-Token": csrf_token,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 401:
                    user_ctx.set_runtime("balance_status", "auth_failed")
                    log_event(logging.ERROR, 'balance', '认证失败(401)，请更新 Cookie',
                              user_id=user_ctx.user_id)
                    return user_ctx.get_runtime("account_balance", 0)
                
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict) and data.get("status", 200) != 200:
                        log_event(logging.WARNING, 'balance', 'API返回错误',
                                  user_id=user_ctx.user_id, message=data.get("message"))
                        return user_ctx.get_runtime("account_balance", 0)
                    
                    balance = int(data.get("data", {}).get("bonus", 0))
                    user_ctx.set_runtime("balance_status", "success")
                    return balance
    except Exception as e:
        user_ctx.set_runtime("balance_status", "network_error")
        log_event(logging.ERROR, 'balance', '获取余额失败',
                  user_id=user_ctx.user_id, data=str(e))
    
    return 0
