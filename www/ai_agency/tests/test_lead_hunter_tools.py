"""Анти-галлюцинации lead_hunter + OpenSERP filter."""
from core.lead_hunter_tools import (
    filter_hallucinated_leads,
    filter_seller_hits,
    lead_matches_serp,
)


class TestLeadSerpFilter:
    def test_drops_marketplace_hosts(self):
        hits = [
            {
                "title": "Wildberries",
                "link": "https://www.wildberries.ru/seller/123",
                "snippet": "селлер",
            },
            {
                "title": "Бренд Ромашка | Официальный сайт",
                "link": "https://romashka-shop.example/",
                "snippet": "Интернет-магазин одежды",
            },
        ]
        out = filter_seller_hits(hits)
        assert len(out) == 1
        assert "romashka" in out[0]["link"]

    def test_rejects_hallucinated_lead(self):
        serp = [
            {
                "title": "Бренд Ромашка",
                "link": "https://romashka.example",
                "snippet": "одежда",
                "query": "q",
            }
        ]
        leads = [
            {
                "company_name": "Модный Дом",
                "contact_telegram": "@modny_dom_seller",
                "source": "Wildberries (парсинг)",
            },
            {
                "company_name": "Бренд Ромашка",
                "website": "https://romashka.example",
                "source_url": "https://romashka.example",
            },
        ]
        kept = filter_hallucinated_leads(leads, serp)
        assert len(kept) == 1
        assert kept[0]["company_name"] == "Бренд Ромашка"

    def test_match_by_name_in_title(self):
        hit = lead_matches_serp(
            {"company_name": "ТехноФикс"},
            [{"title": "ТехноФикс — магазин", "link": "https://t.example", "snippet": ""}],
        )
        assert hit is not None
