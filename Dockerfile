FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SIEVE_CONFIG=/app/config/config.yaml \
    SIEVE_LOG_LEVEL=INFO

WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml README.md ./
COPY promo_bot ./promo_bot
RUN pip install --no-cache-dir --no-deps .

RUN addgroup -S sieve && adduser -S -G sieve -h /app sieve \
    && mkdir -p /state \
    && chown -R sieve:sieve /app /state

COPY --chown=sieve:sieve config/config.yaml ./config/config.yaml
USER sieve

VOLUME ["/state"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["sieve", "health"]
ENTRYPOINT ["sieve"]
CMD ["run"]
