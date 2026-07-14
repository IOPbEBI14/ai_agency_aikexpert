"""
Инструменты для реального поиска лидов.
Использует веб-скрапинг и API для поиска клиентов.
"""
import requests
import json
import logging
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
import time
import re

from core.config import Config

logger = logging.getLogger("LeadTools")


class LeadSearchTools:
    """Инструменты для поиска лидов."""
    
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
    
    def search_google(self, query: str, num_results: int = 10) -> List[Dict[str, str]]:
        """
        Поиск через Google Custom Search API.
        Требует API ключ в .env: GOOGLE_API_KEY, GOOGLE_CX
        """
        if not hasattr(Config, 'GOOGLE_API_KEY') or not Config.GOOGLE_API_KEY:
            logger.warning("Google API ключи не настроены (GOOGLE_API_KEY)")
            return []
        if not hasattr(Config, 'GOOGLE_CX') or not Config.GOOGLE_CX:
            logger.warning("Google CX не настроен (GOOGLE_CX)")
            return []
        
        try:
            url = "https://www.googleapis.com/customsearch/v1"
            params = {
                'key': Config.GOOGLE_API_KEY,
                'cx': Config.GOOGLE_CX,
                'q': query,
                'num': min(num_results, 10),
            }
            
            response = self.session.get(url, params=params, timeout=30)
            if response.status_code >= 400:
                hint = self._google_error_hint(response)
                logger.error(
                    "❌ Ошибка Google поиска: HTTP %s | query=%r | %s",
                    response.status_code,
                    query[:80],
                    hint,
                )
                return []

            data = response.json()
            results = []
            
            for item in data.get('items', []):
                results.append({
                    'title': item.get('title', ''),
                    'link': item.get('link', ''),
                    'snippet': item.get('snippet', '')
                })
            
            logger.info(f"🔍 Google поиск: найдено {len(results)} результатов")
            return results
            
        except Exception as e:
            logger.error(f"❌ Ошибка Google поиска: {e}")
            return []

    @staticmethod
    def _google_error_hint(response: requests.Response) -> str:
        """Разбирает тело ошибки Google API и даёт короткую подсказку."""
        reason = ""
        message = ""
        try:
            payload = response.json()
            err = payload.get("error") or {}
            message = err.get("message") or ""
            errors = err.get("errors") or []
            if errors and isinstance(errors[0], dict):
                reason = errors[0].get("reason") or ""
            status = err.get("status") or ""
        except Exception:
            return (response.text or "")[:400]

        hint = f"status={status or response.status_code} reason={reason} message={message}"
        low = f"{reason} {message} {status}".lower()
        if "accessnotconfigured" in low or "has not been used" in low or "disabled" in low:
            hint += (
                " | → Включите Custom Search API в том же GCP-проекте, "
                "где создан ключ: APIs & Services → Library → Custom Search API → Enable"
            )
        elif "billing" in low:
            hint += " | → Привяжите биллинг к GCP-проекту (Cloud Console → Billing)"
        elif "keyinvalid" in low or "api key not valid" in low:
            hint += " | → Проверьте GOOGLE_API_KEY и что ключ из того же проекта"
        elif "iprefererblocked" in low or "blocked" in low or "restrict" in low:
            hint += (
                " | → Снимите/ослабьте Application restrictions у API key "
                "(Credentials → Edit key), либо добавьте IP сервера"
            )
        elif response.status_code == 403:
            hint += (
                " | Типичный 403: API не включён, ключ ограничен (HTTP referrer/IP), "
                "или ключ из другого проекта. См. docs/GOOGLE_API_SETUP.md §403"
            )
        return hint
    
    def search_yandex(self, query: str, num_results: int = 10) -> List[Dict[str, str]]:
        """
        Поиск через Yandex Search API.
        Требует API ключ в .env: YANDEX_API_KEY
        """
        if not hasattr(Config, 'YANDEX_API_KEY'):
            logger.warning("Yandex API ключ не настроен")
            return []
        
        try:
            url = "https://yandex.com/search/xml"
            params = {
                'query': query,
                'l10n': 'ru',
                'sortby': 'rlv',
                'maxpassages': 1,
                'results': num_results
            }
            headers = {
                'Authorization': f'Bearer {Config.YANDEX_API_KEY}'
            }
            
            response = self.session.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            
            # Парсинг XML ответа (упрощённо)
            soup = BeautifulSoup(response.content, 'xml')
            results = []
            
            for doc in soup.find_all('doc'):
                results.append({
                    'title': doc.find('title').text if doc.find('title') else '',
                    'link': doc.find('url').text if doc.find('url') else '',
                    'snippet': doc.find('passages').text if doc.find('passages') else ''
                })
            
            logger.info(f"🔍 Yandex поиск: найдено {len(results)} результатов")
            return results
            
        except Exception as e:
            logger.error(f"❌ Ошибка Yandex поиска: {e}")
            return []
    
    def scrape_website(self, url: str) -> Dict[str, Any]:
        """
        Извлекает контактную информацию с сайта.
        """
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Извлекаем контакты
            contacts = {
                'emails': [],
                'phones': [],
                'telegram': [],
                'description': ''
            }
            
            # Поиск email
            email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            emails = re.findall(email_pattern, response.text)
            contacts['emails'] = list(set(emails))[:5]  # Максимум 5 email
            
            # Поиск телефонов
            phone_pattern = r'\+?7[\s\-]?\(?[0-9]{3}\)?[\s\-]?[0-9]{3}[\s\-]?[0-9]{2}[\s\-]?[0-9]{2}'
            phones = re.findall(phone_pattern, response.text)
            contacts['phones'] = list(set(phones))[:3]  # Максимум 3 телефона
            
            # Поиск Telegram
            telegram_pattern = r'@([a-zA-Z0-9_]{5,32})|t\.me/([a-zA-Z0-9_]{5,32})'
            telegram_matches = re.findall(telegram_pattern, response.text)
            contacts['telegram'] = list(set([f"@{m[0] or m[1]}" for m in telegram_matches]))[:3]
            
            # Извлекаем описание
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            if meta_desc and meta_desc.get('content'):
                contacts['description'] = meta_desc['content'][:500]
            
            logger.info(f"📄 Сайт {url}: найдено {len(contacts['emails'])} email, {len(contacts['phones'])} телефонов")
            return contacts
            
        except Exception as e:
            logger.error(f"❌ Ошибка скрапинга {url}: {e}")
            return {}
    
    def search_wb_sellers(self, category: str = "Одежда", min_reviews: int = 1000) -> List[Dict[str, Any]]:
        """
        Поиск селлеров Wildberries по категории.
        Использует публичный API WB.
        """
        try:
            # Публичный API WB для поиска товаров
            url = f"https://catalog.wb.ru/brands/{category.lower()}/catalog"
            params = {
                'appType': 1,
                'curr': 'rub',
                'dest': -1257786,
                'sort': 'popular',
                'spp': 30,
                'uicons': 1
            }
            
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            sellers = {}
            
            for product in data.get('data', {}).get('products', [])[:50]:
                brand = product.get('brand', '')
                if brand and brand not in sellers:
                    sellers[brand] = {
                        'company_name': brand,
                        'marketplace': 'Wildberries',
                        'category': category,
                        'reviews_count': product.get('feedbacks', 0),
                        'rating': product.get('rating', 0),
                        'products_count': 1
                    }
                elif brand in sellers:
                    sellers[brand]['products_count'] += 1
            
            # Фильтруем по количеству отзывов
            filtered = [
                s for s in sellers.values()
                if s['reviews_count'] >= min_reviews
            ]
            
            logger.info(f"🛍️ WB поиск: найдено {len(filtered)} селлеров с {min_reviews}+ отзывов")
            return filtered[:20]  # Возвращаем максимум 20
            
        except Exception as e:
            logger.error(f"❌ Ошибка поиска WB селлеров: {e}")
            return []
    
    def search_ozon_sellers(self, category: str = "Одежда") -> List[Dict[str, Any]]:
        """
        Поиск селлеров Ozon (требует API ключ).
        """
        if not hasattr(Config, 'OZON_API_KEY'):
            logger.warning("Ozon API ключ не настроен")
            return []
        
        try:
            url = "https://api-seller.ozon.ru/v2/product/list"
            headers = {
                'Client-Id': Config.OZON_CLIENT_ID,
                'Api-Key': Config.OZON_API_KEY
            }
            payload = {
                'filter': {
                    'visibility': 'ALL'
                },
                'last_id': '',
                'limit': 100
            }
            
            response = self.session.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            sellers = {}
            
            for product in data.get('result', {}).get('items', []):
                seller = product.get('seller', {})
                seller_name = seller.get('name', '')
                if seller_name and seller_name not in sellers:
                    sellers[seller_name] = {
                        'company_name': seller_name,
                        'marketplace': 'Ozon',
                        'category': category,
                        'products_count': 1
                    }
                elif seller_name in sellers:
                    sellers[seller_name]['products_count'] += 1
            
            logger.info(f"🛍️ Ozon поиск: найдено {len(sellers)} селлеров")
            return list(sellers.values())[:20]
            
        except Exception as e:
            logger.error(f"❌ Ошибка поиска Ozon селлеров: {e}")
            return []
    
    def search_telegram_channels(self, query: str) -> List[Dict[str, str]]:
        """
        Поиск Telegram каналов через веб-интерфейс.
        """
        try:
            # Используем Telegram Search
            url = f"https://tgstat.ru/search?q={quote_plus(query)}"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            channels = []
            
            # Парсим результаты поиска
            for channel in soup.find_all('div', class_='channel-card')[:10]:
                name = channel.find('a', class_='channel-name')
                if name:
                    channels.append({
                        'name': name.text.strip(),
                        'link': name.get('href', ''),
                        'description': channel.find('div', class_='channel-description').text.strip()[:200] if channel.find('div', class_='channel-description') else ''
                    })
            
            logger.info(f"📱 Telegram поиск: найдено {len(channels)} каналов")
            return channels
            
        except Exception as e:
            logger.error(f"❌ Ошибка поиска Telegram каналов: {e}")
            return []
    
    def search_avito(self, query: str) -> List[Dict[str, str]]:
        """
        Поиск на Avito (требует обход защиты).
        """
        try:
            url = f"https://www.avito.ru/rossiya?q={quote_plus(query)}"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            listings = []
            
            # Парсим объявления
            for item in soup.find_all('div', class_='item-table')[:20]:
                title = item.find('a', class_='item-title')
                if title:
                    listings.append({
                        'title': title.text.strip(),
                        'link': 'https://www.avito.ru' + title.get('href', ''),
                        'price': item.find('span', class_='price').text.strip() if item.find('span', class_='price') else ''
                    })
            
            logger.info(f"🛒 Avito поиск: найдено {len(listings)} объявлений")
            return listings
            
        except Exception as e:
            logger.error(f"❌ Ошибка поиска Avito: {e}")
            return []


# Глобальный экземпляр
lead_tools = LeadSearchTools()