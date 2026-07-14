ARG PORT=3535
ARG PROXY_CONTENT=TRUE
ARG SOCKS5

# Only set for local/direct access. When TLS is used, the API_URL is assumed to be the same as the frontend.
ARG API_URL

# It uses a reverse proxy to serve the frontend statically and proxy to backend
# from a single exposed port, expecting TLS termination to be handled at the
# edge by the given platform.
FROM docker.io/python:3.13 AS builder

ARG uv=/root/.local/bin/uv

# Install `uv` for faster package bootstrapping
ADD --chmod=755 https://astral.sh/uv/install.sh /install.sh
RUN /install.sh && rm /install.sh

RUN mkdir -p /app/.web
RUN mkdir -p /app/.venv
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

RUN $uv venv

# Install python app requirements and reflex in the container
COPY requirements.txt .
RUN $uv pip install --upgrade pip
RUN $uv pip install -r requirements.txt

# Install reflex helper utilities like bun/node
COPY rxconfig.py ./
RUN reflex init

# Copy local context to `/app` inside container (see .dockerignore)
COPY . .

ARG PORT API_URL PROXY_CONTENT SOCKS5
# Download other npm dependencies and compile frontend
RUN REFLEX_API_URL=${API_URL:-http://localhost:$PORT} reflex export --loglevel debug --frontend-only --no-zip && mv .web/build/client/* /srv/ && rm -rf .web


# Final image with only necessary files
FROM docker.io/python:3.13-slim

# Install Caddy and redis server inside image
RUN apt-get update -y && apt-get install -y caddy redis-server && rm -rf /var/lib/apt/lists/*

ARG PORT API_URL
ENV PATH="/app/.venv/bin:$PATH" PORT=$PORT REFLEX_API_URL=${API_URL:-http://localhost:$PORT} REDIS_URL=redis://localhost PYTHONUNBUFFERED=1 PROXY_CONTENT=${PROXY_CONTENT:-TRUE} SOCKS5=${SOCKS5:-""}

WORKDIR /app
COPY --from=builder /app /app
COPY --from=builder /srv /srv

# Needed until Reflex properly passes SIGTERM on backend.
STOPSIGNAL SIGKILL

EXPOSE $PORT

# Starting the backend.
CMD caddy start && \
  redis-server --daemonize yes && \
  exec reflex run --env prod --backend-only
