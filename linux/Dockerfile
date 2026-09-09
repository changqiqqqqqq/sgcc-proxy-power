# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt /app/requirements.txt

# Playwright installs Chromium together with the Linux libraries and fonts it
# needs. Keeping the Playwright package and browser installation in one layer
# prevents a package/browser revision mismatch.
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /data /home/app \
    && chmod -R a+rX /ms-playwright

COPY --chown=app:app . /app

USER app

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import json, os, urllib.request; port=os.getenv('SGCC_PORT', '8765'); r=urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3); p=json.load(r); assert r.status == 200 and p.get('ok') is True"]

CMD ["python", "-m", "app.main"]
