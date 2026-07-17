"""Контакты лидов и выгрузка outreach-пакета."""
from unittest.mock import patch

from core.outreach_export import (
    build_outreach_csv,
    build_outreach_json,
    build_outreach_markdown,
    collect_outreach_from_tasks,
    enrich_clients_with_website_contacts,
    merge_messages_with_contacts,
)


class TestOutreachContacts:
    @patch("core.lead_tools.lead_tools.scrape_website")
    def test_enrich_from_website(self, mock_scrape):
        mock_scrape.return_value = {
            "emails": ["noreply@wixpress.com", "info@clinic.example"],
            "phones": ["+7 (495) 111-22-33"],
            "telegram": ["@clinic_bot"],
        }
        clients = [
            {
                "company_name": "Клиника А",
                "website": "https://clinic.example",
                "decision_maker_role": "главный врач",
            }
        ]
        out = enrich_clients_with_website_contacts(clients, max_scrapes=5)
        assert out[0]["contact_email"] == "info@clinic.example"
        assert out[0]["contact_phone"]
        assert "website_scrape" in (out[0].get("contacts_note") or "")

    def test_merge_messages(self):
        msgs = merge_messages_with_contacts(
            [{"lead_name": "Клиника А", "message_text": "Hi", "channel": "email"}],
            [{
                "company_name": "Клиника А",
                "contact_email": "a@test.ru",
                "website": "https://a.test",
                "decision_maker_role": "собственник",
            }],
        )
        assert msgs[0]["to_email"] == "a@test.ru"
        assert msgs[0]["subject"]
        assert msgs[0]["delivery_status"] == "ready_for_manual_send"


class TestOutreachPack:
    def test_markdown_and_csv(self):
        messages = [{
            "lead_name": "X",
            "message_text": "Текст",
            "channel": "email",
            "to_email": "x@y.ru",
            "subject": "Тема",
        }]
        md = build_outreach_markdown(messages, clients=[{"company_name": "X"}])
        assert "Outreach pack" in md
        assert "Текст" in md
        csv_text = build_outreach_csv(messages)
        assert "lead_name" in csv_text
        assert "x@y.ru" in csv_text
        js = build_outreach_json(messages, clients=[{"company_name": "X"}])
        assert "manual_only" in js

    def test_collect_from_tasks(self):
        import json
        tasks = [
            {
                "agent_name": "client_hunter",
                "output_data": json.dumps({
                    "clients": [{
                        "company_name": "Dent",
                        "contact_email": "d@t.ru",
                        "website": "https://d.t",
                    }]
                }),
            },
            {
                "agent_name": "sales",
                "output_data": json.dumps({
                    "messages": [{
                        "lead_name": "Dent",
                        "message_text": "Hello",
                        "channel": "email",
                    }],
                    "qualification_questions": ["Q1?"],
                    "next_steps": "call",
                }),
            },
        ]
        clients, messages, qs, ns = collect_outreach_from_tasks(tasks)
        assert len(clients) == 1
        assert messages[0]["to_email"] == "d@t.ru"
        assert qs == ["Q1?"]
        assert ns == "call"
