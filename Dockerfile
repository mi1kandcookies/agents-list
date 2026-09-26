FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

# Use non-root runtime user.
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# Production defaults (override via env in deployment platform)
ENV FLASK_ENV=production
ENV AUTO_SEED_DATA=0
ENV ENABLE_SIM_ENGINE=0
ENV STRICT_PROD_VALIDATION=1

CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "4", "-b", "0.0.0.0:5000", "wsgi:app"]
