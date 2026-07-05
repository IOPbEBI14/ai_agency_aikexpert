"""
Двухуровневый валидатор n8n workflow.

Уровень 1 — Runtime (n8n-workflow-validator):
  Вызывает официальный npm-пакет «n8n-workflow-validator» через npx.
  Использует реальный движок n8n (NodeHelpers.getNodeParameters /
  getNodeParametersIssues) — ловит всё то же, что ловит n8n-редактор при импорте.
  Требует Node.js на хосте; при первом запуске npx скачивает пакет автоматически.

Уровень 2 — Heuristic (Python-only fallback):
  Структурные проверки без внешних зависимостей: ловит самые частые
  LLM-галлюцинации (interval не массив, conditions.string вместо conditions.conditions,
  values вместо assignments, пустой options в if/switch и т.д.).
  Активируется, если Node.js недоступен или npx завершился с ошибкой.

Публичный API:
  validate_n8n_workflow(workflow)  → (is_valid: bool, issues: list[str])
  build_n8n_feedback(issues)       → str  (текст для qa_feedback агенту)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("N8NValidator")

# ─── Константы ────────────────────────────────────────────────────────────────

# Таймаут одного запуска валидатора (секунды).
# Локальный бинарник запускается ~1-2с; npx как fallback — до 30с.
_RUNTIME_TIMEOUT = 30

# Переменная окружения для принудительного отключения runtime-валидации
# (удобно в тестах: N8N_VALIDATOR_RUNTIME=off)
_ENV_DISABLE_RUNTIME = "N8N_VALIDATOR_RUNTIME"

# Директория с package.json/node_modules проекта
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Кэш: путь к исполняемому файлу валидатора (str) или None если недоступен
_runtime_bin: Optional[str] = None
_runtime_checked: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 1: Runtime-валидация через n8n-workflow-validator
# ═══════════════════════════════════════════════════════════════════════════════

def _find_runtime_binary() -> Optional[str]:
    """Ищет бинарник n8n-workflow-validator (кэшируется на процесс).

    Порядок поиска:
      1. ./node_modules/.bin/n8n-workflow-validator  (локальная установка)
      2. n8n-workflow-validator                       (глобальная установка)

    npx намеренно НЕ используется: при отсутствии пакета он скачивает
    n8n-workflow + n8n-nodes-base (~300 МБ), что занимает несколько минут.
    Для установки выполни: cd www/ai_agency && npm install
    """
    global _runtime_bin, _runtime_checked
    if _runtime_checked:
        return _runtime_bin
    _runtime_checked = True

    if os.getenv(_ENV_DISABLE_RUNTIME, "").lower() in ("off", "0", "false"):
        logger.info("ℹ️  Runtime-валидация n8n отключена (N8N_VALIDATOR_RUNTIME=off)")
        return None

    # Локальный бинарник (предпочтительный вариант)
    bin_name = "n8n-workflow-validator.cmd" if sys.platform == "win32" else "n8n-workflow-validator"
    local_bin = os.path.join(_PROJECT_DIR, "node_modules", ".bin", bin_name)
    if os.path.isfile(local_bin):
        _runtime_bin = local_bin
        logger.info(f"✅ n8n-workflow-validator найден локально: {local_bin}")
        return _runtime_bin

    # Глобальная установка
    try:
        result = subprocess.run(
            ["n8n-workflow-validator", "--version"],
            capture_output=True, text=True, timeout=5,
            shell=(sys.platform == "win32"),
        )
        if result.returncode == 0:
            _runtime_bin = "n8n-workflow-validator"
            logger.info("✅ n8n-workflow-validator найден глобально")
            return _runtime_bin
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    logger.info(
        "ℹ️  n8n-workflow-validator не установлен — используется heuristic-валидатор. "
        "Для включения runtime: cd www/ai_agency && npm install"
    )
    return None


def _parse_runtime_output(stdout: str, stderr: str) -> List[str]:
    """Разбирает вывод n8n-workflow-validator и возвращает список замечаний."""
    issues: List[str] = []

    text = stdout.strip()
    # JSON-формат (флаг --json или новые версии пакета)
    if text.startswith(("[", "{")):
        try:
            data = json.loads(text)
            items = data if isinstance(data, list) else data.get("errors", [data])
            for item in items:
                if not isinstance(item, dict):
                    continue
                sev = item.get("severity", "error").upper()
                code = item.get("code", "")
                msg = item.get("message") or item.get("what") or str(item)
                node = item.get("node") or item.get("nodeName", "")
                loc = f" [{node}]" if node else ""
                issues.append(f"[{sev}]{loc} {code}: {msg}")
            return issues
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # Текстовый вывод — ищем строки с маркерами ошибок
    for line in (stdout + "\n" + stderr).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if any(
            m in lower
            for m in ("error", "invalid", "missing", "is not iterable",
                      "n8n_parameter", "deprecated", "unknown node")
        ):
            issues.append(stripped)
    return issues


def validate_n8n_workflow_runtime(
    workflow: Dict[str, Any],
) -> Tuple[Optional[bool], List[str]]:
    """Запускает runtime-валидацию через n8n-workflow-validator.

    Returns:
        (True, [])        — workflow прошёл валидацию
        (False, issues)   — найдены ошибки
        (None, [])        — runtime недоступен (используй heuristic)
    """
    binary = _find_runtime_binary()
    if binary is None:
        return None, []

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
            encoding="utf-8", prefix="n8n_wf_",
        ) as tf:
            json.dump(workflow, tf, ensure_ascii=False)
            tmp_path = tf.name

        cmd = [binary, tmp_path]
        logger.info(f"🔍 Runtime-валидация n8n: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=_RUNTIME_TIMEOUT,
            shell=(sys.platform == "win32"),
        )

        issues = _parse_runtime_output(result.stdout, result.stderr)

        if result.returncode == 0 and not issues:
            logger.info("✅ Runtime-валидация n8n: workflow валиден")
            return True, []

        if result.returncode == 0 and issues:
            logger.warning(f"⚠️  Runtime: предупреждения ({len(issues)}), импорт возможен")
            return True, issues  # предупреждения не блокируют

        logger.warning(f"❌ Runtime: {len(issues)} ошибок (exit {result.returncode})")
        return False, issues or [
            f"n8n-workflow-validator завершился с кодом {result.returncode}. "
            f"stderr: {result.stderr[:400]}"
        ]

    except subprocess.TimeoutExpired:
        logger.error(f"⏱️  n8n-workflow-validator: таймаут {_RUNTIME_TIMEOUT}с")
        return None, []
    except Exception as exc:
        logger.error(f"❌ Ошибка runtime-валидации: {exc}", exc_info=True)
        return None, []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 2: Heuristic-валидация (Python-only)
# ═══════════════════════════════════════════════════════════════════════════════

_SCHEDULE_INTERVAL_HINT = (
    'Поле "rule.interval" у scheduleTrigger должно быть МАССИВОМ объектов, '
    'например: "rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}. '
    'Число или объект вместо массива вызывает ошибку импорта "is not iterable".'
)


def _node_label(node: Dict[str, Any], idx: int) -> str:
    return node.get("name") or node.get("id") or f"#{idx}"


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
    if conditions is None or not isinstance(conditions, dict):
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


def _check_empty_options(node: Dict[str, Any], short_type: str, label: str, issues: List[str]) -> None:
    if short_type in ("if", "switch"):
        params = node.get("parameters", {})
        if params.get("options") == {}:
            issues.append(
                f'[{label}] {short_type}: пустой "options":{{}} ломает импорт '
                f'("Could not find property option"). Удали ключ options.'
            )


def validate_n8n_workflow_heuristic(workflow: Any) -> Tuple[bool, List[str]]:
    """Python-only структурная валидация (fallback если Node.js недоступен)."""
    issues: List[str] = []

    if not isinstance(workflow, dict):
        return False, ['Корень workflow должен быть объектом с "nodes" и "connections".']

    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return False, ['Поле "nodes" отсутствует или не является массивом.']
    if not nodes:
        return False, ['Массив "nodes" пуст.']

    connections = workflow.get("connections")
    if connections is not None and not isinstance(connections, dict):
        issues.append('Поле "connections" должно быть объектом.')

    node_names: set = set()
    for idx, node in enumerate(nodes):
        label = _node_label(node, idx)
        if not isinstance(node, dict):
            issues.append(f"[{label}] нода должна быть объектом.")
            continue

        node_names.add(node.get("name"))
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

        short = node_type.split(".")[-1] if node_type else ""
        if short == "scheduleTrigger":
            _check_schedule_trigger(node, label, issues)
        elif short == "if":
            _check_if_node(node, label, issues)
        elif short == "set":
            _check_set_node(node, label, issues)
        elif short == "httpRequest":
            _check_http_request(node, label, issues)
        _check_empty_options(node, short, label, issues)

    # Связность connections
    if isinstance(connections, dict):
        for src, outputs in connections.items():
            if src not in node_names:
                issues.append(f'connections: источник "{src}" не найден в nodes.')
            if not isinstance(outputs, dict):
                continue
            for _okey, buckets in outputs.items():
                if not isinstance(buckets, list):
                    issues.append(f'connections["{src}"].main должен быть массивом.')
                    continue
                for bucket in buckets:
                    if not isinstance(bucket, list):
                        continue
                    for link in bucket:
                        if isinstance(link, dict) and link.get("node") not in node_names:
                            issues.append(
                                f'connections["{src}"]: цель "{link.get("node")}" не найдена в nodes.'
                            )

    return (len(issues) == 0), issues


# ═══════════════════════════════════════════════════════════════════════════════
# Публичный API
# ═══════════════════════════════════════════════════════════════════════════════

def validate_n8n_workflow(workflow: Any) -> Tuple[bool, List[str]]:
    """Двухуровневая валидация n8n workflow.

    1. Пробует runtime-валидатор (npx n8n-workflow-validator).
    2. Если Node.js недоступен или npx упал — использует heuristic.

    Returns:
        (is_valid, issues) — is_valid=True если критичных проблем нет.
    """
    if not isinstance(workflow, dict):
        return False, ['Workflow должен быть объектом JSON с полями "nodes" и "connections".']

    # Попытка runtime-валидации
    runtime_ok, runtime_issues = validate_n8n_workflow_runtime(workflow)

    if runtime_ok is not None:
        # Runtime отработал — доверяем его результату
        source = "runtime (n8n-workflow-validator)"
        if runtime_ok:
            # Runtime чист — дополнительно прогоняем heuristic для extra-проверок
            _, h_issues = validate_n8n_workflow_heuristic(workflow)
            if h_issues:
                logger.info(
                    f"ℹ️  Heuristic нашёл {len(h_issues)} доп. замечаний после чистого runtime"
                )
                return True, h_issues  # не блокируем, но передаём в фидбек
            return True, []
        else:
            logger.warning(f"❌ Workflow отклонён {source}: {len(runtime_issues)} проблем")
            # Дополняем runtime-замечания heuristic-проверками (без дублирования)
            _, h_issues = validate_n8n_workflow_heuristic(workflow)
            combined = runtime_issues + [h for h in h_issues if h not in runtime_issues]
            return False, combined

    # Fallback: только heuristic
    logger.info("ℹ️  Heuristic-валидация (runtime недоступен)")
    return validate_n8n_workflow_heuristic(workflow)


def build_n8n_feedback(issues: List[str]) -> str:
    """Формирует текст замечаний для возврата агенту developer."""
    header = (
        "Сгенерированный n8n workflow НЕ пройдёт импорт "
        '(ошибка "X is not iterable" / структурные несоответствия). '
        "Исправь следующее и верни полный исправленный workflow:\n"
    )
    return header + "\n".join(f"- {i}" for i in issues)
