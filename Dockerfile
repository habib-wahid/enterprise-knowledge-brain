# Two-stage by ROLE, not by build optimisation.
#
#   builder  has git + credentials + network to the estate; produces knowledge.db
#   server   has neither; serves a database built elsewhere
#
# The split is the deployment story: source never has to leave the network it
# lives on, and the server never needs credentials for it.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-warm the embedding model into the image. Without this the first query
# on a fresh container downloads ~130 MB from huggingface.co — which hangs
# forever on an air-gapped host and is a mystery to whoever is watching.
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('BAAI/bge-small-en-v1.5')"

COPY eck/ ./eck/
COPY register/ ./register/
COPY processes/ ./processes/
COPY eck-cli ./
COPY ASSISTANT_GUIDE.md ./

ENV ECK_PROJECT_ROOT=/app \
    ECK_DB_PATH=/data/knowledge.db \
    ECK_CURATED_DIR=/data/curated

# ---------------------------------------------------------------- server
FROM base AS server
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/api/catalogue',timeout=4).status==200 else 1)"
CMD ["python", "-m", "eck.cli", "serve", "--host", "0.0.0.0", "--port", "8800"]

# ---------------------------------------------------------------- builder
FROM base AS builder
ENV ECK_SOURCES_ROOT=/app/sources
CMD ["sh", "-c", "python -m eck.cli doctor --profile builder && python -m eck.cli refresh --fetch"]

# ---------------------------------------------------------------- mcp
# CAP-8, shared-network transport. stdio has no separate image target — it
# runs as a foreground subprocess of whatever launches it (an IDE, an
# assistant's own config), never as a standalone container.
FROM base AS mcp
EXPOSE 8900
CMD ["sh", "-c", "python -m eck.cli mcp serve --transport streamable-http --host 0.0.0.0 --port 8900"]
