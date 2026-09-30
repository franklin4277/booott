FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services ./services
COPY schemas ./schemas
COPY event_bus ./event_bus
COPY utils ./utils
COPY database ./database
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "services.api_gateway.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
