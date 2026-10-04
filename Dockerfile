FROM python:3.11-slim

ARG RUN_COLLECTSTATIC=false
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
# Consider: move `python -m spacy download en_core_web_sm` to CI or a separate image layer to cache it

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libopenblas-dev \
    git \
    curl \
    ca-certificates \
    libssl-dev \
    netcat-openbsd \
    ffmpeg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Download SpaCy model
RUN python -m spacy download en_core_web_sm

## create non-root user early so we can set ownership during COPY
RUN adduser --disabled-password --gecos "" appuser

# copy source files as appuser to avoid heavy chown later (works for prod builds)
COPY --chown=appuser:appuser . .

# conditional collectstatic; controlled at build time with --build-arg
RUN if [ "${RUN_COLLECTSTATIC}" = "true" ] ; then \
      echo "Running collectstatic at build time"; \
      python manage.py collectstatic --noinput; \
    else \
      echo "Skipping collectstatic at build time"; \
    fi

ENTRYPOINT ["/entrypoint.sh"]

CMD ["sh", "-c", "exec gunicorn studio_desk.wsgi:application --bind 0.0.0.0:8000 $GUNICORN_CMD_ARGS"]
