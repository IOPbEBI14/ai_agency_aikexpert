"""
Трёхуровневый валидатор n8n workflow (+ smoke schemaDelta).

Уровень A — Heuristic (Python, всегда):
  Структурные проверки + Direction K (устойчивость критичных HTTP/NocoDB).

Уровень B — Local JS (``validate-n8n.js``, без npm):
  Быстрый structural-gate, синхронизирован с heuristic.

Уровень C — Official engine (``n8n-workflow-validator``):
  Реальный движок n8n (`n8n-workflow` + `n8n-nodes-base`) — то же, что редактор
  при импорте. Ищется в node_modules / global / ``npx --yes``.
  Конфиг: ``N8N_VALIDATOR_OFFICIAL=auto|on|off`` (для релизов/CI — ``on``).

Smoke (поверх C): ``schemaDelta.missingKeys`` / N8N_PARAMETER ERROR → is_valid=False
  даже если CLI вернул exit 0 с «предупреждениями».

Опционально (будущее / instance): ``N8N_MCP_URL`` + ``N8N_MCP_ACCESS_TOKEN`` —
  официальный n8n Builder MCP (`validate_workflow` для SDK/TS кода). Для JSON
  от developer основным внешним шагом максимальной точности остаётся Layer C.

Правило: любой ERROR от A/B/C/smoke → ``is_valid=False`` (developer получает qa_feedback).

Публичный API:
  validate_n8n_workflow(workflow)  → (is_valid: bool, issues: list[str])
  build_n8n_feedback(issues)       → str
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("N8NValidator")

# ─── Константы ────────────────────────────────────────────────────────────────

_LOCAL_TIMEOUT = 30
_ENV_DISABLE_LOCAL = "N8N_VALIDATOR_RUNTIME"  # off → не вызывать validate-n8n.js
_ENV_RESILIENCE = "N8N_VALIDATOR_RESILIENCE"  # off → без Direction K ERROR
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Мутирующие HTTP-методы — требуют retry + error-ветку (Direction K)
_MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MUTATING_NOCODB_OPS = frozenset({"create", "update", "delete"})
_IDEMPOTENCY_MARKERS = (
    "external_id",
    "idempotency",
    "idempotency_key",
    "dedup",
    "unique_key",
)

# Кэш discovery (сбрасывается в тестах через monkeypatch)
_local_bin: Optional[str] = None
_local_checked: bool = False
_official_cmd: Optional[List[str]] = None
_official_checked: bool = False

_TRIGGER_TYPES = frozenset({
    "scheduleTrigger", "webhook", "emailTrigger", "manualTrigger",
    "mqttTrigger", "amqpTrigger", "kafkaTrigger", "n8nTrigger", "errorTrigger",
})

_SCHEDULE_INTERVAL_HINT = (
    'Поле "rule.interval" у scheduleTrigger должно быть МАССИВОМ объектов, '
    'например: "rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}. '
    'Число или объект вместо массива вызывает ошибку импорта "is not iterable".'
)

_KNOWN_CRED_KEYS: Dict[str, List[str]] = {
    "nocoDb":       ["nocoDbApiToken", "nocoDbApi"],
    "telegram":     ["telegramApi"],
    "httpRequest":  ["httpBasicAuth", "httpHeaderAuth", "httpDigestAuth",
                     "oAuth1Api", "oAuth2Api", "httpCustomAuth"],
    "gmail":        ["gmailOAuth2"],
    "slack":        ["slackOAuth2Api", "slackApi"],
}


def _official_mode() -> str:
    # os.environ первым — тесты/override без перезагрузки Config
    mode = os.getenv("N8N_VALIDATOR_OFFICIAL", "").strip().lower()
    if not mode:
        try:
            from core.config import Config
            mode = (Config.N8N_VALIDATOR_OFFICIAL or "auto").strip().lower()
        except Exception:
            mode = "auto"
    return mode if mode in ("auto", "on", "off") else "auto"


def _official_timeout() -> int:
    raw = os.getenv("N8N_VALIDATOR_OFFICIAL_TIMEOUT", "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        from core.config import Config
        return int(Config.N8N_VALIDATOR_OFFICIAL_TIMEOUT)
    except Exception:
        return 120


def _resilience_enabled() -> bool:
    raw = os.getenv(_ENV_RESILIENCE, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    try:
        from core.config import Config
        val = str(getattr(Config, "N8N_VALIDATOR_RESILIENCE", "on") or "on").strip().lower()
        return val not in ("0", "false", "no", "off")
    except Exception:
        return True


def _is_blocking_smoke_issue(text: str) -> bool:
    """schemaDelta / parameter ERROR — не пропускаем как «warning»."""
    low = text.lower()
    if "missing=" in low or "missingkeys" in low:
        return True
    if "n8n_parameter_validation_error" in low:
        return True
    if "[error]" in low and (
        "schema" in low or "parameter" in low or "could not find" in low
    ):
        return True
    return False


def _http_method(node: Dict[str, Any]) -> str:
    params = node.get("parameters") if isinstance(node.get("parameters"), dict) else {}
    method = params.get("method") or params.get("requestMethod") or "GET"
    return str(method).strip().upper() or "GET"


def _nocodb_operation(node: Dict[str, Any]) -> str:
    params = node.get("parameters") if isinstance(node.get("parameters"), dict) else {}
    return str(params.get("operation") or "").strip().lower()


def _is_critical_side_effect_node(node: Dict[str, Any], short: str) -> bool:
    if short == "httpRequest":
        return _http_method(node) in _MUTATING_HTTP_METHODS
    if short == "nocoDb":
        return _nocodb_operation(node) in _MUTATING_NOCODB_OPS
    return False


def _node_blob(node: Dict[str, Any]) -> str:
    parts = [
        json.dumps(node.get("parameters") or {}, ensure_ascii=False),
        str(node.get("notes") or ""),
        str(node.get("name") or ""),
    ]
    return " ".join(parts).lower()


def _has_idempotency_marker(node: Dict[str, Any]) -> bool:
    blob = _node_blob(node)
    return any(m in blob for m in _IDEMPOTENCY_MARKERS)


def _has_bounded_retry(node: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    if not node.get("retryOnFail"):
        return False, "нет retryOnFail: true"
    max_tries = node.get("maxTries")
    if max_tries is None:
        return False, "retryOnFail без maxTries (нужен конечный лимит, напр. 3)"
    try:
        n = int(max_tries)
    except (TypeError, ValueError):
        return False, f"maxTries должен быть числом, сейчас {max_tries!r}"
    if n < 1 or n > 10:
        return False, f"maxTries={n} вне диапазона 1..10"
    wait = node.get("waitBetweenTries")
    if wait is None:
        return False, "нет waitBetweenTries (нужна пауза / backoff между попытками)"
    try:
        w = int(wait)
    except (TypeError, ValueError):
        return False, f"waitBetweenTries должен быть числом (мс), сейчас {wait!r}"
    if w <= 0:
        return False, "waitBetweenTries должен быть > 0"
    return True, None


def _connection_buckets_nonempty(outputs: Dict[str, Any], key: str) -> bool:
    buckets = outputs.get(key)
    if not isinstance(buckets, list):
        return False
    return any(isinstance(b, list) and len(b) > 0 for b in buckets)


def _has_error_fallback(
    node_name: str,
    node: Dict[str, Any],
    connections: Any,
) -> Tuple[bool, Optional[str]]:
    outs: Dict[str, Any] = {}
    if isinstance(connections, dict):
        raw = connections.get(node_name)
        if isinstance(raw, dict):
            outs = raw

    on_error = str(node.get("onError") or "").strip()
    continue_on_fail = bool(node.get("continueOnFail"))
    has_error_conn = _connection_buckets_nonempty(outs, "error")

    if on_error == "continueErrorOutput":
        if has_error_conn:
            return True, None
        return False, (
            'onError=continueErrorOutput, но в connections нет непустой ветки "error" '
            "(резерв после исчерпания попыток)"
        )
    if has_error_conn:
        return True, None
    if continue_on_fail and _connection_buckets_nonempty(outs, "main"):
        return True, None
    return False, (
        "нет резервной ветки: задайте onError=continueErrorOutput + connections.error "
        "или continueOnFail=true с исходящим main (лог/алерт/сохранение данных)"
    )


def _check_direction_k_resilience(
    node: Dict[str, Any],
    label: str,
    short: str,
    connections: Any,
    issues: List[str],
) -> None:
    """Direction K: retry/backoff/fallback (+ идемпотентность для create/POST)."""
    if not _is_critical_side_effect_node(node, short):
        return

    ok_retry, retry_reason = _has_bounded_retry(node)
    if not ok_retry:
        issues.append(
            f"[{label}] Direction K: критичная нода ({short}) — {retry_reason}. "
            f"Нужно: retryOnFail=true, maxTries=3, waitBetweenTries>=1000 (мс)."
        )

    name = node.get("name") or label
    ok_fb, fb_reason = _has_error_fallback(str(name), node, connections)
    if not ok_fb:
        issues.append(f"[{label}] Direction K: {fb_reason}.")

    needs_idem = False
    if short == "httpRequest" and _http_method(node) == "POST":
        needs_idem = True
    if short == "nocoDb" and _nocodb_operation(node) == "create":
        needs_idem = True
    if needs_idem and not _has_idempotency_marker(node):
        issues.append(
            f"[{label}] Direction K: create/POST без защиты от дублей. "
            f"Добавьте external_id / idempotency_key в body/notes "
            f"(или GET-before-create с уникальным ключом)."
        )

    if isinstance(connections, dict):
        outs = connections.get(name)
        if isinstance(outs, dict):
            for key in ("error", "main"):
                buckets = outs.get(key)
                if not isinstance(buckets, list):
                    continue
                for bucket in buckets:
                    if not isinstance(bucket, list):
                        continue
                    for link in bucket:
                        if isinstance(link, dict) and link.get("node") == name:
                            issues.append(
                                f"[{label}] Direction K: connections.{key} ведёт на ту же ноду "
                                f"— риск бесконечного цикла. Добавьте счётчик/Wait или Stop."
                            )


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER B: Local validate-n8n.js
# ═══════════════════════════════════════════════════════════════════════════════

def _find_local_binary() -> Optional[str]:
    """Локальный validate-n8n.js (без npm-зависимостей)."""
    global _local_bin, _local_checked
    if _local_checked:
        return _local_bin
    _local_checked = True

    if os.getenv(_ENV_DISABLE_LOCAL, "").lower() in ("off", "0", "false"):
        logger.info("ℹ️  Local JS-валидация отключена (N8N_VALIDATOR_RUNTIME=off)")
        return None

    local_script = os.path.join(_PROJECT_DIR, "validate-n8n.js")
    if os.path.isfile(local_script):
        _local_bin = local_script
        logger.info("✅ Local n8n-валидатор: validate-n8n.js")
        return _local_bin
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER C: Official n8n-workflow-validator (движок n8n)
# ═══════════════════════════════════════════════════════════════════════════════

def _find_official_cmd() -> Optional[List[str]]:
    """Команда для официального валидатора (кэш на процесс).

    Порядок:
      1. ./node_modules/.bin/n8n-workflow-validator
      2. global n8n-workflow-validator / n8n-validate
      3. npx --yes n8n-workflow-validator  (при mode=auto|on)
    """
    global _official_cmd, _official_checked
    if _official_checked:
        return _official_cmd
    _official_checked = True

    mode = _official_mode()
    if mode == "off":
        logger.info("ℹ️  Official n8n-validator отключён (N8N_VALIDATOR_OFFICIAL=off)")
        return None

    bin_name = "n8n-workflow-validator.cmd" if sys.platform == "win32" else "n8n-workflow-validator"
    local_bin = os.path.join(_PROJECT_DIR, "node_modules", ".bin", bin_name)
    if os.path.isfile(local_bin):
        _official_cmd = [local_bin]
        logger.info(f"✅ Official n8n-validator (local): {local_bin}")
        return _official_cmd

    for name in ("n8n-workflow-validator", "n8n-validate"):
        try:
            result = subprocess.run(
                [name, "--help"],
                capture_output=True, text=True, timeout=8,
                shell=(sys.platform == "win32"),
            )
            if result.returncode in (0, 1) and (
                "workflow" in (result.stdout + result.stderr).lower()
                or result.returncode == 0
            ):
                _official_cmd = [name]
                logger.info(f"✅ Official n8n-validator (global): {name}")
                return _official_cmd
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue

    # npx — внешний шаг без постоянной установки (первый запуск дольше)
    try:
        result = subprocess.run(
            ["npx", "--version"],
            capture_output=True, text=True, timeout=8,
            shell=(sys.platform == "win32"),
        )
        if result.returncode == 0:
            _official_cmd = ["npx", "--yes", "n8n-workflow-validator"]
            logger.info("✅ Official n8n-validator через npx --yes (первый запуск может занять время)")
            return _official_cmd
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    if mode == "on":
        logger.error(
            "❌ N8N_VALIDATOR_OFFICIAL=on, но n8n-workflow-validator недоступен. "
            "Установите: npm run install-validator  или  npm i -g n8n-workflow-validator"
        )
    else:
        logger.info(
            "ℹ️  Official n8n-validator недоступен — heuristic + local JS. "
            "Установка: npm run install-validator"
        )
    return None


def _parse_validator_output(stdout: str, stderr: str) -> List[str]:
    """Разбирает вывод local/official валидатора → список замечаний."""
    issues: List[str] = []
    text = stdout.strip()

    if text.startswith(("[", "{")):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and data.get("valid") is True and not (
                data.get("issues") or data.get("errors")
            ):
                return []
            items = data if isinstance(data, list) else (
                data.get("issues") or data.get("errors") or ([data] if data.get("message") else [])
            )
            for item in items:
                if isinstance(item, str):
                    issues.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                sev = str(item.get("severity", "error")).upper()
                code = item.get("code", "")
                msg = item.get("message") or item.get("what") or str(item)
                loc_obj = item.get("location") if isinstance(item.get("location"), dict) else {}
                node = (
                    item.get("node")
                    or item.get("nodeName")
                    or loc_obj.get("nodeName")
                    or ""
                )
                # Schema hints от official engine — критичны для LLM-фикса
                ctx = item.get("context") if isinstance(item.get("context"), dict) else {}
                extra = ""
                delta = ctx.get("schemaDelta") if isinstance(ctx.get("schemaDelta"), dict) else {}
                if delta.get("missingKeys"):
                    extra += f" missing={delta['missingKeys']}"
                if delta.get("extraKeys"):
                    extra += f" extra={delta['extraKeys']}"
                if ctx.get("n8nError") and ctx["n8nError"] not in msg:
                    extra += f" n8nError={ctx['n8nError']}"
                loc = f" [{node}]" if node else ""
                prefix = f"[{sev}]{loc}"
                body = f"{code}: {msg}" if code else msg
                issues.append(f"{prefix} {body}{extra}".strip())
            return issues
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    for line in (stdout + "\n" + stderr).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if any(
            m in lower
            for m in ("error", "invalid", "missing", "is not iterable",
                      "n8n_parameter", "deprecated", "unknown node", "warning")
        ):
            issues.append(stripped)
    return issues


def _run_cli_validator(
    cmd: Sequence[str],
    workflow: Dict[str, Any],
    *,
    timeout: int,
    label: str,
    json_flag: bool = True,
) -> Tuple[Optional[bool], List[str]]:
    """Общий запуск CLI-валидатора по временному JSON-файлу."""
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
            encoding="utf-8", prefix="n8n_wf_",
        ) as tf:
            json.dump(workflow, tf, ensure_ascii=False)
            tmp_path = tf.name

        full_cmd = list(cmd)
        if json_flag and "--json" not in full_cmd:
            # official: `tool --json file`; local js: `node script --json file`
            if full_cmd[0] == "node" and len(full_cmd) >= 2:
                full_cmd = [full_cmd[0], full_cmd[1], "--json", tmp_path]
            else:
                full_cmd = full_cmd + ["--json", tmp_path]
        else:
            full_cmd = full_cmd + [tmp_path]

        logger.info(f"🔍 {label}: {' '.join(full_cmd)}")
        result = subprocess.run(
            full_cmd,
            capture_output=True, text=True,
            timeout=timeout,
            shell=(sys.platform == "win32"),
            cwd=_PROJECT_DIR,
        )
        issues = _parse_validator_output(result.stdout, result.stderr)

        if result.returncode == 0 and not issues:
            logger.info(f"✅ {label}: workflow валиден")
            return True, []
        if result.returncode == 0 and issues:
            blocking = [i for i in issues if _is_blocking_smoke_issue(i)]
            if blocking:
                logger.warning(
                    f"❌ {label} smoke: schemaDelta/parameter ERROR "
                    f"({len(blocking)}), несмотря на exit 0"
                )
                return False, issues
            logger.warning(f"⚠️  {label}: предупреждения ({len(issues)})")
            return True, issues

        logger.warning(f"❌ {label}: {len(issues)} ошибок (exit {result.returncode})")
        return False, issues or [
            f"{label} завершился с кодом {result.returncode}. stderr: {result.stderr[:400]}"
        ]
    except subprocess.TimeoutExpired:
        logger.error(f"⏱️  {label}: таймаут {timeout}с")
        return None, []
    except Exception as exc:
        logger.error(f"❌ {label}: {exc}", exc_info=True)
        return None, []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def validate_n8n_workflow_local(
    workflow: Dict[str, Any],
) -> Tuple[Optional[bool], List[str]]:
    """Layer B: локальный validate-n8n.js."""
    binary = _find_local_binary()
    if binary is None:
        return None, []
    return _run_cli_validator(
        ["node", binary], workflow, timeout=_LOCAL_TIMEOUT, label="Local JS",
    )


def validate_n8n_workflow_official(
    workflow: Dict[str, Any],
) -> Tuple[Optional[bool], List[str]]:
    """Layer C: официальный n8n-workflow-validator (движок n8n)."""
    cmd = _find_official_cmd()
    if cmd is None:
        if _official_mode() == "on":
            return False, [
                "Official n8n-workflow-validator обязателен (N8N_VALIDATOR_OFFICIAL=on), "
                "но недоступен. Установите: npm run install-validator"
            ]
        return None, []
    return _run_cli_validator(
        cmd, workflow, timeout=_official_timeout(), label="Official n8n-engine",
    )


def validate_n8n_workflow_runtime(
    workflow: Dict[str, Any],
) -> Tuple[Optional[bool], List[str]]:
    """Обратная совместимость: official → local → None."""
    official_ok, official_issues = validate_n8n_workflow_official(workflow)
    if official_ok is not None:
        return official_ok, official_issues
    return validate_n8n_workflow_local(workflow)


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER A: Heuristic-валидация (Python-only)
# ═══════════════════════════════════════════════════════════════════════════════

def _node_label(node: Dict[str, Any], idx: int) -> str:
    return node.get("name") or node.get("id") or f"#{idx}"


def _short_type(node_type: str) -> str:
    return node_type.split(".")[-1] if node_type else ""


def _check_schedule_trigger(node: Dict[str, Any], label: str, issues: List[str]) -> None:
    params = node.get("parameters", {})
    rule = params.get("rule")
    if rule is None:
        return
    if not isinstance(rule, dict):
        issues.append(f'[{label}] scheduleTrigger: "rule" должен быть объектом. {_SCHEDULE_INTERVAL_HINT}')
        return
    interval = rule.get("interval")
    if interval is not None and not isinstance(interval, list):
        issues.append(
            f'[{label}] scheduleTrigger: "rule.interval" имеет тип '
            f'{type(interval).__name__}, а должен быть массивом. {_SCHEDULE_INTERVAL_HINT}'
        )


def _check_if_node(node: Dict[str, Any], label: str, issues: List[str]) -> None:
    params = node.get("parameters", {})
    conditions = params.get("conditions")
    type_version = float(node.get("typeVersion", 1) or 1)
    if conditions is None:
        issues.append(
            f'[{label}] if: отсутствует "parameters.conditions". '
            f'Без условий IF всегда false/ломается.'
        )
        return
    if not isinstance(conditions, dict):
        issues.append(f'[{label}] if: "conditions" должен быть объектом.')
        return
    if type_version >= 2:
        inner = conditions.get("conditions")
        if not isinstance(inner, list):
            issues.append(
                f'[{label}] if (typeVersion {type_version}): ожидается '
                f'"conditions.conditions" как МАССИВ. Обнаружена структура v1 '
                f'(conditions.string/number). Приведи к формату v2: '
                f'"conditions": {{"combinator":"and","conditions":[{{"leftValue":"...",...}}]}}.'
            )
            return
        if len(inner) == 0:
            issues.append(
                f'[{label}] if: "conditions.conditions" пуст — неработающий IF. '
                f'Добавь хотя бы одно условие с leftValue/operator/rightValue.'
            )
            return
        for i, cond in enumerate(inner):
            if not isinstance(cond, dict):
                issues.append(f'[{label}] if: conditions.conditions[{i}] должен быть объектом.')
                continue
            left = cond.get("leftValue")
            if left is None or (isinstance(left, str) and not left.strip()):
                issues.append(
                    f'[{label}] if: conditions.conditions[{i}] имеет пустой leftValue. '
                    f'Пример: "={{ $json.statusCode }}".'
                )
            if "operator" not in cond:
                issues.append(
                    f'[{label}] if: conditions.conditions[{i}] без operator. '
                    f'Нужно: "operator": {{"type":"number","operation":"equals"}}.'
                )


def _check_set_node(node: Dict[str, Any], label: str, issues: List[str]) -> None:
    params = node.get("parameters", {})
    type_version = float(node.get("typeVersion", 1) or 1)
    if type_version < 3:
        return
    assignments = params.get("assignments")
    if "values" in params and assignments is None:
        issues.append(
            f'[{label}] set (typeVersion {type_version}): используется устаревшее поле '
            f'"values". В v3+ нужен "assignments": {{"assignments": [{{"id":"<uuid>",'
            f'"name":"field","value":"={{...}}","type":"string"}}]}}.'
        )
    elif assignments is not None:
        if not isinstance(assignments, dict) or not isinstance(assignments.get("assignments"), list):
            issues.append(
                f'[{label}] set (typeVersion {type_version}): '
                f'"assignments.assignments" должен быть массивом.'
            )


def _check_http_request(node: Dict[str, Any], label: str, issues: List[str]) -> None:
    params = node.get("parameters", {})
    type_version = float(node.get("typeVersion", 1) or 1)
    if type_version >= 4:
        if "bodyContentType" in params or (
            "body" in params and isinstance(params.get("body"), dict)
        ):
            issues.append(
                f'[{label}] httpRequest (typeVersion {type_version}): параметры v3 '
                f'("body"/"bodyContentType"). В v4+ нужно: "sendBody":true, '
                f'"specifyBody":"json", "jsonBody":"=...{{...}}..." (строка-выражение).'
            )
    url = params.get("url")
    if url is None or (isinstance(url, str) and not url.strip()):
        issues.append(f'[{label}] httpRequest: отсутствует "parameters.url".')


def _check_webhook(node: Dict[str, Any], label: str, issues: List[str]) -> None:
    params = node.get("parameters", {})
    path = params.get("path")
    if path is None or (isinstance(path, str) and not path.strip()):
        issues.append(
            f'[{label}] webhook: отсутствует "parameters.path". '
            f'Без path webhook не регистрируется.'
        )


def _check_empty_options(node: Dict[str, Any], short_type: str, label: str, issues: List[str]) -> None:
    if short_type not in ("if", "switch"):
        return
    params = node.get("parameters", {})
    options = params.get("options")
    if isinstance(options, dict) and len(options) == 0:
        issues.append(
            f'[{label}] {short_type}: пустой "options":{{}} ломает импорт '
            f'("Could not find property option"). Удали ключ options.'
        )


def _check_nocodb_update(node: Dict[str, Any], label: str, issues: List[str]) -> None:
    params = node.get("parameters", {})
    if params.get("operation") != "update":
        return
    if isinstance(params.get("data"), dict):
        issues.append(
            f'[{label}] nocoDb update: поле "data":{{}} не сохраняет значения. '
            f'Используй "fieldsUi": {{"fieldValues": [{{"fieldName":"synced","fieldValue":"true"}}]}} '
            f'(typeVersion 2) или "updateFields": {{"fieldValues":[...]}} (typeVersion 1).'
        )
        return
    fields_ui = params.get("fieldsUi")
    update_fields = params.get("updateFields")
    has_fields = (
        isinstance(fields_ui, dict) and isinstance(fields_ui.get("fieldValues"), list)
    ) or (
        isinstance(update_fields, dict) and isinstance(update_fields.get("fieldValues"), list)
    )
    if not has_fields:
        issues.append(
            f'[{label}] nocoDb update: нет fieldsUi.fieldValues / updateFields.fieldValues. '
            f'Без fieldValues обновление ничего не запишет.'
        )


def _check_credentials_keys(node: Dict[str, Any], short_type: str, label: str, issues: List[str]) -> None:
    expected = _KNOWN_CRED_KEYS.get(short_type)
    if not expected:
        return
    creds = node.get("credentials")
    if not isinstance(creds, dict):
        return
    for key in creds:
        if key not in expected:
            issues.append(
                f'[{label}] credentials: ключ "{key}" недопустим для {short_type}. '
                f'Ожидается один из: {", ".join(expected)}. '
                f'Неверный ключ = "Cannot read properties of undefined" при выполнении.'
            )


def _check_connections(
    nodes: List[Any],
    connections: Any,
    node_names: set,
    issues: List[str],
) -> None:
    connected_sources: set = set()
    connected_targets: set = set()
    if_nodes = {
        n.get("name")
        for n in nodes
        if isinstance(n, dict) and _short_type(n.get("type") or "") == "if" and n.get("name")
    }

    if isinstance(connections, dict):
        for src, outputs in connections.items():
            if src not in node_names:
                issues.append(f'connections: источник "{src}" не найден в nodes.')
            connected_sources.add(src)
            if not isinstance(outputs, dict):
                issues.append(f'connections["{src}"] должен быть объектом.')
                continue
            for okey, buckets in outputs.items():
                if not isinstance(buckets, list):
                    issues.append(f'connections["{src}"].{okey} должен быть массивом массивов.')
                    continue
                if src in if_nodes and okey == "main" and len(buckets) < 2:
                    issues.append(
                        f'connections["{src}"]: IF должен иметь обе ветки в main '
                        f'(true=индекс 0 и false=индекс 1). Сейчас веток: {len(buckets)}. '
                        f'Пример: "main": [ [{{true}}], [{{false}}] ].'
                    )
                for bucket in buckets:
                    if not isinstance(bucket, list):
                        continue
                    for link in bucket:
                        if not isinstance(link, dict):
                            continue
                        if "inputIndex" in link:
                            issues.append(
                                f'connections["{src}"]: ключ "inputIndex" должен быть "index". '
                                f'n8n игнорирует "inputIndex" — соединение не создаётся.'
                            )
                        if "index" not in link and "inputIndex" not in link:
                            issues.append(
                                f'connections["{src}"] → "{link.get("node")}": отсутствует '
                                f'"index" (обычно 0). Без index соединение может не создаться.'
                            )
                        target = link.get("node")
                        if target:
                            if target not in node_names:
                                issues.append(
                                    f'connections["{src}"]: цель "{target}" не найдена в nodes.'
                                )
                            connected_targets.add(target)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = node.get("name")
        if not name:
            continue
        short = _short_type(node.get("type") or "")
        is_trigger = short in _TRIGGER_TYPES
        in_src = name in connected_sources
        in_tgt = name in connected_targets
        if is_trigger and not in_src:
            issues.append(
                f'[{name}] trigger-нода не подключена ни к одной следующей ноде. '
                f'Добавь её в "connections" как источник.'
            )
        elif not is_trigger and not in_src and not in_tgt:
            issues.append(
                f'[{name}] нода полностью изолирована — отсутствует в "connections" '
                f'ни как источник, ни как цель. Подключи её или удали из "nodes".'
            )


def validate_n8n_workflow_heuristic(workflow: Any) -> Tuple[bool, List[str]]:
    """Python-only структурная валидация (всегда обязательна)."""
    issues: List[str] = []

    if not isinstance(workflow, dict):
        return False, ['Корень workflow должен быть объектом с "nodes" и "connections".']

    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return False, ['Поле "nodes" отсутствует или не является массивом.']
    if not nodes:
        return False, ['Массив "nodes" пуст.']

    connections = workflow.get("connections")
    if connections is None:
        issues.append('Поле "connections" отсутствует — workflow не свяжет ноды при импорте.')
    elif not isinstance(connections, dict):
        issues.append('Поле "connections" должно быть объектом.')

    node_names: set = set()
    seen_names: Dict[str, int] = {}

    for idx, node in enumerate(nodes):
        label = _node_label(node, idx)
        if not isinstance(node, dict):
            issues.append(f"[{label}] нода должна быть объектом.")
            continue

        name = node.get("name")
        if name:
            seen_names[name] = seen_names.get(name, 0) + 1
            node_names.add(name)

        node_type = node.get("type", "")
        if not isinstance(node_type, str) or not node_type:
            issues.append(f'[{label}] отсутствует строковое поле "type".')
            node_type = ""
        elif not node_type.startswith(("n8n-nodes-base.", "n8n-nodes-", "@n8n/")):
            issues.append(
                f'[{label}] "type"="{node_type}" — некорректный префикс '
                f'(ожидается "n8n-nodes-base.<node>").'
            )

        if "typeVersion" not in node:
            issues.append(f'[{label}] отсутствует "typeVersion".')
        elif not isinstance(node["typeVersion"], (int, float)):
            issues.append(f'[{label}] "typeVersion" должен быть числом.')

        pos = node.get("position")
        if not isinstance(pos, list) or len(pos) != 2:
            issues.append(f'[{label}] "position" должен быть [x, y].')

        params = node.get("parameters")
        if params is None:
            issues.append(f'[{label}] отсутствует объект "parameters".')
            continue
        if not isinstance(params, dict):
            issues.append(f'[{label}] "parameters" должен быть объектом.')
            continue

        short = _short_type(node_type)
        if short == "scheduleTrigger":
            _check_schedule_trigger(node, label, issues)
        elif short == "if":
            _check_if_node(node, label, issues)
        elif short == "set":
            _check_set_node(node, label, issues)
        elif short == "httpRequest":
            _check_http_request(node, label, issues)
        elif short == "webhook":
            _check_webhook(node, label, issues)
        elif short == "nocoDb":
            _check_nocodb_update(node, label, issues)
        _check_empty_options(node, short, label, issues)
        _check_credentials_keys(node, short, label, issues)
        if _resilience_enabled():
            _check_direction_k_resilience(node, label, short, connections, issues)

    for name, count in seen_names.items():
        if count > 1:
            issues.append(
                f'Дублируется имя ноды "{name}" ({count} раз). '
                f'Имена в nodes должны быть уникальны — иначе connections ломаются.'
            )

    _check_connections(nodes, connections, node_names, issues)

    return (len(issues) == 0), issues


# ═══════════════════════════════════════════════════════════════════════════════
# Публичный API
# ═══════════════════════════════════════════════════════════════════════════════

def _merge_issues(*groups: List[str]) -> List[str]:
    seen: set = set()
    merged: List[str] = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def validate_n8n_workflow(workflow: Any) -> Tuple[bool, List[str]]:
    """Трёхуровневая валидация n8n workflow + smoke schemaDelta.

    A. Heuristic (всегда, блокирует) — структура + Direction K.
    B. Local validate-n8n.js (если доступен).
    C. Official n8n-workflow-validator / npx (если auto|on и доступен).
    Smoke: schemaDelta.missingKeys / N8N_PARAMETER ERROR → fail.

    is_valid=False при ERROR от любого слоя (в т.ч. official engine).
    """
    if not isinstance(workflow, dict):
        return False, ['Workflow должен быть объектом JSON с полями "nodes" и "connections".']

    heuristic_ok, heuristic_issues = validate_n8n_workflow_heuristic(workflow)
    local_ok, local_issues = validate_n8n_workflow_local(workflow)
    official_ok, official_issues = validate_n8n_workflow_official(workflow)

    all_issues = _merge_issues(heuristic_issues, local_issues, official_issues)
    smoke_hit = any(_is_blocking_smoke_issue(i) for i in all_issues)

    if not heuristic_ok:
        logger.warning(f"❌ Workflow отклонён heuristic: {len(heuristic_issues)} проблем")
        return False, all_issues or heuristic_issues

    if local_ok is False:
        logger.warning(f"❌ Workflow отклонён local JS: {len(local_issues)} проблем")
        return False, all_issues or local_issues

    if official_ok is False:
        logger.warning(f"❌ Workflow отклонён official n8n-engine: {len(official_issues)} проблем")
        return False, all_issues or official_issues

    if smoke_hit:
        logger.warning("❌ Workflow отклонён smoke (schemaDelta / parameter ERROR)")
        return False, all_issues

    layers = ["heuristic"]
    if local_ok is True:
        layers.append("local-js")
    if official_ok is True:
        layers.append("official-engine")
    elif official_ok is None:
        layers.append("official-skipped")
    layers.append("smoke")

    if all_issues:
        logger.info(
            f"ℹ️  Workflow валиден с замечаниями ({len(all_issues)}); слои: {', '.join(layers)}"
        )
    else:
        logger.info(f"✅ n8n workflow прошёл валидацию; слои: {', '.join(layers)}")

    return True, all_issues


def build_n8n_feedback(issues: List[str]) -> str:
    """Формирует текст замечаний для возврата агенту developer."""
    header = (
        "Сгенерированный n8n workflow НЕ пройдёт импорт / проверку движком n8n "
        '(ошибка "X is not iterable" / N8N_PARAMETER_VALIDATION_ERROR / структура) '
        "или не закрывает чек-лист устойчивости (Direction K). "
        "Исправь следующее и верни полный исправленный workflow:\n"
    )
    return header + "\n".join(f"- {i}" for i in issues)
