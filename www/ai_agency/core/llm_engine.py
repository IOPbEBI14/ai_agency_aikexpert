"""Мульти-провайдерный LLM-движок.

Провайдеры: openai, grok, anthropic, deepseek, yandexgpt, gigachat.
Активный провайдер переключается через API/дашборд (в памяти процесса).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from core.config import Config

logger = logging.getLogger("LLMEngine")


def _try_fix_truncated_json(content: str) -> str:
    """Локальная копия — без импорта utils (циклическая зависимость)."""
    if not content or not content.strip().startswith("{"):
        return ""
    content = content.strip()
    open_braces = content.count("{") - content.count("}")
    open_brackets = content.count("[") - content.count("]")
    fixed = content + "]" * open_brackets + "}" * open_braces
    try:
        import json as _json
        _json.loads(fixed)
        return fixed
    except Exception:
        last_comma = fixed.rfind(",")
        if last_comma > 0:
            fixed2 = fixed[:last_comma] + "}" * open_braces
            try:
                import json as _json
                _json.loads(fixed2)
                return fixed2
            except Exception:
                pass
        return ""

_lock = threading.RLock()
_active_provider_id: Optional[str] = None
_gigachat_token: Optional[str] = None
_gigachat_token_expires: float = 0.0


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    api_style: str  # openai_chat | yandex_responses | anthropic | gigachat
    default_base_url: str
    default_model: str
    env_key: str
    env_base: str
    env_model: str


PROVIDERS: Dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        id="openai",
        label="OpenAI",
        api_style="openai_chat",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        env_key="OPENAI_API_KEY",
        env_base="OPENAI_BASE_URL",
        env_model="OPENAI_MODEL",
    ),
    "grok": ProviderSpec(
        id="grok",
        label="Grok (xAI)",
        api_style="openai_chat",
        default_base_url="https://api.x.ai/v1",
        default_model="grok-2-latest",
        env_key="GROK_API_KEY",
        env_base="GROK_BASE_URL",
        env_model="GROK_MODEL",
    ),
    "anthropic": ProviderSpec(
        id="anthropic",
        label="Anthropic",
        api_style="anthropic",
        default_base_url="https://api.anthropic.com",
        default_model="claude-sonnet-4-20250514",
        env_key="ANTHROPIC_API_KEY",
        env_base="ANTHROPIC_BASE_URL",
        env_model="ANTHROPIC_MODEL",
    ),
    "deepseek": ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        api_style="openai_chat",
        default_base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        env_key="DEEPSEEK_API_KEY",
        env_base="DEEPSEEK_BASE_URL",
        env_model="DEEPSEEK_MODEL",
    ),
    "yandexgpt": ProviderSpec(
        id="yandexgpt",
        label="YandexGPT",
        api_style="yandex_responses",
        default_base_url="https://ai.api.cloud.yandex.net/v1",
        default_model="yandexgpt",
        env_key="YANDEX_API_KEY",  # fallback → LLM_API_KEY
        env_base="YANDEX_BASE_URL",
        env_model="YANDEX_MODEL",
    ),
    "gigachat": ProviderSpec(
        id="gigachat",
        label="GigaChat",
        api_style="gigachat",
        default_base_url="https://gigachat.devices.sberbank.ru/api/v1",
        default_model="GigaChat",
        env_key="GIGACHAT_CREDENTIALS",
        env_base="GIGACHAT_BASE_URL",
        env_model="GIGACHAT_MODEL",
    ),
}

PROVIDER_ORDER = (
    "openai", "grok", "anthropic", "deepseek", "yandexgpt", "gigachat",
)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _provider_api_key(spec: ProviderSpec) -> str:
    key = _env(spec.env_key)
    if key:
        return key
    # Обратная совместимость: общие LLM_* → yandexgpt; также DEEPSEEK может быть в LLM_API_KEY
    if spec.id == "yandexgpt":
        return (Config.LLM_API_KEY or "").strip()
    if spec.id == "deepseek" and (Config.LLM_MODEL or "").lower().startswith("deepseek"):
        return (Config.LLM_API_KEY or "").strip()
    if spec.id == "openai":
        return _env("OPENAI_API_KEY") or _env("LLM_API_KEY")
    return ""


def _provider_base_url(spec: ProviderSpec) -> str:
    url = _env(spec.env_base)
    if url:
        return url.rstrip("/")
    if spec.id == "yandexgpt":
        return (Config.LLM_BASE_URL or spec.default_base_url).rstrip("/")
    return spec.default_base_url.rstrip("/")


def _provider_model(spec: ProviderSpec) -> str:
    model = _env(spec.env_model)
    if model:
        return model
    if spec.id == "yandexgpt":
        return Config.LLM_MODEL or spec.default_model
    return spec.default_model


def get_active_provider_id() -> str:
    global _active_provider_id
    with _lock:
        if _active_provider_id and _active_provider_id in PROVIDERS:
            return _active_provider_id
        configured = (_env("LLM_PROVIDER") or "yandexgpt").lower()
        if configured not in PROVIDERS:
            configured = "yandexgpt"
        _active_provider_id = configured
        return _active_provider_id


def set_active_provider(provider_id: str) -> Dict[str, Any]:
    """Переключает активный провайдер. Ключ должен быть задан в .env."""
    pid = (provider_id or "").strip().lower()
    if pid not in PROVIDERS:
        raise ValueError(
            f"Неизвестный провайдер: {provider_id}. "
            f"Доступны: {', '.join(PROVIDER_ORDER)}"
        )
    spec = PROVIDERS[pid]
    if not _provider_api_key(spec):
        raise ValueError(
            f"Провайдер {spec.label} не настроен: задайте {spec.env_key} в .env"
            + (" (или LLM_API_KEY для YandexGPT)" if pid == "yandexgpt" else "")
        )
    if pid == "yandexgpt" and not (Config.LLM_FOLDER_ID or _env("YANDEX_FOLDER_ID")):
        raise ValueError("Для YandexGPT нужен LLM_FOLDER_ID (или YANDEX_FOLDER_ID) в .env")

    global _active_provider_id
    with _lock:
        _active_provider_id = pid
    logger.info("🔀 Активный LLM-провайдер: %s (%s)", spec.label, _provider_model(spec))
    return get_llm_status()


def list_providers() -> List[Dict[str, Any]]:
    active = get_active_provider_id()
    items = []
    for pid in PROVIDER_ORDER:
        spec = PROVIDERS[pid]
        key = _provider_api_key(spec)
        configured = bool(key)
        if pid == "yandexgpt":
            configured = configured and bool(
                Config.LLM_FOLDER_ID or _env("YANDEX_FOLDER_ID")
            )
        items.append({
            "id": pid,
            "label": spec.label,
            "model": _provider_model(spec),
            "configured": configured,
            "active": pid == active,
            "api_style": spec.api_style,
        })
    return items


def get_llm_status() -> Dict[str, Any]:
    active = get_active_provider_id()
    spec = PROVIDERS[active]
    return {
        "provider": active,
        "label": spec.label,
        "model": _provider_model(spec),
        "configured": bool(_provider_api_key(spec)),
        "providers": list_providers(),
    }


def _extract_openai_content(data: Dict[str, Any]) -> Tuple[str, int]:
    content = ""
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
    usage = data.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    if tokens == 0:
        tokens = int(usage.get("prompt_tokens") or 0) + int(
            usage.get("completion_tokens") or 0
        )
    return content, tokens


def _extract_yandex_content(data: Dict[str, Any]) -> Tuple[str, int]:
    content = ""
    if "output_text" in data:
        content = data["output_text"] or ""
    elif "output" in data:
        for item in data.get("output") or []:
            if item.get("type") == "message" and "content" in item:
                for block in item["content"]:
                    if block.get("type") == "output_text":
                        content += block.get("text", "")
    usage = data.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    if tokens == 0:
        tokens = int(usage.get("input_tokens") or 0) + int(
            usage.get("output_tokens") or 0
        )
    return content, tokens


def _extract_anthropic_content(data: Dict[str, Any]) -> Tuple[str, int]:
    parts = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    usage = data.get("usage") or {}
    tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    return "".join(parts), tokens


def _gigachat_access_token(credentials: str) -> str:
    """OAuth GigaChat. credentials — Authorization key (Basic …) или rq-UID:secret."""
    global _gigachat_token, _gigachat_token_expires
    with _lock:
        if _gigachat_token and time.time() < _gigachat_token_expires - 60:
            return _gigachat_token

    oauth_url = _env(
        "GIGACHAT_OAUTH_URL",
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
    )
    scope = _env("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    verify = _env("GIGACHAT_VERIFY_SSL", "true").lower() in ("1", "true", "yes", "on")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": _env("GIGACHAT_RQUID", "ai-agency-os"),
    }
    # Уже готовый Basic-токен или сырой client_id:secret
    if credentials.lower().startswith("basic "):
        headers["Authorization"] = credentials
    else:
        headers["Authorization"] = f"Basic {credentials}"

    resp = requests.post(
        oauth_url,
        headers=headers,
        data={"scope": scope},
        timeout=60,
        verify=verify,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GigaChat OAuth {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    token = data.get("access_token") or ""
    expires_at = float(data.get("expires_at") or 0) / 1000.0
    if not expires_at:
        expires_at = time.time() + int(data.get("expires_in") or 1800)
    if not token:
        raise RuntimeError("GigaChat OAuth: нет access_token")

    with _lock:
        _gigachat_token = token
        _gigachat_token_expires = expires_at
    return token


def _call_openai_chat(
    spec: ProviderSpec,
    system_prompt: str,
    user_task: str,
    *,
    max_tokens: int,
    bearer: Optional[str] = None,
    verify: bool = True,
) -> Tuple[str, int, Dict[str, Any]]:
    base = _provider_base_url(spec)
    url = f"{base}/chat/completions"
    key = bearer or _provider_api_key(spec)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": _provider_model(spec),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=180, verify=verify)
    if resp.status_code != 200:
        raise RuntimeError(
            f"❌ LLM ({spec.label}) статус {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    content, tokens = _extract_openai_content(data)
    return content, tokens, payload


def _call_yandex(
    spec: ProviderSpec,
    system_prompt: str,
    user_task: str,
    *,
    max_tokens: int,
) -> Tuple[str, int, Dict[str, Any]]:
    base = _provider_base_url(spec)
    url = f"{base}/responses"
    folder = _env("YANDEX_FOLDER_ID") or (Config.LLM_FOLDER_ID or "")
    model = _provider_model(spec)
    model_uri = model if model.startswith("gpt://") else f"gpt://{folder}/{model}"
    headers = {
        "Authorization": f"Bearer {_provider_api_key(spec)}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_uri,
        "instructions": system_prompt,
        "input": [{"role": "user", "content": user_task}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(
            f"❌ LLM ({spec.label}) статус {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    content, tokens = _extract_yandex_content(data)
    return content, tokens, payload


def _call_anthropic(
    spec: ProviderSpec,
    system_prompt: str,
    user_task: str,
    *,
    max_tokens: int,
) -> Tuple[str, int, Dict[str, Any]]:
    base = _provider_base_url(spec)
    url = f"{base.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": _provider_api_key(spec),
        "anthropic-version": _env("ANTHROPIC_VERSION", "2023-06-01"),
        "Content-Type": "application/json",
    }
    payload = {
        "model": _provider_model(spec),
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_task}],
        "temperature": 0.3,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(
            f"❌ LLM ({spec.label}) статус {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    content, tokens = _extract_anthropic_content(data)
    return content, tokens, {"max_tokens": max_tokens}


def _call_gigachat(
    spec: ProviderSpec,
    system_prompt: str,
    user_task: str,
    *,
    max_tokens: int,
) -> Tuple[str, int, Dict[str, Any]]:
    creds = _provider_api_key(spec)
    token = _gigachat_access_token(creds)
    verify = _env("GIGACHAT_VERIFY_SSL", "true").lower() in ("1", "true", "yes", "on")
    return _call_openai_chat(
        spec, system_prompt, user_task,
        max_tokens=max_tokens, bearer=token, verify=verify,
    )


def invoke(
    agent_name: str,
    system_prompt: str,
    user_task: str,
    max_retries: int = 2,
) -> Tuple[str, int]:
    """Вызов активного LLM-провайдера с retry при обрезке JSON."""
    pid = get_active_provider_id()
    spec = PROVIDERS[pid]
    max_tokens = 16000

    for attempt in range(max_retries + 1):
        try:
            logger.info(
                "🤖 %s → %s/%s (попытка %s/%s)",
                agent_name, spec.label, _provider_model(spec),
                attempt + 1, max_retries + 1,
            )
            if spec.api_style == "openai_chat":
                content, tokens, payload = _call_openai_chat(
                    spec, system_prompt, user_task, max_tokens=max_tokens
                )
            elif spec.api_style == "yandex_responses":
                content, tokens, payload = _call_yandex(
                    spec, system_prompt, user_task, max_tokens=max_tokens
                )
            elif spec.api_style == "anthropic":
                content, tokens, payload = _call_anthropic(
                    spec, system_prompt, user_task, max_tokens=max_tokens
                )
            elif spec.api_style == "gigachat":
                content, tokens, payload = _call_gigachat(
                    spec, system_prompt, user_task, max_tokens=max_tokens
                )
            else:
                raise RuntimeError(f"Неизвестный api_style: {spec.api_style}")

            content_stripped = (content or "").strip()
            if content_stripped.startswith("{") and not content_stripped.endswith("}"):
                logger.warning("⚠️ Ответ %s обрезан. Попытка %s", agent_name, attempt + 1)
                if attempt < max_retries:
                    max_tokens = min(max_tokens * 2, 32000)
                    continue
                content = _try_fix_truncated_json(content)
                if not content:
                    raise ValueError("Ответ обрезан и не может быть восстановлен")

            logger.info("✅ %s ответил (%s). Токенов: %s", agent_name, spec.label, tokens)
            return content, tokens

        except requests.exceptions.Timeout:
            logger.error("⏱️ Таймаут LLM (%s) для %s", spec.label, agent_name)
            if attempt < max_retries:
                continue
            raise
        except RuntimeError:
            if attempt < max_retries:
                continue
            raise
        except Exception as e:
            logger.error("❌ Ошибка LLM (%s): %s", spec.label, e)
            if attempt < max_retries:
                continue
            raise

    raise RuntimeError(f"Не удалось получить ответ от {agent_name} ({spec.label})")
