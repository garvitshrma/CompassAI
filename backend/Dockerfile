FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python -c "from core.retrieval import model; model(); print('embedder cached')"

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["gunicorn", "compass.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "180"]
