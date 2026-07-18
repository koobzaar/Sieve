FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY promo_bot ./promo_bot
RUN pip install --no-cache-dir .

RUN addgroup -S sieve && adduser -S -G sieve -h /app sieve \
    && mkdir -p /state \
    && chown -R sieve:sieve /app /state

COPY --chown=sieve:sieve config ./config
USER sieve

VOLUME ["/state"]
ENTRYPOINT ["sieve"]
CMD ["--config", "/app/config/config.local.yaml", "run"]
