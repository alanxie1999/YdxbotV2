import logging
import aiohttp
import asyncio
import time
import hashlib
from collections import deque
from typing import Dict, List, Any, Optional

# 尝试导入 DashScope SDK
try:
    import dashscope
    from dashscope import Generation
    from http import HTTPStatus
    HAS_DASHSCOPE = True
except ImportError:
    HAS_DASHSCOPE = False

logger = logging.getLogger('model_manager')

_OPENAI_COMPATIBLE_RATE_LIMIT_WINDOWS: Dict[str, deque] = {}
_OPENAI_COMPATIBLE_RATE_LIMIT_LOCKS: Dict[str, asyncio.Lock] = {}


def _normalize_base_url(base_url: Any) -> str:
    return str(base_url or "").rstrip("/")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _infer_openai_compatible_provider(provider: Any, base_url: Any) -> str:
    explicit = str(provider or "").strip().lower()
    if explicit:
        return explicit

    normalized = _normalize_base_url(base_url).lower()
    if "integrate.api.nvidia.com" in normalized:
        return "nvidia"
    if "apis.iflow.cn" in normalized:
        return "iflow"
    return "openai_compatible"


def _provider_display_name(provider: Any) -> str:
    provider_name = str(provider or "").strip().lower()
    mapping = {
        "nvidia": "NVIDIA",
        "iflow": "iFlow",
        "openai_compatible": "OpenAI Compatible",
        "openai-compatible": "OpenAI Compatible",
    }
    return mapping.get(provider_name, provider_name or "OpenAI Compatible")


async def _acquire_openai_compatible_rate_limit(
    provider: str,
    base_url: str,
    api_key: str,
    rpm: int,
):
    if rpm <= 0:
        return

    key_hash = hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[:12]
    bucket = f"{provider}:{_normalize_base_url(base_url)}:{key_hash}"
    lock = _OPENAI_COMPATIBLE_RATE_LIMIT_LOCKS.setdefault(bucket, asyncio.Lock())

    async with lock:
        now = time.monotonic()
        window = _OPENAI_COMPATIBLE_RATE_LIMIT_WINDOWS.setdefault(bucket, deque())
        while window and now - window[0] >= 60:
            window.popleft()

        if len(window) >= rpm:
            wait_sec = max(0.05, 60 - (now - window[0]) + 0.05)
            logger.warning(
                f"{_provider_display_name(provider)} 达到速率上限，等待 {wait_sec:.1f}s 后继续"
            )
            await asyncio.sleep(wait_sec)
            now = time.monotonic()
            while window and now - window[0] >= 60:
                window.popleft()

        window.append(now)

class ModelManager:
    def __init__(self):
        self.models: List[Dict[str, Any]] = []
        self.api_key_indices = {}  # 用于轮询 API Key
        self.shared_ai_config: Dict[str, Any] = {}
        self.fallback_chain: List[str] = []
        self.load_models_from_config()

    def apply_shared_config(self, global_config: Dict[str, Any]):
        """接收通用配置（config/global.json）的 AI 配置并生效。"""
        cfg = global_config or {}
        ai_cfg = cfg.get("ai") or cfg.get("iflow") or {}
        self.shared_ai_config = ai_cfg if isinstance(ai_cfg, dict) else {}
        self.load_models_from_config()

    def load_models_from_config(self):
        """从共享 AI 配置加载并标准化模型配置。"""
        self.models = []
        self.api_key_indices = {}
        self.fallback_chain = []
        try:
            ai_cfg = self.shared_ai_config if isinstance(self.shared_ai_config, dict) else {}
            iflow_enabled = ai_cfg.get('enabled', True)
            api_key = ai_cfg.get('api_keys', ai_cfg.get('api_key', ""))
            base_url = ai_cfg.get('base_url', "https://apis.iflow.cn/v1")
            iflow_models = ai_cfg.get('models', {})
            configured_chain = ai_cfg.get('fallback_chain', [])
            provider = ai_cfg.get('provider', "")
            timeout_sec = ai_cfg.get('timeout', 30)
            rate_limit_rpm = ai_cfg.get('rate_limit_rpm')

            if iflow_enabled:
                if not isinstance(iflow_models, dict):
                    iflow_models = {}
                resolved_provider = _infer_openai_compatible_provider(provider, base_url)
                resolved_rate_limit_rpm = _safe_int(rate_limit_rpm, 0)
                if resolved_provider == "nvidia" and resolved_rate_limit_rpm <= 0:
                    resolved_rate_limit_rpm = 40

                for idx, info in iflow_models.items():
                    if not isinstance(info, dict):
                        continue
                    actual_model_id = info.get("model_id", idx)
                    self.models.append({
                        "provider": resolved_provider,
                        "model_id": actual_model_id,
                        "name": info.get("name", actual_model_id),
                        "api_key": api_key,
                        "base_url": base_url,
                        "timeout": _safe_int(timeout_sec, 30),
                        "rate_limit_rpm": resolved_rate_limit_rpm,
                        "max_tokens": info.get("max_tokens", 8192),
                        "enabled": info.get("enabled", True),
                        "idx": str(idx)
                    })

                if isinstance(configured_chain, list) and configured_chain:
                    self.fallback_chain = [str(x) for x in configured_chain]
                else:
                    self.fallback_chain = [str(x) for x in iflow_models.keys()]
            
            logger.info(f"成功从 config 加载 {len(self.models)} 个模型配置")
            
        except Exception as e:
            logger.error(f"加载模型配置失败: {e}")
            self.models = []

    def load_models(self):
        """兼容旧接口，重新加载配置"""
        self.load_models_from_config()

    def get_model(self, model_id: str) -> Optional[Dict[str, Any]]:
        """获取指定模型配置，支持真实 model_id 或配置序号(idx)。"""
        target = str(model_id)
        for model in self.models:
            if model.get('model_id') == target:
                return model
            idx = model.get('idx')
            # 修复：降级链编号无法解析的问题，原因：配置中常使用“1/2/3”序号而非真实 model_id。
            if idx is not None and str(idx) == target:
                return model
        return None

    def list_models(self) -> Dict[str, List[Dict[str, Any]]]:
        """按厂商分组列出模型"""
        grouped = {}
        for model in self.models:
            provider = model.get('provider', 'unknown')
            if provider not in grouped:
                grouped[provider] = []
            grouped[provider].append(model)
        return grouped

    def get_api_key(self, model_config: Dict[str, Any]) -> str:
        """获取 API Key，支持轮询"""
        api_keys = model_config.get('api_key')
        if not api_keys:
            return ""
        
        if isinstance(api_keys, list):
            model_id = model_config['model_id']
            idx = self.api_key_indices.get(model_id, 0)
            key = api_keys[idx % len(api_keys)]
            self.api_key_indices[model_id] = (idx + 1) % len(api_keys)
            return key
        return str(api_keys)

    async def call_model(self, model_id: str, messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        """
        统一模型调用接口，支持自动降级
        返回格式: {"success": bool, "content": str, "error": str, "usage": dict}
        """
        # 获取降级链
        fallback_chain = [str(x) for x in (self.fallback_chain or [])]
        target_model_id = str(model_id)
        requested_model = self.get_model(target_model_id)
        requested_actual_model_id = (
            str(requested_model.get('model_id'))
            if requested_model and requested_model.get('model_id')
            else target_model_id
        )
        
        # 确定尝试顺序
        try_models = []
        
        # 检查传入的 model_id 是否是配置的key（如"1"）
        if target_model_id in fallback_chain:
            # 是配置的key，按降级链处理
            start_idx = fallback_chain.index(target_model_id)
            try_models = fallback_chain[start_idx:]
        else:
            # 可能是真实的模型ID（如"qwen/qwen3-next..."）。
            if requested_model is None and fallback_chain:
                # 运行态里残留了旧 provider 的模型名时，直接回退到当前链路的第一个模型。
                try_models = fallback_chain[:]
            else:
                # 先尝试直接调用
                try_models = [target_model_id]
                
                # 如果该模型在降级链中，添加链中后续的模型作为备选
                for idx_key in fallback_chain:
                    model_cfg = self.get_model(idx_key)
                    if model_cfg and str(model_cfg.get('model_id')) == target_model_id:
                        # 找到了对应的配置key，添加链中后续的模型
                        start_idx = fallback_chain.index(idx_key) + 1
                        try_models.extend(fallback_chain[start_idx:])
                        break
            
        # 记录所有尝试的错误
        errors = []
        
        for current_id in try_models:
            model_config = self.get_model(current_id)
            if not model_config:
                errors.append(f"{current_id}: 模型不存在")
                continue
            
            if not model_config.get('enabled', True):
                errors.append(f"{current_id}: 模型已禁用")
                continue

            provider = model_config.get('provider')
            resolved_model_id = str(model_config.get('model_id', current_id))
            resolved_model_key = str(model_config.get('idx', current_id))
            logger.info(f"正在尝试调用模型: {current_id} ({provider})")
            
            try:
                result = None
                if provider == 'aliyun':
                    result = await self._call_aliyun(model_config, messages, **kwargs)
                elif provider == 'google':
                    result = await self._call_google(model_config, messages, **kwargs)
                elif provider in {'iflow', 'nvidia', 'openai_compatible', 'openai-compatible'}:
                    result = await self._call_iflow(model_config, messages, **kwargs)
                elif provider == 'siliconflow':
                    result = await self._call_siliconflow(model_config, messages, **kwargs)
                else:
                    result = {"success": False, "error": f"不支持的厂商: {provider}", "content": ""}
                
                if result['success']:
                    if resolved_model_id != requested_actual_model_id:
                        logger.warning(f"模型 {target_model_id} 调用失败，已降级并成功使用 {current_id}")
                    result['model_id'] = resolved_model_id
                    result['requested_model_id'] = requested_actual_model_id
                    result['resolved_model_key'] = resolved_model_key
                    result['fallback_used'] = resolved_model_id != requested_actual_model_id
                    return result
                else:
                    error_msg = f"{current_id} 调用失败: {result['error']}"
                    logger.warning(error_msg)
                    errors.append(error_msg)
                    
            except Exception as e:
                error_msg = f"{current_id} 发生异常: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)
        
        # 所有尝试都失败
        return {"success": False, "error": " | ".join(errors), "content": ""}

    async def _call_iflow(self, config: Dict[str, Any], messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        """OpenAI 兼容 API 调用适配（iFlow / NVIDIA / 其他兼容端点）"""
        api_key = self.get_api_key(config)
        provider = _infer_openai_compatible_provider(config.get('provider'), config.get('base_url'))
        provider_label = _provider_display_name(provider)
        base_url = _normalize_base_url(config.get('base_url') or 'https://apis.iflow.cn/v1')
        model_id = config['model_id']
        timeout_sec = max(1, _safe_int(config.get('timeout', 30), 30))
        rate_limit_rpm = _safe_int(config.get('rate_limit_rpm', 0), 0)
        max_retries = max(1, _safe_int(config.get('max_retries', 1), 1))
        if provider == "nvidia" and rate_limit_rpm <= 0:
            rate_limit_rpm = 40
        
        url = f"{base_url}/chat/completions"
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        openai_messages = []
        for msg in messages:
            role = msg['role']
            if role == 'model': role = 'assistant'
            openai_messages.append({
                "role": role,
                "content": msg['content']
            })

        payload = {
            "model": model_id,
            "messages": openai_messages,
            "temperature": kwargs.get('temperature', 0.7),
            "max_tokens": kwargs.get('max_tokens', 4096),
            "stream": False
        }

        await _acquire_openai_compatible_rate_limit(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            rpm=rate_limit_rpm,
        )

        async with aiohttp.ClientSession() as session:
            for attempt in range(1, max_retries + 1):
                try:
                    async with session.post(url, headers=headers, json=payload, timeout=timeout_sec) as response:
                        if response.status != 200:
                            text = await response.text()
                            error_text = f"{provider_label} API Error {response.status}: {text}"
                            if response.status in {408, 409, 425, 429, 449, 500, 502, 503, 504} and attempt < max_retries:
                                await asyncio.sleep(min(2 ** (attempt - 1), 4))
                                continue
                            return {"success": False, "error": error_text, "content": ""}
                        
                        data = await response.json()

                        if isinstance(data, dict) and data.get("error"):
                            return {"success": False, "error": f"{provider_label} API Error: {data.get('error')}", "content": ""}

                        # 修复：兼容多种响应结构，原因：部分模型不会返回标准 message.content 字符串。
                        # 兼容 OpenAI 风格 message.content (str/list)
                        choices = data.get("choices", []) if isinstance(data, dict) else []
                        if isinstance(choices, list) and choices:
                            choice = choices[0] if isinstance(choices[0], dict) else {}
                            message = choice.get("message", {}) if isinstance(choice, dict) else {}
                            content = message.get("content") if isinstance(message, dict) else None

                            if isinstance(content, str) and content.strip():
                                return {"success": True, "content": content, "error": ""}

                            if isinstance(content, list):
                                parts = []
                                for item in content:
                                    if isinstance(item, str):
                                        parts.append(item)
                                    elif isinstance(item, dict):
                                        text = item.get("text") or item.get("content")
                                        if text:
                                            parts.append(str(text))
                                merged = "".join(parts).strip()
                                if merged:
                                    return {"success": True, "content": merged, "error": ""}

                            # 某些模型可能只返回 reasoning_content
                            reasoning_content = message.get("reasoning_content") if isinstance(message, dict) else None
                            if isinstance(reasoning_content, str) and reasoning_content.strip():
                                return {"success": True, "content": reasoning_content, "error": ""}

                            # 兼容极少数模型返回 choices[0].text
                            choice_text = choice.get("text") if isinstance(choice, dict) else None
                            if isinstance(choice_text, str) and choice_text.strip():
                                return {"success": True, "content": choice_text, "error": ""}

                        # 兼容 output_text 结构
                        if isinstance(data, dict):
                            output_text = data.get("output_text")
                            if isinstance(output_text, str) and output_text.strip():
                                return {"success": True, "content": output_text, "error": ""}
                            output = data.get("output")
                            if isinstance(output, dict):
                                text = output.get("text")
                                if isinstance(text, str) and text.strip():
                                    return {"success": True, "content": text, "error": ""}

                        return {
                            "success": False,
                            "error": f"{provider_label} Response Parse Error: {str(data)[:500]}",
                            "content": ""
                        }
                except asyncio.TimeoutError:
                    if attempt < max_retries:
                        await asyncio.sleep(min(2 ** (attempt - 1), 4))
                        continue
                    return {"success": False, "error": f"{provider_label} API Timeout", "content": ""}
                except Exception as e:
                    if attempt < max_retries:
                        await asyncio.sleep(min(2 ** (attempt - 1), 4))
                        continue
                    return {"success": False, "error": f"{provider_label} Request Error: {str(e)}", "content": ""}


    async def _call_aliyun(self, config: Dict[str, Any], messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        """阿里云 API 调用适配"""
        if not HAS_DASHSCOPE:
            return {"success": False, "error": "DashScope SDK 未安装", "content": ""}

        api_key = self.get_api_key(config)
        dashscope.api_key = api_key
        
        try:
            resp = Generation.call(
                model=config['model_id'],
                messages=messages,
                result_format='message',
                temperature=kwargs.get('temperature', 0.3)
            )
            
            if resp.status_code == HTTPStatus.OK:
                content = resp.output.choices[0].message.content
                return {"success": True, "content": content, "error": ""}
            else:
                return {"success": False, "error": f"Aliyun API Error: {resp.message}", "content": ""}
        except Exception as e:
            return {"success": False, "error": f"Aliyun Exception: {str(e)}", "content": ""}

    async def _call_google(self, config: Dict[str, Any], messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        """Google Gemini API 调用适配"""
        api_key = self.get_api_key(config)
        base_url = config.get('base_url') or 'https://generativelanguage.googleapis.com/v1beta'
        model_id = config['model_id']
        
        url = f"{base_url}/models/{model_id}:generateContent?key={api_key}"
        
        # 转换消息格式
        contents = []
        for msg in messages:
            role = "user" if msg['role'] == 'user' else "model"
            if msg['role'] == 'system':
                # 为兼容性，暂将 system prompt 作为第一条 user 消息
                role = "user"
                content = f"[System Instruction]\n{msg['content']}"
            else:
                content = msg['content']
            
            contents.append({
                "role": role,
                "parts": [{"text": content}]
            })

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": kwargs.get('temperature', 0.3),
                "maxOutputTokens": kwargs.get('max_tokens', 8192)
            }
        }

        # 配置代理 (仅 Google 使用)
        proxy = None
        if getattr(config, 'proxy_enabled', False) and getattr(config, 'proxy', None):
            p = config.proxy
            # 简单构造 HTTP 代理 URL，假设本地代理同时支持 HTTP
            # 如果是 socks5，aiohttp 需要 aiohttp-socks 库，这里先尝试 HTTP
            proxy = f"http://{p.get('host', '127.0.0.1')}:{p.get('port', 7890)}"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, timeout=30, proxy=proxy) as response:
                    if response.status != 200:
                        text = await response.text()
                        return {"success": False, "error": f"Google API Error {response.status}: {text}", "content": ""}
                    
                    data = await response.json()
                    try:
                        content = data['candidates'][0]['content']['parts'][0]['text']
                        return {"success": True, "content": content, "error": ""}
                    except (KeyError, IndexError):
                        return {"success": False, "error": f"Google Response Parse Error: {data}", "content": ""}
            except asyncio.TimeoutError:
                return {"success": False, "error": "Google API Timeout", "content": ""}
            except Exception as e:
                return {"success": False, "error": f"Google Connection Error: {str(e)}", "content": ""}

    async def validate_model(self, model_id: str) -> Dict[str, Any]:
        """验证模型可用性，返回详细信息"""
        test_message = [{"role": "user", "content": "Hello, verify connection."}]
        start_time = time.time()
        result = await self.call_model(model_id, test_message, temperature=0.1, max_tokens=10)
        duration = (time.time() - start_time) * 1000
        result['latency'] = f"{duration:.0f}"
        return result

# 全局单例
model_manager = ModelManager()
