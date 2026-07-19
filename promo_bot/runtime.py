from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from .config import AppConfig, env_secret, load_factory
from .evaluator import RetryableEvaluationError
from .models import Decision, Promotion
from .pipeline import PromotionPipeline
from .preference_bot import (
    PreferenceCommandProcessor,
    TelegramBotAPI,
    TelegramPreferenceBot,
)
from .preference_interpreter import create_gemini_preference_interpreter
from .preference_store import SQLitePreferenceStore
from .preferences import AtomicPreferenceProvider, PreferenceSnapshot
from .sources.pelando import PelandoSchemaError
from .store import SQLiteStateStore, StoreError

logger = logging.getLogger(__name__)


def resident_memory_bytes() -> int:
    cgroup = Path("/sys/fs/cgroup/memory.current")
    try:
        return int(cgroup.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pass
    status = Path("/proc/self/status")
    try:
        for line in status.read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


class Service:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.stop = asyncio.Event()
        self._failure_started: dict[str, float] = {}
        self._failure_alerted: set[str] = set()
        self.queue: asyncio.Queue[Promotion] = asyncio.Queue(maxsize=config.queue_capacity)
        self.store = SQLiteStateStore(
            config.state_path,
            retention_days=config.retention_days,
            retention_cap=config.retention_cap,
            corpus_limit=config.corpus_limit,
            retry_limit=config.retry_limit,
            retry_ttl_seconds=config.retry_ttl_seconds,
        )
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=10),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            follow_redirects=True,
            headers={"User-Agent": "sieve/1.0"},
        )
        preference_settings = config.preferences
        self.preference_store = SQLitePreferenceStore(
            self.store,
            max_entries=preference_settings.max_entries,
            max_operations=preference_settings.max_operations,
            max_state_bytes=preference_settings.max_state_bytes,
            confirmation_ttl_seconds=preference_settings.confirmation_ttl_seconds,
            outbox_capacity=preference_settings.queue_capacity,
            on_snapshot=self._preference_snapshot_changed,
        )
        initial_snapshot = self.preference_store.initialize(
            profile=config.profile,
            aliases=config.aliases,
            hard_rules=config.hard_rules,
        )
        self.preference_provider = AtomicPreferenceProvider(initial_snapshot)
        self.preference_store.provider = self.preference_provider
        self.store.ensure_alias_generation(dict(initial_snapshot.aliases))
        evaluator_factory = load_factory(config.evaluator_factory)
        sink_factory = load_factory(config.sink_factory)
        self.evaluator = evaluator_factory(config.evaluator, profile=config.profile, client=self.http)
        self.sink = sink_factory(config.sink, client=self.http)
        source_modes = {
            item.name: item.mode for item in config.sources if item.mode is not None
        }
        self.pipeline = PromotionPipeline(
            store=self.store,
            evaluator=self.evaluator,
            sink=self.sink,
            profile=config.profile,
            aliases=config.aliases,
            hard_rules=config.hard_rules,
            threshold=config.bm25_threshold,
            auto_forward_threshold=config.bm25_auto_forward_threshold,
            auto_forward_mode=config.bm25_auto_forward_mode,
            below_threshold_audit_rate=config.bm25_below_threshold_audit_rate,
            k1=config.bm25_k1,
            b=config.bm25_b,
            cold_start_documents=config.cold_start_documents,
            exceptional_temperature=config.exceptional_temperature,
            default_mode=config.mode,
            source_modes=source_modes,
            preference_provider=self.preference_provider,
        )
        self.preference_interpreter = None
        self.preference_bot = None
        self.preference_owner_id: int | None = None
        if preference_settings.enabled:
            owner_id = (
                preference_settings.owner_id
                if preference_settings.owner_id is not None
                else int(env_secret(preference_settings.owner_id_env))
            )
            owner_chat_id = (
                preference_settings.chat_id
                if preference_settings.chat_id is not None
                else int(env_secret(preference_settings.chat_id_env))
            )
            self.preference_owner_id = owner_id
            def language_provider() -> str:
                return self.preference_store.ui_language(owner_id)

            evaluator_language = getattr(self.evaluator, "set_language_provider", None)
            if callable(evaluator_language):
                evaluator_language(language_provider)
            sink_language = getattr(self.sink, "set_language_provider", None)
            if callable(sink_language):
                sink_language(language_provider)
            parser_settings = dict(config.evaluator)
            parser_settings.update(preference_settings.parser)
            parser_settings["max_operations"] = preference_settings.max_operations
            self.preference_interpreter = create_gemini_preference_interpreter(
                parser_settings, client=self.http
            )
            bot_api = TelegramBotAPI(
                token=env_secret(preference_settings.token_env),
                api_url=preference_settings.api_url,
                timeout_seconds=max(40, preference_settings.polling_timeout + 10),
            )
            processor = PreferenceCommandProcessor(
                store=self.preference_store,
                interpreter=self.preference_interpreter,
                owner_chat_id=owner_chat_id,
                owner_user_id=owner_id,
                rate_per_minute=preference_settings.rate_per_minute,
                rate_per_hour=preference_settings.rate_per_hour,
            )
            self.preference_bot = TelegramPreferenceBot(
                api=bot_api,
                processor=processor,
                store=self.preference_store,
                owner_chat_id=owner_chat_id,
                polling_timeout=preference_settings.polling_timeout,
                queue_capacity=preference_settings.queue_capacity,
            )
        self.sources = [
            load_factory(item.factory)(
                item.settings,
                name=item.name,
                http_client=self.http,
                health_reporter=self.report_health,
            )
            for item in config.sources
            if item.enabled
        ]

    def _preference_snapshot_changed(
        self,
        snapshot: PreferenceSnapshot,
        previous: PreferenceSnapshot | None,
    ) -> None:
        if previous is None or dict(snapshot.aliases) != dict(previous.aliases):
            self.store.start_alias_rebuild(dict(snapshot.aliases))

    def _pick(self, english: str, portuguese: str) -> str:
        if self.preference_owner_id is not None and self.preference_store.ui_language(
            self.preference_owner_id
        ) == "pt-BR":
            return portuguese
        return english

    async def report_health(self, name: str, error: Exception | None) -> None:
        try:
            failures = self.store.record_health(name, None if error is None else str(error))
        except Exception as store_error:
            logger.critical(
                "database_health_failure",
                extra={"event": "database_health_failure", "error": str(store_error)},
            )
            with suppress(Exception):
                await self.sink.alert(
                    self._pick("Database", "Banco de dados")
                    + f": {type(store_error).__name__}: {str(store_error)[:350]}"
                )
            return
        if error is None:
            self._failure_started.pop(name, None)
            self._failure_alerted.discard(name)
            return
        started = self._failure_started.setdefault(name, time.monotonic())
        elapsed = time.monotonic() - started
        logger.error(
            "component_failure",
            extra={"event": "component_failure", "component": name, "failures": failures, "error": str(error)},
        )
        immediate = isinstance(error, (PelandoSchemaError, StoreError))
        persistent_llm = (
            name == "llm" and elapsed >= self.config.llm_outage_alert_seconds
        )
        repeated_component = (
            name != "llm" and failures >= self.config.failure_alert_threshold
        )
        should_alert = immediate or persistent_llm or repeated_component
        if should_alert and name not in self._failure_alerted:
            self._failure_alerted.add(name)
            with suppress(Exception):
                await self.sink.alert(
                    f"{name}: {type(error).__name__}: {str(error)[:350]} "
                    + self._pick(
                        f"(consecutive failures: {failures})",
                        f"(falhas consecutivas: {failures})",
                    )
                )

    async def emit(self, promotion: Promotion) -> None:
        await self.queue.put(promotion)

    async def _pipeline_worker(self) -> None:
        while not self.stop.is_set() or not self.queue.empty():
            try:
                promotion = await asyncio.wait_for(self.queue.get(), timeout=1)
            except TimeoutError:
                continue
            try:
                result = await self.pipeline.process(promotion)
                if result.decision == Decision.RETRY:
                    await self.report_health("llm", RuntimeError(result.reason))
                elif result.reason.startswith("llm_permanent_error:"):
                    await self.report_health("llm", RuntimeError(result.reason))
                elif result.stage == "llm":
                    await self.report_health("llm", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.report_health("pipeline", exc)
            finally:
                self.queue.task_done()

    async def _retry_worker(self) -> None:
        while not self.stop.is_set():
            for job in self.store.due_retries(limit=10):
                try:
                    await self.pipeline.process_retry(job.promotion)
                except RetryableEvaluationError as exc:
                    alive = self.store.reschedule_retry(job.id, str(exc))
                    await self.report_health("llm", exc)
                    if not alive:
                        logger.error(
                            "llm_retry_expired",
                            extra={"event": "llm_retry_expired", "promotion_id": job.promotion.id},
                        )
                except Exception as exc:
                    self.store.complete_retry(job.id)
                    await self.report_health("llm_retry", exc)
                else:
                    self.store.complete_retry(job.id)
                    await self.report_health("llm", None)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5)
            except TimeoutError:
                pass

    async def _maintenance(self) -> None:
        while not self.stop.is_set():
            try:
                removed = self.store.prune()
                removed.update(
                    {
                        f"preferences_{key}": value
                        for key, value in self.preference_store.prune_transient().items()
                    }
                )
                self.store.record_health("runtime")
                logger.info(
                    "maintenance",
                    extra={"event": "maintenance", "queue_size": self.queue.qsize(), "removed": removed},
                )
            except Exception as exc:
                await self.report_health("database", exc)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=60)
            except TimeoutError:
                pass

    async def _alias_rebuild_worker(self) -> None:
        while not self.stop.is_set():
            try:
                result = self.store.rebuild_alias_batch(250)
                if result["processed"]:
                    await asyncio.sleep(0)
                    continue
            except Exception as exc:
                await self.report_health("alias_rebuild", exc)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5)
            except TimeoutError:
                pass

    async def _memory_monitor(self) -> None:
        threshold = self.config.memory_limit_mb * 1024 * 1024
        while not self.stop.is_set():
            used = resident_memory_bytes()
            if used and used >= threshold:
                logger.critical(
                    "memory_pressure",
                    extra={"event": "memory_pressure", "rss_bytes": used, "threshold_bytes": threshold},
                )
                with suppress(Exception):
                    await self.sink.alert(
                        self._pick(
                            f"Memory pressure: {used / 1024 / 1024:.1f} MB; preventive restart.",
                            f"Pressão de memória: {used / 1024 / 1024:.1f} MB; reinício preventivo.",
                        )
                    )
                self.store.flush()
                self.stop.set()
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=10)
            except TimeoutError:
                pass

    async def run(self) -> None:
        if not self.sources:
            raise RuntimeError("no enabled promotion sources")
        if self.preference_bot is not None:
            await self.preference_bot.drain_outbox()
            await self.preference_bot.check_webhook()
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(signal_name, self.stop.set)
        tasks = [
            asyncio.create_task(self._pipeline_worker(), name="pipeline"),
            asyncio.create_task(self._retry_worker(), name="retry"),
            asyncio.create_task(self._maintenance(), name="maintenance"),
            asyncio.create_task(self._memory_monitor(), name="memory"),
            asyncio.create_task(self._alias_rebuild_worker(), name="alias-rebuild"),
            *[
                asyncio.create_task(source.run(self.emit, self.stop), name=f"source:{source.name}")
                for source in self.sources
            ],
        ]
        if self.preference_bot is not None:
            tasks.append(
                asyncio.create_task(
                    self.preference_bot.run(self.stop), name="preference-bot"
                )
            )
        logger.info("service_started", extra={"event": "service_started", "sources": len(self.sources)})
        try:
            while not self.stop.is_set():
                terminal = [task for task in tasks if task.done() and task.exception()]
                if terminal:
                    raise terminal[0].exception()  # type: ignore[misc]
                await asyncio.sleep(0.5)
            await self.queue.join()
        finally:
            self.stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for source in self.sources:
                with suppress(Exception):
                    await source.close()
            with suppress(Exception):
                await self.evaluator.close()
            with suppress(Exception):
                await self.sink.close()
            if self.preference_interpreter is not None:
                with suppress(Exception):
                    await self.preference_interpreter.close()
            if self.preference_bot is not None:
                with suppress(Exception):
                    await self.preference_bot.close()
            await self.http.aclose()
            self.preference_store.close()
            self.store.close()
            logger.info("service_stopped", extra={"event": "service_stopped"})


async def run_service(config: AppConfig) -> None:
    await Service(config).run()
