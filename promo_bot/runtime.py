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
from .delivery import TelegramDeliveryWorker
from .evaluator import RetryableEvaluationError
from .models import Decision, Promotion
from .pipeline import MultiUserPromotionPipeline, PromotionPipeline
from .preference_bot import (
    MultiUserCommandProcessor,
    TelegramBotAPI,
    TelegramPreferenceBot,
)
from .preference_interpreter import create_gemini_preference_interpreter
from .preference_store import SQLitePreferenceStore
from .preferences import AtomicPreferenceProvider, PreferenceSnapshot
from .sources.pelando import PelandoSchemaError
from .store import SQLiteStateStore, StoreError
from .telegram_formatter import TelegramFormatter

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
            headers={"User-Agent": "sieve/1.1.0-beta.1"},
        )
        preference_settings = config.preferences
        if preference_settings.enabled:
            admin_telegram_user_id = int(
                env_secret(
                    preference_settings.admin_telegram_user_id_env
                )
            )
            administrator = self.store.bootstrap_admin(
                telegram_user_id=admin_telegram_user_id,
                telegram_chat_id=admin_telegram_user_id,
            )
        else:
            administrator = self.store.ensure_legacy_admin()
        self.preference_store = SQLitePreferenceStore(
            self.store,
            user_id=administrator.id,
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
        self.preference_stores: dict[str, SQLitePreferenceStore] = {
            administrator.id: self.preference_store
        }
        evaluator_factory = load_factory(config.evaluator_factory)
        sink_factory = load_factory(config.sink_factory)
        self.evaluator = evaluator_factory(config.evaluator, profile=config.profile, client=self.http)
        self.sink = sink_factory(config.sink, client=self.http)
        alert_destination = getattr(self.sink, "set_alert_destination", None)
        if callable(alert_destination):
            alert_destination(administrator.telegram_chat_id)

        def preference_store_for(account: Any) -> SQLitePreferenceStore:
            existing = self.preference_stores.get(account.id)
            if existing is not None:
                return existing
            scoped = SQLitePreferenceStore(
                self.store,
                user_id=account.id,
                max_entries=preference_settings.max_entries,
                max_operations=preference_settings.max_operations,
                max_state_bytes=preference_settings.max_state_bytes,
                confirmation_ttl_seconds=preference_settings.confirmation_ttl_seconds,
                outbox_capacity=preference_settings.queue_capacity,
                on_snapshot=self._preference_snapshot_changed,
            )
            try:
                scoped.current_snapshot()
            except Exception:
                scoped.initialize(profile="", aliases={}, hard_rules=())
            self.preference_stores[account.id] = scoped
            return scoped

        def pipeline_for(
            account: Any, provider: AtomicPreferenceProvider
        ) -> PromotionPipeline:
            return PromotionPipeline(
                store=self.store,
                evaluator=self.evaluator,
                sink=self.sink,
                profile="",
                aliases={},
                hard_rules=(),
                gemini_evaluation_enabled=config.gemini_evaluation_enabled,
                threshold=config.bm25_threshold,
                auto_forward_threshold=config.bm25_auto_forward_threshold,
                auto_forward_mode=config.bm25_auto_forward_mode,
                below_threshold_audit_rate=config.bm25_below_threshold_audit_rate,
                k1=config.bm25_k1,
                b=config.bm25_b,
                cold_start_documents=config.cold_start_documents,
                exceptional_temperature=config.exceptional_temperature,
                preference_provider=provider,
            )

        self.pipeline = MultiUserPromotionPipeline(
            store=self.store,
            pipeline_factory=pipeline_for,
            preference_store_factory=preference_store_for,
        )
        self.delivery_worker = TelegramDeliveryWorker(self.store, self.sink)
        self.preference_interpreter = None
        self.preference_bot = None
        self.preference_owner_id: int | None = administrator.telegram_user_id
        if preference_settings.enabled:
            def language_provider() -> str:
                return self.preference_store.ui_language(
                    administrator.telegram_user_id
                )

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
            processor = MultiUserCommandProcessor(
                state=self.store,
                interpreter=self.preference_interpreter,
                admin_store=self.preference_store,
                max_users=preference_settings.max_users,
                rate_per_minute=preference_settings.rate_per_minute,
                rate_per_hour=preference_settings.rate_per_hour,
            )
            self.preference_bot = TelegramPreferenceBot(
                api=bot_api,
                processor=processor,
                store=self.preference_store,
                owner_chat_id=administrator.telegram_chat_id,
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

    def _alert_text(self, key: str, **values: object) -> str:
        preference_store = getattr(
            self, "preference_store", None
        )
        owner_id = getattr(self, "preference_owner_id", None)
        language = (
            preference_store.ui_language(owner_id)
            if preference_store is not None
            and owner_id is not None
            else "en"
        )
        return TelegramFormatter(language).t(key, **values)

    async def report_health(self, name: str, error: Exception | None) -> None:
        try:
            failures = self.store.record_health(name, None if error is None else str(error))
        except Exception as store_error:
            logger.exception(
                "database_health_failure",
                extra={
                    "event": "database_health_failure",
                    "component": name,
                    "error_type": type(store_error).__name__,
                    "error": str(store_error)[:500],
                },
            )
            with suppress(Exception):
                await self.sink.alert(
                    self._alert_text(
                        "alert.database_failure",
                        error_type=type(store_error).__name__,
                    )
                )
            return
        if error is None:
            self._failure_started.pop(name, None)
            self._failure_alerted.discard(name)
            return
        started = self._failure_started.setdefault(name, time.monotonic())
        elapsed = time.monotonic() - started
        schema_failure = isinstance(error, PelandoSchemaError)
        immediate = isinstance(error, StoreError)
        persistent_llm = (
            name == "llm" and elapsed >= self.config.llm_outage_alert_seconds
        )
        repeated_component = (
            name != "llm" and failures >= self.config.failure_alert_threshold
        )
        should_alert = not schema_failure and (
            immediate or persistent_llm or repeated_component
        )
        alert_will_send = should_alert and name not in self._failure_alerted
        logger.error(
            "component_failure",
            extra={
                "event": "component_failure",
                "component": name,
                "failures": failures,
                "error_type": type(error).__name__,
                "error": str(error)[:500],
                "failure_duration_seconds": round(elapsed, 3),
                "schema_failure": schema_failure,
                "immediate_alert_class": immediate,
                "alert_eligible": should_alert,
                "alert_will_send": alert_will_send,
                "failure_alert_threshold": self.config.failure_alert_threshold,
                "llm_outage_alert_seconds": self.config.llm_outage_alert_seconds,
            },
        )
        if alert_will_send:
            self._failure_alerted.add(name)
            with suppress(Exception):
                await self.sink.alert(
                    self._alert_text(
                        "alert.component_failure",
                        component=name,
                        count=failures,
                        error_type=type(error).__name__,
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
                results = await self.pipeline.process(promotion)
                for result in results.values():
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
                    await self.pipeline.process_retry(job.user_id, job.promotion)
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

    async def _delivery_worker(self) -> None:
        while not self.stop.is_set():
            try:
                await self.delivery_worker.drain_once(limit=20)
            except Exception as exc:
                await self.report_health("delivery", exc)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1)
            except TimeoutError:
                pass

    async def _maintenance(self) -> None:
        while not self.stop.is_set():
            try:
                removed = self.store.prune()
                for user_id, preference_store in self.preference_stores.items():
                    removed.update(
                        {
                            f"preferences_{user_id}_{key}": value
                            for key, value in preference_store.prune_transient().items()
                        }
                    )
                self.store.record_health("runtime")
                operational = self.store.operational_health(
                    queue_depth=self.queue.qsize(),
                    preference_queue_depth=(
                        self.preference_bot.queue.qsize()
                        if self.preference_bot is not None
                        else 0
                    ),
                    cold_start_documents=self.config.cold_start_documents,
                )
                logger.info(
                    "maintenance",
                    extra={
                        "event": "maintenance",
                        "queue_size": self.queue.qsize(),
                        "removed": removed,
                        "operational": operational,
                    },
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
                        self._alert_text(
                            "alert.memory_pressure",
                            memory_mb=f"{used / 1024 / 1024:.1f}",
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
            asyncio.create_task(self._delivery_worker(), name="delivery"),
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
