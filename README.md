# NeoMarket B2B Service

FastAPI + SQLAlchemy B2B seller cabinet service for the NeoMarket marketplace.

## Stack

- Python 3.12
- FastAPI
- SQLAlchemy 2.0
- PostgreSQL (production) / SQLite (tests)

## Development setup

```bash
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements.txt
```

## Tests

```bash
pytest tests/ -v
```
