FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=10001:10001 app.py ./

USER 10001:10001
EXPOSE 8080

CMD ["gunicorn", "--bind=0.0.0.0:8080", "--workers=1", "--no-control-socket", "--access-logfile=-", "app:app"]
