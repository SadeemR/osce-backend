# OSCE Simulator — Backend API

Flask REST API that powers the [OSCE Simulator app](https://github.com/SadeemR/osce-flutter-app). It turns any clinical case into an AI-simulated patient, and grades the student's conversation against the university's own rubric.

> 🎓 Graduation project · Python + Flask + PostgreSQL + Claude API
> 🔗 Frontend repository: [osce-flutter-app](https://github.com/SadeemR/osce-flutter-app)

---

## What It Does

- **Dynamic patient persona** — builds a system prompt from the case fields (patient, complaint, findings, attitude, attached file), so it works for any case and any university.
- **Station types** — `bad_news` (breaking bad news) and `history_taking`. Each type gets its own patient behavior: in history taking the patient opens with the chief complaint only and answers just what is asked.
- **Hidden case details** — `hidden_info` is given to the AI patient but never shown to the student; the patient reveals each detail only when asked about it.
- **Rubric-driven evaluation** — scores the conversation using the uploaded rubric's exact criteria, weights and maximum scores, and returns structured JSON. Without a rubric it falls back to a default for the station type (SPIKES for bad news, a history-taking rubric for history taking).
- **Rubric parsing** — extracts criteria from an uploaded marking sheet.
- **Bilingual** — Arabic or English, following the case and rubric language.
- **Isolated sessions** — every case gets its own `session_id`, so multiple students can practice at the same time without their conversations mixing. Idle sessions expire after 2 hours.
- **Persistence** — stores students and evaluation results in PostgreSQL.

## Architecture

```mermaid
flowchart LR
    A[Flutter App] -- REST / JSON --> B[Flask API]
    B -- persona & evaluation prompts --> C[Claude API]
    B --> D[(PostgreSQL)]
```

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/update-case` | Start a session with a new case (JSON) → returns `session_id` |
| `POST` | `/update-case-file` | Start a session with an attached case file (multipart) → returns `session_id` |
| `POST` | `/chat` | Send `session_id` + the doctor's message, get the patient's reply |
| `POST` | `/evaluate` | Grade that session's conversation against the rubric |
| `POST` | `/parse-rubric` | Extract criteria from a rubric file |
| `POST` | `/students/` | Add a student |
| `GET` | `/students/` | List students |
| `POST` | `/students/generate-prompt/` | Generate a persona prompt for a case |
| `POST` | `/save-result/` | Save an evaluation result |
| `GET` | `/results/` | List saved results |

## Tech Stack

Python · Flask · Flask-CORS · Gunicorn · PostgreSQL (psycopg2) · Anthropic Claude API · Railway

## Running Locally

```bash
git clone https://github.com/SadeemR/osce-backend.git
cd osce-backend
pip install -r requirements.txt

export ANTHROPIC_API_KEY="your-key"
export DATABASE_URL="postgresql://..."   # optional — database features are skipped without it

python app.py
```

Then open `http://localhost:8000/health`. Without `DATABASE_URL`, the API still works; results simply aren't stored.

## Tests

```bash
pip install pytest
pytest
```

The tests mock the Claude API, so they run offline at no cost. They cover session isolation, session expiry, station-specific patient behavior, hidden case details, default rubrics and running without a database.

## Deployment

Deployed on Railway using the included `Procfile` (Gunicorn). Set `ANTHROPIC_API_KEY` and `DATABASE_URL` as environment variables — no secrets are stored in the code.

## Future Improvements

- Move session storage to Redis so the API can scale beyond a single worker
- Authentication and rate limiting
- Measure evaluator agreement with human examiners

## Author

**Sadeem Mubarak** — [GitHub](https://github.com/SadeemR)
