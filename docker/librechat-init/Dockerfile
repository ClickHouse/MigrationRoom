# librechat-init: bootstraps the playground user and per-source agents.
#
# Built on mongo:7 (Debian) for `mongosh` because LibreChat v0.8.5 quirks force
# direct MongoDB writes for some agent fields. curl + jq are baked in so the
# Compose `command:` block doesn't need runtime apt-get.
FROM mongo:7

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl jq ca-certificates \
 && rm -rf /var/lib/apt/lists/*
