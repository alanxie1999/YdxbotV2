"""
main_multiuser.py - 多用户版本主程序
版本: 2.0.0
日期: 2026-02-20
功能: 支持多用户并发运行的Telegram客户端
"""

import logging
import asyncio
import os
import sys
import errno
import json
try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt
else:
    msvcrt = None
from typing import Any, Dict, List
from types import SimpleNamespace
import requests
from telethon import TelegramClient, events
from logging.handlers import TimedRotatingFileHandler
from user_manager import UserManager, UserContext
from update_manager import periodic_release_check_loop

# 日志配置
logger = logging.getLogger('main_multiuser')
logger.setLevel(logging.DEBUG)
logger.propagate = False

ACCOUNT_LOG_BACKUP_DAYS = 3
_MAIN_ACCOUNT_SLUG_REGISTRY: Dict[str, str] = {}
MAIN_ACCOUNT_LOG_ROOT = os.path.join("logs", "accounts")


def _sanitize_account_slug(text: str, fallback: str = "unknown") -> str:
    raw = str(text or "").strip().lower().replace(" ", "-")
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
    return cleaned or fallback


def _build_account_label(account_slug: str) -> str:
    return f"ydx-{account_slug}"


def _resolve_user_ctx_log_slug(user_ctx: UserContext) -> str:
    slug = str(getattr(user_ctx, "account_slug", "") or "").strip()
    if slug:
        return slug
    user_id = str(getattr(user_ctx, "user_id", 0) or 0)
    return _sanitize_account_slug("", fallback=(f"user-{user_id}" if user_id not in {"", "0"} else "unknown"))


def register_main_user_log_identity(user_ctx: UserContext) -> str:
    user_id = str(getattr(user_ctx, "user_id", 0) or 0)
    account_slug = _resolve_user_ctx_log_slug(user_ctx)
    _MAIN_ACCOUNT_SLUG_REGISTRY[user_id] = account_slug
    return account_slug


def _infer_main_log_category(level: int, module: str, event: str) -> str:
    if level >= logging.WARNING:
        return "warning"
    text = f"{module}:{event}".lower()
    if any(token in text for token in ("start", "login", "release", "check", "health")):
        return "runtime"
    return "business"


class _MainLogDefaultsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "custom_module"):
            record.custom_module = "main"
        if not hasattr(record, "event"):
            record.event = "general"
        if not hasattr(record, "data"):
            record.data = ""
        if not hasattr(record, "user_id"):
            record.user_id = "0"
        if not hasattr(record, "category"):
            record.category = _infer_main_log_category(record.levelno, str(record.custom_module), str(record.event))
        if not hasattr(record, "account_slug"):
            fallback_slug = f"user-{record.user_id}" if str(record.user_id) != "0" else "unknown"
            record.account_slug = _sanitize_account_slug("", fallback=fallback_slug)
        if not hasattr(record, "account_tag"):
            record.account_tag = f"【ydx-{record.account_slug}】"
        return True


_main_log_filter = _MainLogDefaultsFilter()

file_handler = TimedRotatingFileHandler('numai.log', when='midnight', interval=1, backupCount=ACCOUNT_LOG_BACKUP_DAYS, encoding='utf-8')
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] [%(custom_module)s:%(event)s] %(message)s | %(data)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
file_handler.setLevel(logging.DEBUG)
file_handler.addFilter(_main_log_filter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] %(message)s | %(data)s',
    datefmt='%H:%M:%S'
))
console_handler.setLevel(logging.INFO)
console_handler.addFilter(_main_log_filter)
logger.addHandler(console_handler)


class _MainAccountCategoryRouterHandler(logging.Handler):
    def __init__(self, root_dir: str, backup_count: int = ACCOUNT_LOG_BACKUP_DAYS):
        super().__init__(level=logging.DEBUG)
        self.root_dir = root_dir
        self.backup_count = backup_count
        self._handlers: Dict[tuple, TimedRotatingFileHandler] = {}
        self._default_filter = _MainLogDefaultsFilter()
        self._formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] [%(custom_module)s:%(event)s] %(message)s | %(data)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    def _get_handler(self, account_slug: str, category: str) -> TimedRotatingFileHandler:
        key = (account_slug, category)
        if key in self._handlers:
            return self._handlers[key]
        account_dir = os.path.join(self.root_dir, account_slug)
        os.makedirs(account_dir, exist_ok=True)
        log_path = os.path.join(account_dir, f"{category}.log")
        handler = TimedRotatingFileHandler(
            log_path,
            when='midnight',
            interval=1,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        handler.setFormatter(self._formatter)
        handler.addFilter(self._default_filter)
        self._handlers[key] = handler
        return handler

    def emit(self, record: logging.LogRecord):
        try:
            self._default_filter.filter(record)
            account_slug = str(getattr(record, "account_slug", "unknown") or "unknown")
            if account_slug == "unknown":
                return
            category = str(getattr(record, "category", "runtime") or "runtime")
            handler = self._get_handler(account_slug, category)
            handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self):
        for handler in self._handlers.values():
            try:
                handler.close()
            except Exception:
                pass
        self._handlers.clear()
        super().close()


class _MainAccountIdentityFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        account_slug = str(getattr(record, "account_slug", "") or "").strip()
        if not account_slug:
            fallback_slug = f"user-{getattr(record, 'user_id', '0')}" if str(getattr(record, "user_id", "0")) != "0" else "unknown"
            account_slug = _sanitize_account_slug("", fallback=fallback_slug)
            record.account_slug = account_slug
        record.account_label = _build_account_label(account_slug)
        record.account_tag = f"【ydx-{account_slug}】"
        return True


account_category_handler = _MainAccountCategoryRouterHandler(MAIN_ACCOUNT_LOG_ROOT, backup_count=7)
account_category_handler.addFilter(_main_log_filter)
_main_account_identity_filter = _MainAccountIdentityFilter()
account_category_handler.addFilter(_main_account_identity_filter)
logger.addHandler(account_category_handler)
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(account_label)s | %(message)s',
    datefmt='%H:%M:%S'
))
console_handler.addFilter(_main_account_identity_filter)
try:
    logger.removeHandler(file_handler)
    file_handler.close()
except Exception:
    pass


def log_event(level, module, event=None, message='', **kwargs):
    # 兼容3参数调用: log_event(level, module, event)
    if event is None:
        event = module
        module = 'main'
        message = ''
    elif not message and not kwargs:
        # log_event(level, module, event) - event作为message
        message = event
        event = module
        module = 'main'
    category = str(kwargs.pop("category", "")).strip().lower()
    account_name = str(kwargs.pop("account_name", "")).strip()
    user_id = str(kwargs.get("user_id", 0))
    account_slug = str(kwargs.pop("account_slug", "")).strip()
    if not account_slug:
        account_slug = _MAIN_ACCOUNT_SLUG_REGISTRY.get(user_id, "")
    if not account_slug:
        account_slug = _sanitize_account_slug(account_name, fallback=(f"user-{user_id}" if user_id not in {"", "0"} else "unknown"))
    if category not in {"runtime", "warning", "business"}:
        category = _infer_main_log_category(level, str(module), str(event))
    data = ', '.join(f'{k}={v}' for k, v in kwargs.items())
    logger.log(
        level,
        message,
        extra={
            'custom_module': module,
            'event': event,
            'data': data,
            'user_id': user_id,
            'category': category,
            'account_slug': account_slug,
            'account_tag': f"【ydx-{account_slug}】",
        },
    )


async def create_client(user_ctx: UserContext, global_config: dict) -> TelegramClient:
    session_path = os.path.join(
        user_ctx.user_dir, 
        user_ctx.config.telegram.get("session_name", "session")
    )
    
    client = TelegramClient(
        session_path,
        user_ctx.config.telegram.get("api_id"),
        user_ctx.config.telegram.get("api_hash")
    )
    return client


def _get_session_path(user_ctx: UserContext) -> str:
    return os.path.join(
        user_ctx.user_dir,
        user_ctx.config.telegram.get("session_name", "session")
    )


def _is_session_lock_conflict(error: OSError) -> bool:
    return error.errno in (errno.EACCES, errno.EAGAIN) or getattr(error, "winerror", None) in {32, 33}


def _lock_session_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return

    if msvcrt is None:
        raise OSError(errno.ENOSYS, "session file locking is unavailable")

    if os.lseek(fd, 0, os.SEEK_END) == 0:
        os.write(fd, b"0")
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _unlock_session_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return

    if msvcrt is None:
        return

    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _acquire_session_lock(user_ctx: UserContext) -> bool:
    """
    为每个账号的 Telethon session 增加进程级文件锁，避免多个进程同时写同一个 .session。
    这类并发写入会触发 sqlite3.OperationalError: database is locked。
    """
    session_path = _get_session_path(user_ctx)
    lock_path = f"{session_path}.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        _lock_session_fd(fd)
        setattr(user_ctx, "_session_lock_fd", fd)
        setattr(user_ctx, "_session_lock_path", lock_path)
        return True
    except OSError as e:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if _is_session_lock_conflict(e):
            log_event(
                logging.ERROR,
                'start',
                '账号session已被其他进程占用',
                user_id=user_ctx.user_id,
                session=session_path,
                lock=lock_path,
            )
            return False
        raise


def _release_session_lock(user_ctx: UserContext):
    fd = getattr(user_ctx, "_session_lock_fd", None)
    if fd is None:
        return
    try:
        _unlock_session_fd(fd)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass
    setattr(user_ctx, "_session_lock_fd", None)


def _get_admin_console_cfg(user_ctx: UserContext) -> Dict[str, Any]:
    return user_ctx.config.admin_console if isinstance(getattr(user_ctx.config, "admin_console", None), dict) else {}


def _get_admin_console_mode(user_ctx: UserContext) -> str:
    return str(_get_admin_console_cfg(user_ctx).get("mode", "") or "").strip()


def _resolve_admin_telegram_id_chat(user_ctx: UserContext):
    cfg = _get_admin_console_cfg(user_ctx).get("telegram_id", {})
    if not isinstance(cfg, dict):
        return None
    target = cfg.get("chat_id")
    return _normalize_target(target)


def _resolve_admin_chat(user_ctx: UserContext):
    """兼容旧内部调用：仅返回 telegram_id 管理入口。"""
    return _resolve_admin_telegram_id_chat(user_ctx)


def _get_admin_telegram_bot_cfg(user_ctx: UserContext) -> Dict[str, Any]:
    cfg = _get_admin_console_cfg(user_ctx).get("telegram_bot", {})
    return cfg if isinstance(cfg, dict) else {}


def _get_admin_bot_allowed_sender_ids(user_ctx: UserContext) -> set:
    cfg = _get_admin_telegram_bot_cfg(user_ctx)
    raw = cfg.get("allowed_sender_ids", [])
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    result = set()
    for item in items:
        normalized = _normalize_target(item)
        if normalized not in (None, ""):
            result.add(str(normalized))
    return result


def _normalize_bot_parse_mode(parse_mode: str | None) -> str | None:
    text = str(parse_mode or "").strip().lower()
    if text == "html":
        return "HTML"
    return None


def _build_admin_bot_commands() -> List[Dict[str, str]]:
    return [
        {"command": "help", "description": "查看帮助"},
        {"command": "status", "description": "查看状态"},
        {"command": "pause", "description": "暂停押注"},
        {"command": "resume", "description": "恢复押注"},
        {"command": "balance", "description": "刷新余额"},
        {"command": "yss", "description": "查看预设"},
    ]


def _render_bot_text_payload(text: str, parse_mode: str | None) -> tuple[str, str | None]:
    mode = str(parse_mode or "").strip().lower()
    raw = str(text or "")
    if mode == "html":
        return raw, "HTML"
    if mode != "markdown":
        return raw, None

    import html
    import re

    placeholders: List[tuple[str, str]] = []

    def _store(value: str) -> str:
        token = f"__BOT_FMT_{len(placeholders)}__"
        placeholders.append((token, value))
        return token

    def _replace_pre(match):
        body = match.group(1)
        return _store(f"<pre>{html.escape(body)}</pre>")

    protected = re.sub(r"```([\s\S]*?)```", _replace_pre, raw)
    escaped = html.escape(protected)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", lambda m: f"<b>{m.group(1)}</b>", escaped)
    escaped = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{m.group(1)}</code>", escaped)

    for token, value in placeholders:
        escaped = escaped.replace(html.escape(token), value)
        escaped = escaped.replace(token, value)
    return escaped, "HTML"


async def _bot_api_request(bot_token: str, method: str, *, payload: Dict[str, Any] | None = None, timeout: int = 30) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    response = await asyncio.to_thread(requests.post, url, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok", False):
        raise RuntimeError(str(data))
    return data


async def _ensure_admin_bot_menu(user_ctx: UserContext) -> None:
    bot_cfg = _get_admin_telegram_bot_cfg(user_ctx)
    bot_token = str(bot_cfg.get("bot_token", "") or "").strip()
    chat_id = _normalize_target(bot_cfg.get("chat_id"))
    if not bot_token or chat_id in (None, ""):
        return
    commands = _build_admin_bot_commands()
    try:
        await _bot_api_request(
            bot_token,
            "setMyCommands",
            payload={
                "commands": commands,
                "scope": {"type": "chat", "chat_id": chat_id},
            },
            timeout=10,
        )
        await _bot_api_request(
            bot_token,
            "setChatMenuButton",
            payload={
                "chat_id": chat_id,
                "menu_button": {"type": "commands"},
            },
            timeout=10,
        )
    except Exception as e:
        log_event(logging.ERROR, 'admin_bot', '管理员 Bot 菜单初始化失败', user_id=user_ctx.user_id, error=str(e))


async def _send_admin_console_text(client, user_ctx: UserContext, text: str, parse_mode: str | None = None):
    mode = _get_admin_console_mode(user_ctx)
    if mode == "telegram_id":
        chat_id = _resolve_admin_telegram_id_chat(user_ctx)
        if not chat_id:
            return None
        return await client.send_message(chat_id, text, parse_mode=parse_mode)

    if mode == "telegram_bot":
        bot_cfg = _get_admin_telegram_bot_cfg(user_ctx)
        bot_token = str(bot_cfg.get("bot_token", "") or "").strip()
        chat_id = _normalize_target(bot_cfg.get("chat_id"))
        if not bot_token or chat_id in (None, ""):
            return None
        rendered_text, api_parse_mode = _render_bot_text_payload(text, parse_mode)
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": rendered_text,
        }
        if api_parse_mode:
            payload["parse_mode"] = api_parse_mode
        result = await _bot_api_request(bot_token, "sendMessage", payload=payload, timeout=10)
        msg = result.get("result", {}) if isinstance(result.get("result", {}), dict) else {}
        return SimpleNamespace(
            chat_id=chat_id,
            id=msg.get("message_id"),
            is_bot_api=True,
            bot_token=bot_token,
        )
    return None


async def _prime_admin_bot_offset(bot_token: str) -> int:
    result = await _bot_api_request(bot_token, "getUpdates", payload={"timeout": 0, "allowed_updates": ["message"]}, timeout=10)
    updates = result.get("result", []) if isinstance(result.get("result", []), list) else []
    if not updates:
        return 0
    latest = max(int(item.get("update_id", 0) or 0) for item in updates)
    return latest + 1


def _normalize_target(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text.lstrip("-").isdigit():
            try:
                return int(text)
            except Exception:
                return value
        return text
    return value


def _iter_targets(target: Any) -> List[Any]:
    if isinstance(target, (list, tuple, set)):
        result: List[Any] = []
        for item in target:
            normalized = _normalize_target(item)
            if normalized not in (None, ""):
                result.append(normalized)
        return result
    normalized = _normalize_target(target)
    if normalized in (None, ""):
        return []
    return [normalized]


def _get_user_event_lock(user_ctx: UserContext) -> asyncio.Lock:
    lock = getattr(user_ctx, "_event_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(user_ctx, "_event_lock", lock)
    return lock


async def _run_admin_console_bot_loop(client: TelegramClient, user_ctx: UserContext, global_config: dict):
    bot_cfg = _get_admin_telegram_bot_cfg(user_ctx)
    bot_token = str(bot_cfg.get("bot_token", "") or "").strip()
    chat_id = _normalize_target(bot_cfg.get("chat_id"))
    allowed_senders = _get_admin_bot_allowed_sender_ids(user_ctx)
    if not bot_token or chat_id in (None, ""):
        return

    try:
        offset = await _prime_admin_bot_offset(bot_token)
    except Exception as e:
        log_event(logging.ERROR, 'admin_bot', '管理员 Bot 初始化失败', user_id=user_ctx.user_id, error=str(e))
        return

    setattr(user_ctx, "_admin_bot_offset", offset)

    while getattr(user_ctx, "client", None) is client:
        try:
            payload = {
                "timeout": 20,
                "allowed_updates": ["message"],
            }
            if offset > 0:
                payload["offset"] = offset
            result = await _bot_api_request(bot_token, "getUpdates", payload=payload, timeout=30)
            updates = result.get("result", []) if isinstance(result.get("result", []), list) else []
            for update in updates:
                update_id = int(update.get("update_id", 0) or 0)
                if update_id > 0:
                    offset = update_id + 1
                message = update.get("message", {}) if isinstance(update.get("message", {}), dict) else {}
                if not message:
                    continue
                message_chat = message.get("chat", {}) if isinstance(message.get("chat", {}), dict) else {}
                message_chat_id = _normalize_target(message_chat.get("id"))
                if str(message_chat_id) != str(chat_id):
                    continue
                if str(message_chat.get("type", "") or "") != "private":
                    continue
                sender_id = _normalize_target((message.get("from", {}) or {}).get("id"))
                if allowed_senders and str(sender_id) not in allowed_senders:
                    continue
                text = str(message.get("text", "") or "").strip()
                if not text:
                    continue
                if text == "/start":
                    text = "/help"
                event = SimpleNamespace(
                    raw_text=text,
                    chat_id=message_chat_id,
                    id=update_id,
                    sender_id=sender_id,
                )
                async with _get_user_event_lock(user_ctx):
                    from zq_multiuser import process_user_command as zq_user
                    await zq_user(client, event, user_ctx, global_config)
        except Exception as e:
            log_event(logging.ERROR, 'admin_bot', '管理员 Bot 轮询异常', user_id=user_ctx.user_id, error=str(e))
            await asyncio.sleep(3)


def _normalize_ai_keys(ai_cfg: Any) -> List[str]:
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


def _looks_like_ai_key_issue(error_text: str) -> bool:
    text = str(error_text or "").lower()
    if not text:
        return False
    return any(sig in text for sig in ("401", "unauthorized", "authentication", "invalid api key", "invalid token", "forbidden"))


def _get_allowed_sender_ids(user_ctx: UserContext) -> set:
    """
    可选命令发送者白名单（默认关闭，保持兼容）。
    支持 notification.allowed_sender_ids / allowed_senders / admins。
    """
    notification = user_ctx.config.notification if isinstance(user_ctx.config.notification, dict) else {}
    raw = (
        notification.get("allowed_sender_ids")
        or notification.get("allowed_senders")
        or notification.get("admins")
    )
    if not raw:
        return set()

    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    result = set()
    for item in items:
        normalized = _normalize_target(item)
        if normalized in (None, ""):
            continue
        result.add(str(normalized))
    return result


def register_handlers(client: TelegramClient, user_ctx: UserContext, global_config: dict):
    config = user_ctx.config
    state = user_ctx.state
    presets = user_ctx.presets
    button_mapping = global_config.get("button_mapping", {})
    admin_chat = _resolve_admin_telegram_id_chat(user_ctx) if _get_admin_console_mode(user_ctx) == "telegram_id" else None
    zq_group_targets = _iter_targets(config.groups.get("zq_group", []))
    zq_bot_targets = _iter_targets(config.groups.get("zq_bot"))
    
    @client.on(events.NewMessage(
        chats=zq_group_targets,
        pattern=r"\[近 40 次结果\]\[由近及远\]\[0 小 1 大\].*",
        from_users=zq_bot_targets
    ))
    async def bet_on_handler(event):
        log_event(logging.DEBUG, 'bet_on', '收到押注触发消息', 
                  user_id=user_ctx.user_id, msg_id=event.id)
        async with _get_user_event_lock(user_ctx):
            await zq_bet_on(client, event, user_ctx, global_config)
    
    @client.on(events.NewMessage(
        chats=zq_group_targets,
        # 修复：多用户分支 - 结算正则字符类误写会匹配到 `|`，导致异常消息也被当作结算。
        pattern=r"已结算: 结果为 (\d+) (大|小)",
        from_users=zq_bot_targets
    ))
    async def settle_handler(event):
        log_event(logging.DEBUG, 'settle', '收到结算消息',
                  user_id=user_ctx.user_id, msg_id=event.id)
        async with _get_user_event_lock(user_ctx):
            await zq_settle(client, event, user_ctx, global_config)

    @client.on(events.NewMessage(
        chats=zq_group_targets,
        from_users=zq_bot_targets
    ))
    async def red_packet_handler(event):
        await zq_red_packet(client, event, user_ctx, global_config)
    
    if admin_chat:
        @client.on(events.NewMessage(chats=admin_chat))
        async def user_handler(event):
            raw_text = (event.raw_text or "").strip()
            safe_cmd = raw_text[:50]
            lower_text = raw_text.lower()
            if lower_text.startswith("apikey ") or lower_text.startswith("/apikey "):
                safe_cmd = "apikey ***"
            log_event(logging.DEBUG, 'user_cmd', '收到用户命令',
                      user_id=user_ctx.user_id, cmd=safe_cmd)
            async with _get_user_event_lock(user_ctx):
                await zq_user(client, event, user_ctx, global_config)


async def zq_bet_on(client, event, user_ctx: UserContext, global_config: dict):
    from zq_multiuser import process_bet_on
    await process_bet_on(client, event, user_ctx, global_config)


async def zq_settle(client, event, user_ctx: UserContext, global_config: dict):
    from zq_multiuser import process_settle
    await process_settle(client, event, user_ctx, global_config)


async def zq_user(client, event, user_ctx: UserContext, global_config: dict):
    from zq_multiuser import process_user_command
    await process_user_command(client, event, user_ctx, global_config)


async def zq_red_packet(client, event, user_ctx: UserContext, global_config: dict):
    from zq_multiuser import process_red_packet
    await process_red_packet(client, event, user_ctx, global_config)


async def check_models_for_user(client, user_ctx: UserContext):
    try:
        user_model_mgr = user_ctx.get_model_manager()
        user_model_mgr.load_models()
        models = user_model_mgr.list_models()
        
        report = f"🚀 **Bot 启动模型自检报告**\n\n"
        report += f"👤 **用户**: {user_ctx.config.name}\n\n"
        
        total_models = sum(len(ms) for ms in models.values())
        success_count = 0
        failure_errors: List[str] = []
        
        for provider, ms in models.items():
            report += f"📁 **{provider.upper()}**\n"
            for m in ms:
                mid = m['model_id']
                if not m.get('enabled', True):
                    report += f"⚪ `{mid}`: 已禁用\n"
                    continue
                
                res = await user_model_mgr.validate_model(mid)
                if res['success']:
                    status = "✅ 正常"
                    latency = res.get('latency', 'N/A')
                    success_count += 1
                else:
                    status = f"❌ 失败"
                    latency = "-"
                    failure_errors.append(str(res.get("error", "")))
                
                report += f"{status} `{mid}` ({latency}ms)\n"
            report += "\n"
        
        report += f"📊 **汇总**: {success_count}/{total_models} 可用\n"
        report += f"🤖 **当前默认**: `{user_ctx.get_runtime('current_model_id', 'qwen3-coder-plus')}`"
        
        await _send_admin_console_text(client, user_ctx, report)

        ai_cfg = user_ctx.config.ai if isinstance(user_ctx.config.ai, dict) else {}
        has_keys = bool(_normalize_ai_keys(ai_cfg))
        key_issue_detected = (not has_keys) or any(_looks_like_ai_key_issue(err) for err in failure_errors)
        if key_issue_detected:
            warn = (
                "⚠️ 大模型AI key 失效/缺失，请更新 key！！！\n"
                "请在管理员窗口执行：`apikey set <新key>`"
            )
            await _send_admin_console_text(client, user_ctx, warn)
        log_event(logging.INFO, 'model_check', '模型自检完成', user_id=user_ctx.user_id)
        
    except Exception as e:
        log_event(logging.ERROR, 'model_check', '模型自检失败', 
                  user_id=user_ctx.user_id, error=str(e))


async def fetch_account_balance(user_ctx: UserContext) -> int:
    import aiohttp
    
    zhuque = user_ctx.config.zhuque
    cookie = zhuque.get("cookie", "")
    csrf_token = zhuque.get("csrf_token", "") or zhuque.get("x_csrf", "")
    api_url = zhuque.get("api_url", "https://zhuque.in/api/user/getInfo?")
    
    if not cookie or not csrf_token:
        log_event(logging.ERROR, 'balance', '缺少朱雀配置', user_id=user_ctx.user_id)
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
                    log_event(logging.INFO, 'balance', '获取余额成功',
                              user_id=user_ctx.user_id, balance=balance)
                    return balance
                else:
                    user_ctx.set_runtime("balance_status", "network_error")
                    log_event(logging.ERROR, 'balance', '获取余额失败',
                              user_id=user_ctx.user_id, status=response.status)
                    return user_ctx.get_runtime("account_balance", 0)
    except Exception as e:
        user_ctx.set_runtime("balance_status", "network_error")
        log_event(logging.ERROR, 'balance', '获取余额异常',
                  user_id=user_ctx.user_id, error=str(e))
        return user_ctx.get_runtime("account_balance", 0)


def _apply_startup_balance_snapshot(user_ctx: UserContext, balance: int) -> int:
    user_ctx.set_runtime("account_balance", int(balance))
    try:
        gambling_fund = int(user_ctx.get_runtime("gambling_fund", 0) or 0)
    except (TypeError, ValueError):
        gambling_fund = 0
    user_ctx.set_runtime("gambling_fund", gambling_fund)
    return gambling_fund


async def start_user(user_ctx: UserContext, global_config: dict):
    lock_acquired = False
    try:
        register_main_user_log_identity(user_ctx)
        try:
            from zq_multiuser import register_user_log_identity
            register_user_log_identity(user_ctx)
        except Exception as e:
            log_event(
                logging.WARNING,
                'start',
                '注册业务日志账号标识失败',
                user_id=user_ctx.user_id,
                error=str(e),
                category='warning',
            )

        zq_group_targets = _iter_targets(user_ctx.config.groups.get("zq_group", []))
        zq_bot_targets = _iter_targets(user_ctx.config.groups.get("zq_bot"))
        admin_mode = _get_admin_console_mode(user_ctx)

        # 启动前校验，避免“进程运行但账号无命令/无结算”的静默失败。
        if not zq_group_targets or not zq_bot_targets:
            log_event(
                logging.ERROR,
                'start',
                '用户启动失败：缺少必要监听配置',
                user_id=user_ctx.user_id,
                zq_group=zq_group_targets,
                zq_bot=zq_bot_targets,
            )
            return None

        lock_acquired = _acquire_session_lock(user_ctx)
        if not lock_acquired:
            return None

        client = await create_client(user_ctx, global_config)
        user_ctx.client = client
        
        await client.connect()
        
        if not await client.is_user_authorized():
            log_event(logging.WARNING, 'start', '用户未授权，开始登录流程',
                      user_id=user_ctx.user_id)
            if not sys.stdin.isatty():
                log_event(
                    logging.ERROR,
                    'start',
                    '非交互环境无法执行登录，请先在交互终端完成账号授权',
                    user_id=user_ctx.user_id,
                    session=user_ctx.config.telegram.get("session_name", ""),
                )
                _release_session_lock(user_ctx)
                return None
            print(f"\n🔐 用户 {user_ctx.config.name} 需要登录 Telegram")
            print(f"   请按照提示输入手机号和验证码...\n")
            try:
                await client.start()
                log_event(logging.INFO, 'start', '登录成功',
                          user_id=user_ctx.user_id)
                print(f"✅ 用户 {user_ctx.config.name} 登录成功！\n")
            except Exception as e:
                log_event(logging.ERROR, 'start', '登录失败',
                          user_id=user_ctx.user_id, error=str(e))
                print(f"❌ 登录失败: {e}")
                _release_session_lock(user_ctx)
                return None
        
        register_handlers(client, user_ctx, global_config)
        
        # await check_models_for_user(client, user_ctx)
        
        balance = await fetch_account_balance(user_ctx)
        # 启动时只刷新账户余额，保留手动维护的博彩资金。
        gambling_fund = _apply_startup_balance_snapshot(user_ctx, balance)
        log_event(
            logging.INFO,
            'start',
            '启动余额快照已刷新（菠菜资金保持独立）',
            user_id=user_ctx.user_id,
            account_balance=balance,
            gambling_fund=gambling_fund,
        )

        # 启动恢复时只保留挂单自愈。
        from zq_multiuser import heal_stale_pending_bets, send_message_v2, _build_ops_card, get_software_version_text
        heal_result = heal_stale_pending_bets(user_ctx)

        user_ctx.save_state()

        healed_count = int(heal_result.get("count", 0) or 0)
        if healed_count > 0:
            healed_preview = ", ".join(heal_result.get("items", [])[:5])
            if len(heal_result.get("items", [])) > 5:
                healed_preview += " ..."
            log_event(
                logging.WARNING,
                'start',
                '检测到历史未结算挂单并已自愈',
                user_id=user_ctx.user_id,
                count=healed_count,
                items=healed_preview,
            )
            mes = (
                "🩹 挂单自愈已执行\n"
                f"检测到历史异常挂单：{healed_count} 笔（result=None）\n"
                "处理方式：已自动标记为“异常未结算”（不再参与胜率/连输统计）\n"
                f"样例：{healed_preview}"
            )
            try:
                await _send_admin_console_text(client, user_ctx, mes)
            except Exception as e:
                log_event(
                    logging.ERROR,
                    'start',
                    '挂单自愈通知发送失败',
                    user_id=user_ctx.user_id,
                    error=str(e),
                )

        log_event(logging.INFO, 'start', '用户启动成功',
                  user_id=user_ctx.user_id, name=user_ctx.config.name, balance=balance)

        startup_msg = _build_ops_card(
            "✅ 脚本启动成功",
            summary="当前账号已完成启动并开始监听。",
            fields=[
                ("版本", get_software_version_text()),
                ("账户余额", f"{max(0, int(balance or 0)) / 10000:.2f} 万"),
                ("菠菜资金", f"{max(0, int(gambling_fund or 0)) / 10000:.2f} 万"),
            ],
        )
        try:
            await send_message_v2(
                client,
                "startup_ready",
                startup_msg,
                user_ctx,
                global_config,
            )
        except Exception as e:
            log_event(
                logging.ERROR,
                'start',
                '启动成功通知发送失败',
                user_id=user_ctx.user_id,
                error=str(e),
            )

        if admin_mode == "telegram_bot":
            await _ensure_admin_bot_menu(user_ctx)

        if admin_mode == "telegram_bot":
            existing_task = getattr(user_ctx, "_admin_console_task", None)
            if existing_task is None or existing_task.done():
                user_ctx._admin_console_task = asyncio.create_task(
                    _run_admin_console_bot_loop(client, user_ctx, global_config)
                )
        
        return client
        
    except Exception as e:
        log_event(logging.ERROR, 'start', '用户启动失败',
                  user_id=user_ctx.user_id, error=str(e))
        if lock_acquired:
            _release_session_lock(user_ctx)
        return None


async def main():
    print("=" * 50)
    print("多用户 Telegram Bot 启动中...")
    print("=" * 50)
    
    user_manager = UserManager()
    user_count = user_manager.load_all_users()
    
    if user_count == 0:
        print("❌ 未找到任何用户配置！")
        print("请在 users/ 目录下创建用户配置文件。")
        print("参考 users/_template/ 目录中的模板文件。")
        return
    
    print(f"✅ 已加载 {user_count} 个用户配置")
    log_event(logging.INFO, 'main', '加载用户配置', count=user_count)
    
    clients = []
    tasks = []
    
    for user_id, user_ctx in user_manager.get_all_users().items():
        print(f"🔄 正在启动用户: {user_ctx.config.name} (ID: {user_id})...")
        client = await start_user(user_ctx, user_manager.global_config)
        
        if client:
            clients.append(client)
            tasks.append(client.run_until_disconnected())
            print(f"✅ 用户 {user_ctx.config.name} 启动成功")
        else:
            print(f"❌ 用户 {user_ctx.config.name} 启动失败")
    
    if not clients:
        print("❌ 没有成功启动任何用户，程序退出")
        return
    
    print("=" * 50)
    print(f"🚀 所有用户已启动，共 {len(clients)} 个客户端运行中")
    print("=" * 50)
    log_event(logging.INFO, 'main', '所有用户启动完成', count=len(clients))

    async def notify_release(message: str):
        for user_ctx in user_manager.get_all_users().values():
            if not user_ctx.client:
                continue
            try:
                await _send_admin_console_text(user_ctx.client, user_ctx, message)
            except Exception as e:
                log_event(
                    logging.ERROR,
                    'release_check',
                    '发布通知发送失败',
                    user_id=user_ctx.user_id,
                    error=str(e),
                )

    asyncio.create_task(periodic_release_check_loop(notify_release))
    
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for user_ctx in user_manager.get_all_users().values():
            user_ctx.save_state()
            _release_session_lock(user_ctx)
    
    log_event(logging.INFO, 'main', '程序正常退出')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 脚本已手动终止")
        log_event(logging.INFO, 'main', 'stop', message='脚本被用户手动终止')
    except Exception as e:
        log_event(logging.ERROR, 'main', 'error', message='启动失败', error=str(e))
