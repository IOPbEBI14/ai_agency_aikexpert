# Инструкция: получение `GOOGLE_API_KEY` и `GOOGLE_CX`

Агент `client_hunter` использует **Google Custom Search JSON API**  
(официально: [Custom Search JSON API](https://developers.google.com/custom-search/v1/introduction)).

Нужны **два** значения:

| Переменная | Что это |
|------------|---------|
| `GOOGLE_API_KEY` | API-ключ проекта в Google Cloud |
| `GOOGLE_CX` | ID Programmable Search Engine (Search Engine ID) |

Добавьте их в `www/ai_agency/.env`:

```env
GOOGLE_API_KEY=ваш_ключ
GOOGLE_CX=ваш_search_engine_id
```

---

## Часть A. Получить `GOOGLE_API_KEY`

### 1. Создайте проект в Google Cloud

1. Откройте [Google Cloud Console](https://console.cloud.google.com/).
2. Войдите в аккаунт Google.
3. Сверху выберите **Select a project → New Project**.
4. Имя: например `ai-agency-client-hunter` → **Create**.

### 2. Включите Custom Search JSON API

1. В меню: **APIs & Services → Library**  
   (или поиск: «Custom Search API»).
2. Найдите **Custom Search API** / **Custom Search JSON API**.
3. Нажмите **Enable**.

### 3. Создайте API key

1. **APIs & Services → Credentials**.
2. **+ Create credentials → API key**.
3. Скопируйте ключ — это `GOOGLE_API_KEY`.
4. Рекомендуется сразу ограничить ключ:
   - **Edit API key → API restrictions → Restrict key**
   - Разрешить только **Custom Search API**
   - (опционально) Application restrictions: IP сервера агентства

> Не коммитьте ключ в git. Файл `.env` уже в `.gitignore`.

---

## Часть B. Получить `GOOGLE_CX` (Search Engine ID)

API ищет **не «весь Google сам по себе»**, а через созданный вами **Programmable Search Engine**.

### 1. Создайте поисковую систему

1. Откройте [Programmable Search Engine Control Panel](https://programmablesearchengine.google.com/controlpanel/all).
2. **Add** / **Create a search engine**.
3. **Name**: например `AI Agency Open Web`.
4. **What to search?**
   - Для поиска клиентов по открытому вебу включите поиск по **всему интернету**  
     (опция вроде *Search the entire web* / «Искать по всему интернету»).
   - Если UI просит указать сайты: можно временно указать любой сайт (например `wikipedia.org`),  
     затем в настройках CSE включить **Search the entire web**.
5. Создайте движок (**Create**).

Официальный туториал: [Creating a Programmable Search Engine](https://developers.google.com/custom-search/docs/tutorial/creatingcse).

### 2. Скопируйте Search Engine ID

1. Откройте созданный движок → **Overview** / **Basics**.
2. Найдите **Search engine ID** — длинная строка вида `a1b2c3d4e5f6g7h8i` или `0175...:xxxxx`.
3. Это значение — `GOOGLE_CX` (параметр `cx` в API).

### 3. Важные настройки для `client_hunter`

В панели CSE проверьте:

| Настройка | Рекомендация |
|-----------|--------------|
| Search the entire web | **Включено** (иначе будут только указанные сайты) |
| Image search | не обязательно |
| SafeSearch | по желанию |

---

## Часть C. Проверка

### 1. Ручной тест в браузере

Подставьте свои значения:

```text
https://www.googleapis.com/customsearch/v1?key=GOOGLE_API_KEY&cx=GOOGLE_CX&q=селлер%20wildberries%20автоматизация
```

Ожидание: JSON с полем `items` (title, link, snippet).

Типичные ошибки:
- `API key not valid` → неверный ключ или API не включён
- `invalid argument` / пустой `cx` → неверный `GOOGLE_CX`
- `Daily Limit Exceeded` → исчерпан дневной лимит

### 2. Проверка агентства

1. Пропишите ключи в `.env`.
2. Перезапустите FastAPI (`python main.py`).
3. Запустите проект с задачей `client_hunter`.
4. В логах ожидайте: `client_hunter Google: N уникальных результатов`.
5. Если ключей нет — агент вернёт `clients=[]` и предупреждение в notes (не выдумывает компании).

---

## Лимиты и биллинг (кратко)

- У Custom Search JSON API есть **бесплатная квота** (часто порядка **100 запросов/сутки** на проект; актуальные цифры смотрите в Cloud Console → Quotas).
- Сверх квоты — платные запросы (см. [Pricing](https://developers.google.com/custom-search/v1/overview#pricing) в документации Google).
- Для регистрации биллинга в Cloud иногда нужна карта; бесплатная квота при этом обычно сохраняется, пока не превысите лимит.

---

## Безопасность

1. Не публикуйте ключ в чатах, скриншотах, репозитории.
2. Ограничьте ключ только Custom Search API.
3. При утечке — **Rotate / Regenerate** ключ в Cloud Console.
4. Для продакшена предпочтительнее отдельный GCP-проект только под поиск.

---

## Часть D. Ошибка HTTP 403 / PERMISSION_DENIED

### Сообщение: «This project does not have the access to Custom Search JSON API»

Это **не** ошибка ключа и **не** «забыли Enable».

С 2025–2026 Google **закрыл Custom Search JSON API для новых клиентов**.  
Официально: [Custom Search JSON API Overview](https://developers.google.com/custom-search/v1/overview) —
*«The Custom Search JSON API is closed to new customers»*.  
Старые аккаунты могут пользоваться до **1 января 2027**.

Поэтому при новом GCP-проекте вы получите:

```json
{
  "error": {
    "code": 403,
    "message": "This project does not have the access to Custom Search JSON API.",
    "status": "PERMISSION_DENIED",
    "errors": [{ "reason": "forbidden" }]
  }
}
```

даже если:
- API в Library показывает Enabled,
- Billing привязан,
- ключ и CX корректны.

**Что делать:**
1. Если есть **старый** GCP-проект, где CSE уже работал раньше — используйте его ключ.
2. Иначе Custom Search JSON API **недоступен** — нужен альтернативный провайдер поиска (Serper, SerpAPI, Brave Search и т.п.).  
   Напишите в чат — подключим fallback в `client_hunter`.

### Другие 403 (если message другой)

1. `accessNotConfigured` → Enable Custom Search API в том же проекте, что и ключ.
2. Application restrictions (HTTP referrers) → для сервера поставьте None или IP.
3. Неверный / отозванный ключ → другой `message` (`API key not valid`).

### Быстрая диагностика в браузере

```text
https://www.googleapis.com/customsearch/v1?key=ВАШ_KEY&cx=ВАШ_CX&q=test
```

Смотрите `error.message` и `error.errors[0].reason`.

---

## Связанные файлы в проекте

| Файл | Назначение |
|------|------------|
| `core/config.py` | читает `GOOGLE_API_KEY`, `GOOGLE_CX` |
| `core/client_hunter_tools.py` | вызовы Google |
| `core/lead_tools.py` → `search_google()` | HTTP к `googleapis.com/customsearch/v1` |
| `docs/TZ_client_search_template.md` | шаблон ТЗ на поиск клиентов |
