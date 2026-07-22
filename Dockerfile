# Syntax is intentionally simple so Docker BuildKit is optional.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --gid 10001 indexer \
    && useradd --uid 10001 --gid indexer --create-home --home-dir /home/indexer --shell /usr/sbin/nologin indexer \
    && python -m venv /opt/venv

WORKDIR /app

COPY pyproject.toml requirements.in requirements.txt ./
RUN pip install --require-hashes -r requirements.txt

COPY src ./src
COPY config ./config
COPY abis ./abis
COPY migrations ./migrations
COPY scripts/entrypoint.sh /usr/local/bin/rpc-state-indexer-entrypoint
RUN pip install --no-deps --no-build-isolation . \
    && chmod 0555 /usr/local/bin/rpc-state-indexer-entrypoint \
    && chown -R indexer:indexer /app

USER indexer

EXPOSE 9090
ENTRYPOINT ["/usr/local/bin/rpc-state-indexer-entrypoint"]
CMD ["daemon"]
