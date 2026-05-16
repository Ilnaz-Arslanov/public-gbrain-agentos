---
name: perplexity-research
description: "Web research via Perplexity Sonar API with citations. Use for live web search, fact-checking, trend analysis, best-practice lookup. Triggers: «search», «research», «fact-check», «best practices», «perplexity»."
---

# Perplexity Web Research

Веб-ресёрч через Perplexity Sonar API с цитатами.

## API Key

```
~/.claude-lab/.secrets/perplexity.env
```

Загрузка: `source ~/.claude-lab/.secrets/perplexity.env` → переменная `PERPLEXITY_API_KEY`

## Использование

```bash
source ~/.claude-lab/.secrets/perplexity.env

curl -s --max-time 60 "https://api.perplexity.ai/chat/completions" \
  -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "sonar-pro",
  "messages": [{"role": "user", "content": "your query here"}],
  "search_recency_filter": "month",
  "return_citations": true
}'
```

## Модели

| Модель | Для чего | Цена |
|--------|----------|------|
| sonar-pro | Глубокий ресёрч, сложные вопросы | ~$3/1000 запросов |
| sonar | Быстрый поиск, простые вопросы | ~$1/1000 запросов |

## Параметры

- `search_recency_filter`: `hour`, `day`, `week`, `month` -- фильтр свежести
- `return_citations`: `true` -- возвращает источники
- `temperature`: 0.0-1.0 (по умолчанию 0.2)

## Парсинг ответа

```python
import json
data = json.loads(response)
text = data['choices'][0]['message']['content']
citations = data.get('citations', [])
```

## Когда использовать

- Актуальные данные из интернета
- Best practices, how-to
- Факт-чек утверждений
- Анализ трендов и новостей
- Сравнение технологий

## Когда НЕ использовать

- Data already in your local vault / database
- Internal files / configs
- Tasks that don't require web search
