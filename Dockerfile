FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hooze ./hooze
COPY migrations ./migrations
# One worker until Redis is configured (see .env.example); scale workers only after UPSTASH_* is set.
CMD ["sh", "-c", "uvicorn hooze.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
