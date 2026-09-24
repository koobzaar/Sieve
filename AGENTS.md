# Repository Guidelines

## Project Structure & Module Organization

`promo_bot/` contains the application: `pipeline.py` coordinates filtering and delivery, `sources/` holds Telegram and Pelando ingestion, and `locales/` contains English and Brazilian Portuguese text. Configuration lives in `config/config.yaml`; deployment files are `Dockerfile` and `compose*.yaml`. Tests mirror application features under `tests/`, with saved HTML in `tests/fixtures/` and Docker system tests in `tests/system/`. `fixtures/` holds a sample labeled replay file, while `assets/` contains README media.

## Build, Test, and Development Commands

- `python -m pip install -e ".[test,dev]"` installs the package, tests and lint tooling (Python 3.12+).
- `python -m ruff check promo_bot tests` checks undefined names, unused imports and syntax errors.
- `python -m pytest -m "not soak"` runs the regular local suite without long soak tests.
- `python -m pytest` runs the full locally enabled suite.
- `docker compose run --rm sieve validate-config --runtime` checks settings, enabled sources, factories and required environment variables without calling external services.
- `docker compose up -d --build` builds and starts the service; `docker compose logs -f sieve` follows its logs.

For local execution without Docker, use `sieve --config config/config.yaml run` after setting `state.path` and `session_path` to writable local paths.

## Coding Style & Naming Conventions

Follow the existing Python style: four-space indentation, `snake_case` modules/functions, `PascalCase` classes, and type annotations where neighboring code uses them. Keep source adapters in `promo_bot/sources/` and user-facing strings in both locale YAML files. Follow `.editorconfig` and `.gitattributes`: UTF-8, LF endings and spaces; YAML uses two-space indentation. Ruff's `E9` and `F` checks are required; avoid unrelated formatting rewrites.

## Testing Guidelines

Use pytest and pytest-asyncio. Name files `test_<feature>.py` and tests `test_<behavior>`. Add deterministic fixtures or mocked transports for new behavior; regular tests should not require live services. Mark long tests `soak`, live Gemini tests `contract`, and Docker black-box tests `system`. Run opt-in gates with the environment flags documented in `README.md` (`SIEVE_RUN_GEMINI_CONTRACT` and `SIEVE_RUN_SYSTEM`). No coverage threshold is configured.

## Commit & Pull Request Guidelines

Recent commits use short imperative subjects prefixed with `feat:`, `fix:`, `chore:`, or `ops:`; follow that pattern and keep commits scoped. In pull requests, explain behavior and configuration changes, link relevant issues, and list test commands and results. Include a screenshot or sample Telegram output when presentation changes.

## Security & Configuration

Copy `.env.example` to `.env` for local secrets. Never commit `.env`, Telegram session files, or SQLite state. Keep credentials in environment variables referenced by `config/config.yaml`.

## Configuration and Docker Conventions

- Keep the complete advanced configuration in `config/config.yaml`. Expose common deployment settings through whole-value `${SIEVE_NAME:-default}` references and document each in `.env.example`. The default determines the environment value's type; string defaults remain strings (including `off`). Do not add another settings loader or configuration inheritance.
- Docker Compose loads `.env` through `env_file`; Python reads process environment only. Local runs must export variables explicitly. Use `SIEVE_CONFIG` for the CLI config path, `SIEVE_CONFIG_FILE` for the host Compose mount, and `SIEVE_LOG_LEVEL` for logging. CLI flags take precedence.
- Keep credentials as `*_env` references, validate their presence only for runtime checks, and never print their values. Validate numeric types/ranges and booleans in `load_config`; report `ConfigurationError` with the setting name instead of silently coercing invalid values.
- Run containers as non-root with a read-only root filesystem. All persistent writes belong under `/state`; temporary files belong under `/tmp`. Keep the stock config inside the image and mount local configuration read-only. Do not bake local configs, credentials, sessions or databases into the image.
- Keep production and emulator Compose runtime restrictions consistent. Docker's stop grace period must exceed `runtime.shutdown_timeout_seconds`; the in-app memory limit must stay below the container limit. Include portable runtime dependencies such as `tzdata` explicitly.
- Keep runtime dependencies in `pyproject.toml` and commit the generated `requirements.lock`. Regenerate with `uv pip compile pyproject.toml --python-version 3.12 --universal --generate-hashes --no-annotate --no-header --output-file requirements.lock` when dependencies change. Docker installs this lock with hash verification before copying source; validate dependency updates with the Docker gate.
- Update both `README.md` and `README.pt-BR.md` when setup, configuration or debug commands change. Validate Compose with `docker compose config --quiet` so credentials are not printed.
- Deployment helpers must derive the repository directory and Docker context or accept explicit environment overrides; do not hardcode machine-specific paths/contexts. Validate startup settings before recreating services and do not prune unrelated Docker resources.

## Performance, Lifecycle and Verification

- Preserve BM25 scores and per-user alias isolation when optimizing. Count document frequency once per document. Stream corpus rebuilds, keep caches bounded, key them by query and alias content, and maintain cached aggregates during insertion, eviction and transaction rollback. The runtime uses one state-store writer per database; do not share a state volume across bot instances.
- Supervise long-lived workers, treat unexpected exit/cancellation as a service failure, cancel and await child tasks, and bound shutdown queue draining. Durable delivery/retry queues must survive restart. Close resources even if asynchronous startup fails.
- Remove helpers only after checking Python references, YAML factory paths, protocols and framework callbacks. Static unused-code reports alone do not prove a dynamically invoked function is dead.
- Bug fixes need deterministic regression tests. Run the regular suite and Ruff; changes to Docker, dependencies, startup, shutdown or delivery also require the isolated Docker gate (`SIEVE_RUN_SYSTEM=1 python -m pytest -m system`). Use dummy credentials and the local emulator. Run bounded-memory soak checks with `RUN_SOAK=1 python -m pytest -m soak` for corpus/cache changes. Live provider tests require separate explicit authorization.
- Query current library documentation through Context7 MCP before changing library-specific behavior.
- Record any new lasting development rule in this file as part of the same change.
