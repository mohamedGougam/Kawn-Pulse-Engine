# Kawn Pulse Engine (Backend MVP)

Kawn Pulse Engine is a **topic pulse backend**: you search one topic and get **public reactions**, **quote cards**, **themes**, **sentiment**, and an **AI summary** aggregated from multiple public sources.

This MVP includes:
- **Sources**: Reddit, YouTube, News/RSS
- **Backend**: FastAPI + async pipeline + SQLite storage
- **AI layer**: Hugging Face Transformers locally when available, with an automatic **mock AI fallback** for lightweight testing

## Run locally (Windows)

1. Create a `.env` file (start from `.env.example`):
   - Copy `.env.example` to `.env`
2. Run:
   - Double-click `run.bat`

Or manually:

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open Swagger UI at `http://127.0.0.1:8000/docs`.

## Mock mode (recommended for first run)

By default, `.env.example` sets:
- `ENABLE_MOCK_DATA=true`

Behavior:
- If Reddit/YouTube keys are missing **or** `ENABLE_MOCK_DATA=true`, connectors will return realistic mock items.
- If Transformers can’t load (missing PyTorch, low RAM, etc.), the AI layer automatically falls back to mock AI outputs.

Try queries like:
- `Creed 3`
- `Real Madrid`
- `AI tools`
- `Algeria football`
- `World Cup 2026`

## Add Reddit API keys (optional)

Set in `.env`:
- `REDDIT_CLIENT_ID=...`
- `REDDIT_CLIENT_SECRET=...`
- `REDDIT_USER_AGENT=KawnPulseEngine/1.0`

Notes:
- This MVP uses a lightweight approach and can run fully in mock mode.

## Add YouTube API key (optional)

Set in `.env`:
- `YOUTUBE_API_KEY=...`

## Example API requests

### Health

`GET /health`

### Search topic pulse

`POST /api/topics/search`

Request:

```json
{ "query": "Creed 3" }
```

Response (shape):
- `topic`: stored topic record
- `summary`: AI summary + themes + sentiment
- `source_breakdown`: per-source counts
- `cards`: quote/reaction cards for UI

### Get topic

`GET /api/topics/{topic_id}`

### Get cards (paginated)

`GET /api/topics/{topic_id}/cards?page=1&page_size=20`

### Trending topics

`GET /api/topics/trending`

### Refresh topic

`POST /api/topics/{topic_id}/refresh`

### Source connector status

`GET /api/sources/status`

## Project structure

```
app/
  main.py
  config.py
  database.py
  models/
    db_models.py
    schemas.py
  api/
    routes_topics.py
    routes_health.py
    routes_sources.py
  connectors/
    base.py
    reddit_connector.py
    youtube_connector.py
    news_connector.py
    mock_connector.py
  services/
    aggregation_service.py
    normalization_service.py
    cleaning_service.py
    ai_service.py
    sentiment_service.py
    summarization_service.py
    pulse_card_service.py
    scheduler_service.py
  repositories/
    topic_repository.py
    source_item_repository.py
    pulse_card_repository.py
  utils/
    text_utils.py
    date_utils.py
tests/
```

## Roadmap: future connectors (not in MVP)

Designed to be added later as connectors:
- X (Twitter)
- Instagram
- Facebook
- TikTok

