# syntax=docker/dockerfile:1

# The base image is pinned by digest (a multi-arch index, so it works on amd64
# and arm64) and every Python package is pinned in requirements.txt, so the same
# commit builds the same image. To update the base, run
#   docker buildx imagetools inspect python:3.12-slim-bookworm
# and paste the new index digest here.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

# --- build: install dependencies into a virtualenv --------------------------
FROM ${PYTHON_IMAGE} AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt \
    && /opt/venv/bin/pip check \
    && /opt/venv/bin/pip uninstall -y pip

# --- runtime: the virtualenv and the app, run as an unprivileged user -------
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

RUN groupadd --system --gid 10001 gateway \
    && useradd --system --uid 10001 --gid gateway --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin gateway

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
# Code is owned by root and read-only to the app user. Only docs/evals (where the
# eval CLI may write Markdown reports) is writable.
COPY app/ ./app/
RUN python -m compileall -q app \
    && mkdir -p docs/evals \
    && chown gateway:gateway docs/evals

USER 10001:10001

EXPOSE 8000

# Liveness only: /healthz checks no dependencies. The image has no curl, so the
# check uses Python. ECS ignores this and uses the same command from the task
# definition; Docker and Compose use it as is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

# Keep-alive longer than a load balancer's idle timeout (60 s on AWS ALB), so the
# server never closes a connection the balancer is about to reuse (a 502).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "75"]
