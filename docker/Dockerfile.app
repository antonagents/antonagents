# Antonagents API / orchestrator image.
#
# This process shells out to `docker run` for every task run, so the image ships
# the Docker *CLI* (not the engine) and, at runtime, mounts the host Docker
# socket (see docker-compose.yml) to launch sibling task containers on the host
# daemon. The heavy agent runtime lives in the separate task image
# (docker/Dockerfile.task), NOT here.
#
# Pinned to bookworm so the Docker apt repo (which lags newer Debian) resolves.
FROM python:3.12-slim-bookworm

# Docker CLI only — used to launch/inspect sibling task containers via the socket.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching. providers.json is a tracked, secret-free
# default (references env-var names, not values); operators can override it via a
# bind mount in compose.
COPY requirements.txt providers.json ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/

EXPOSE 8080
CMD ["python", "-m", "app.main"]
