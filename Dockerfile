FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Poetry lives in its own venv; the app dependencies go to /opt/venv.
RUN python -m venv /opt/poetry \
    && /opt/poetry/bin/pip install "poetry==2.5.1" \
    && python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /srv
COPY pyproject.toml poetry.lock ./
# Dev dependencies (pytest, ruff) are included by default so tests run in the same image.
# For a production image: docker build --build-arg INSTALL_DEV=false .
ARG INSTALL_DEV=true
RUN if [ "$INSTALL_DEV" = "true" ]; then \
        /opt/poetry/bin/poetry install --no-root --no-interaction --no-ansi; \
    else \
        /opt/poetry/bin/poetry install --no-root --no-interaction --no-ansi --only main; \
    fi

RUN useradd --create-home --uid 10001 app && chown app:app /srv
COPY --chown=app:app . .
USER app

EXPOSE 8000
CMD ["gunicorn", "app.main:app", "--worker-class", "uvicorn_worker.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8000", "--graceful-timeout", "30"]
