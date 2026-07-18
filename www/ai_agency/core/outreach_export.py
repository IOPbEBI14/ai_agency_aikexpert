"""Контакты с сайтов лидов + выгрузка готовых писем sales (без отправки)."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("OutreachExport")

# Фирменная подпись директора (обязательна в каждом письме sales)
SALES_SIGNATURE = (
    'С уважением,\n'
    'Иконников Алексей,\n'
    'директор агентства "Деловая экспертиза"'
)

_BAD_EMAIL_RE = re.compile(
    r"(?i)(noreply|no-?reply|donotreply|example\.|sentry\.|wixpress|"
    r"cloudflare|schema\.org|googleapis|w3\.org|png|jpg|jpeg|webp|svg)"
)


def _pick_best_email(emails: List[str]) -> Optional[str]:
    cleaned = []
    for e in emails or []:
        e = (e or "").strip()
        if not e or "@" not in e:
            continue
        if _BAD_EMAIL_RE.search(e):
            continue
        cleaned.append(e)
    if not cleaned:
        return None
    # предпочитаем info@ / admin@ / clinic@
    preferred = ("info@", "admin@", "clinic@", "stomat", "mail@", "hello@")
    for pref in preferred:
        for e in cleaned:
            if pref in e.lower():
                return e
    return cleaned[0]


def _pick_first(items: List[str]) -> Optional[str]:
    for x in items or []:
        x = (x or "").strip()
        if x:
            return x
    return None


def enrich_clients_with_website_contacts(
    clients: List[Dict[str, Any]],
    *,
    max_scrapes: int = 15,
) -> List[Dict[str, Any]]:
    """Дополняет карточки клиентов контактами с их website (открытый scrape).

    Не выдумывает данные: если scrape пуст — поля остаются пустыми.
    """
    from core.lead_tools import lead_tools

    enriched: List[Dict[str, Any]] = []
    scraped = 0
    for client in clients:
        item = dict(client)
        website = (item.get("website") or "").strip()
        email = (item.get("contact_email") or "").strip() or None
        phone = (item.get("contact_phone") or "").strip() or None
        tg = (item.get("contact_telegram") or "").strip() or None
        note = item.get("contacts_note") or ""

        if website and scraped < max_scrapes and not (email and phone):
            try:
                raw = lead_tools.scrape_website(website) or {}
                scraped += 1
                if not email:
                    email = _pick_best_email(list(raw.get("emails") or []))
                if not phone:
                    phone = _pick_first(list(raw.get("phones") or []))
                if not tg:
                    phone_tg = _pick_first(list(raw.get("telegram") or []))
                    tg = phone_tg
                if email or phone or tg:
                    note = (note + "; " if note else "") + "website_scrape"
            except Exception as e:
                logger.warning("scrape contacts failed for %s: %s", website, e)
                scraped += 1

        item["contact_email"] = email
        item["contact_phone"] = phone
        item["contact_telegram"] = tg
        item["contacts_note"] = note or None
        if not item.get("decision_maker_role"):
            item["decision_maker_role"] = item.get("decision_maker_role")
        enriched.append(item)

    with_contacts = sum(
        1 for c in enriched if c.get("contact_email") or c.get("contact_phone")
    )
    logger.info(
        "outreach contacts: scraped=%d clients=%d with_email_or_phone=%d",
        scraped, len(enriched), with_contacts,
    )
    return enriched


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def ensure_sales_signature(text: str) -> str:
    """Гарантирует фирменную подпись в конце письма (если модель забыла)."""
    body = (text or "").rstrip()
    if not body:
        return SALES_SIGNATURE
    # Уже есть характерный маркер подписи
    if "иконников алексей" in body.lower() and "деловая экспертиза" in body.lower():
        return body
    return f"{body}\n\n{SALES_SIGNATURE}"


def merge_messages_with_contacts(
    messages: List[Dict[str, Any]],
    clients: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Подставляет контакты/сайт/ЛПР в письма sales по имени компании."""
    by_name = {_norm_name(c.get("company_name") or ""): c for c in clients}
    out: List[Dict[str, Any]] = []
    for msg in messages:
        m = dict(msg)
        key = _norm_name(m.get("lead_name") or "")
        c = by_name.get(key) or {}
        m.setdefault("website", c.get("website"))
        m.setdefault("to_email", c.get("contact_email") or m.get("to_email"))
        m.setdefault("to_phone", c.get("contact_phone") or m.get("to_phone"))
        m.setdefault("to_telegram", c.get("contact_telegram") or m.get("to_telegram"))
        m.setdefault(
            "decision_maker_role",
            c.get("decision_maker_role") or m.get("decision_maker_role"),
        )
        m["message_text"] = ensure_sales_signature(str(m.get("message_text") or ""))
        if not m.get("subject"):
            company = m.get("lead_name") or "клиника"
            m["subject"] = f"Пара идей для {company} — без длинных презентаций"
        # если канал email, но адреса нет — помечаем
        if m.get("channel") == "email" and not m.get("to_email"):
            m["delivery_status"] = "ready_text_no_email"
        else:
            m["delivery_status"] = "ready_for_manual_send"
        out.append(m)
    return out


def build_outreach_markdown(
    messages: List[Dict[str, Any]],
    *,
    qualification_questions: Optional[List[str]] = None,
    next_steps: str = "",
    clients: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Человекочитаемый пакет писем для ручной отправки."""
    lines = [
        "# Outreach pack — готовые письма (без автоотправки)",
        "",
        "Статус: тексты подготовлены агентом `sales`. Отправка вручную.",
        "",
    ]
    if clients:
        lines.append("## Контакты ЛПР / клиник")
        lines.append("")
        for c in clients:
            lines.append(f"### {c.get('company_name') or '—'}")
            lines.append(f"- Сайт: {c.get('website') or '—'}")
            lines.append(f"- ЛПР (гипотеза): {c.get('decision_maker_role') or '—'}")
            lines.append(f"- Email: {c.get('contact_email') or '—'}")
            lines.append(f"- Телефон: {c.get('contact_phone') or '—'}")
            lines.append(f"- Telegram: {c.get('contact_telegram') or '—'}")
            if c.get("contacts_note"):
                lines.append(f"- Источник контактов: {c['contacts_note']}")
            lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## Письма")
    lines.append("")
    for i, m in enumerate(messages, 1):
        lines.append(f"### {i}. {m.get('lead_name') or 'Лид'}")
        lines.append(f"- Канал: `{m.get('channel') or 'email'}`")
        lines.append(f"- Кому (email): {m.get('to_email') or '—'}")
        lines.append(f"- Телефон: {m.get('to_phone') or '—'}")
        lines.append(f"- Telegram: {m.get('to_telegram') or '—'}")
        lines.append(f"- Сайт: {m.get('website') or '—'}")
        lines.append(f"- ЛПР: {m.get('decision_maker_role') or '—'}")
        lines.append(f"- Тема: {m.get('subject') or '—'}")
        lines.append(f"- Статус: {m.get('delivery_status') or 'ready_for_manual_send'}")
        pts = m.get("personalization_points") or []
        if pts:
            lines.append("- Персонализация: " + "; ".join(str(p) for p in pts))
        lines.append("")
        lines.append("```")
        lines.append(str(m.get("message_text") or "").strip())
        lines.append("```")
        lines.append("")

    if qualification_questions:
        lines.append("## Вопросы квалификации")
        for q in qualification_questions:
            lines.append(f"- {q}")
        lines.append("")
    if next_steps:
        lines.append("## Next steps")
        lines.append(str(next_steps))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_outreach_csv(messages: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    fields = [
        "lead_name", "channel", "to_email", "to_phone", "to_telegram",
        "website", "decision_maker_role", "subject", "message_text",
        "delivery_status", "personalization_points",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for m in messages:
        row = {k: m.get(k) for k in fields}
        pts = m.get("personalization_points") or []
        row["personalization_points"] = "; ".join(str(p) for p in pts)
        writer.writerow(row)
    return buf.getvalue()


def build_outreach_json(
    messages: List[Dict[str, Any]],
    *,
    qualification_questions: Optional[List[str]] = None,
    next_steps: str = "",
    clients: Optional[List[Dict[str, Any]]] = None,
) -> str:
    payload = {
        "export_kind": "outreach_pack",
        "send_mode": "manual_only",
        "clients": clients or [],
        "messages": messages,
        "qualification_questions": qualification_questions or [],
        "next_steps": next_steps or "",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def collect_outreach_from_tasks(
    tasks: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str], str]:
    """Достаёт clients + sales messages из задач проекта."""
    clients: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    questions: List[str] = []
    next_steps = ""

    def _parse(raw: Any) -> Optional[Dict[str, Any]]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                data = json.loads(raw)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    for t in tasks:
        agent = t.get("agent_name")
        parsed = _parse(t.get("output_data"))
        if not parsed:
            continue
        if agent == "client_hunter":
            for c in parsed.get("clients") or []:
                if isinstance(c, dict):
                    clients.append(c)
        if agent == "sales":
            for m in parsed.get("messages") or []:
                if isinstance(m, dict):
                    messages.append(m)
            questions = list(parsed.get("qualification_questions") or questions)
            next_steps = parsed.get("next_steps") or next_steps

    messages = merge_messages_with_contacts(messages, clients)
    return clients, messages, questions, next_steps
