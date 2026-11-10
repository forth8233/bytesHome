import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent.context import ContextBuilder
from app.agent.proposals import ProposalService
from app.agent.reasoner import Reasoner
from app.api.routers import (
    audit,
    auth,
    channels,
    chat,
    config_router,
    events,
    fastpath,
    health,
    members,
    memories,
    proposals,
    scenes,
    sse,
    stats,
    timers,
    voice,
)
from app.channels.manager import ChannelManager, ChannelNotifier
from app.chat.service import ChatService
from app.core.config import app_version, get_settings
from app.core.logging import configure_logging
from app.db.session import SessionFactory, engine, init_db
from app.events.bus import EventBus
from app.events.log_sink import EventLogSink
from app.events.pipeline import EventPipeline
from app.fastpath.engine import FastPathEngine
from app.fastpath.scheduler import ScheduleRunner
from app.ha.adapter import HAAdapter
from app.llm.client import LLMClient
from app.notify.notifier import HANotifier, MultiNotifier
from app.notify.sse import SSEHub
from app.safety.guard import ActionGuard
from app.services.bootstrap import bootstrap
from app.services.config_service import ConfigService

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.auto_create_schema:
        await init_db()

    config = ConfigService(SessionFactory)
    await config.load()
    await bootstrap(config)

    adapter = HAAdapter(config, call_timeout_seconds=settings.ha_call_timeout_seconds)
    bus = EventBus()
    await bus.start()
    pipeline = EventPipeline(config, adapter.registry, bus)
    await pipeline.start()
    log_sink = EventLogSink(SessionFactory, bus.subscribe("persist"))
    await log_sink.start()
    adapter.event_sink = pipeline.handle
    await adapter.start()

    sse_hub = SSEHub()
    guard = ActionGuard(config, SessionFactory)
    proposal_service = ProposalService(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=guard,
        sse=sse_hub,
    )
    builder = ContextBuilder(
        session_factory=SessionFactory,
        cache=adapter.cache,
        registry=adapter.registry,
    )
    fast_path = FastPathEngine(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=guard,
        proposals=proposal_service,
        sse=sse_hub,
    )
    await fast_path.reload()
    chat_service = ChatService(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=guard,
        proposals=proposal_service,
        client=LLMClient(config),
        sse=sse_hub,
        fast_path=fast_path,
        timeout_seconds=settings.chat_timeout_seconds,
        slow_path_limit_per_minute=settings.chat_slow_path_limit_per_minute,
    )
    channel_manager = ChannelManager(
        config=config,
        session_factory=SessionFactory,
        chat_service=chat_service,
        proposals=proposal_service,
    )
    channel_notifier = ChannelNotifier(
        session_factory=SessionFactory, manager=channel_manager, config=config
    )
    reasoner = Reasoner(
        config=config,
        queue=bus.subscribe("reasoner"),
        builder=builder,
        client=LLMClient(config),
        guard=guard,
        proposals=proposal_service,
        session_factory=SessionFactory,
        sse=sse_hub,
        notifier=MultiNotifier(HANotifier(config, adapter), channel_notifier),
    )
    await reasoner.start()
    await channel_manager.start()
    sweeper = asyncio.create_task(proposal_service.run_sweeper(), name="proposal-sweeper")
    schedule_runner = ScheduleRunner(
        session_factory=SessionFactory,
        adapter=adapter,
        guard=guard,
        channel_notifier=channel_notifier,
    )
    sched_task = asyncio.create_task(schedule_runner.run(), name="scheduled-task-runner")

    app.state.config_service = config
    app.state.ha_adapter = adapter
    app.state.event_bus = bus
    app.state.event_pipeline = pipeline
    app.state.event_log_sink = log_sink
    app.state.sse_hub = sse_hub
    app.state.action_guard = guard
    app.state.proposal_service = proposal_service
    app.state.chat_service = chat_service
    app.state.fast_path = fast_path
    app.state.reasoner = reasoner
    app.state.channel_manager = channel_manager
    app.state.schedule_runner = schedule_runner
    logger.info("app.started", auto_create_schema=settings.auto_create_schema)
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass
        sched_task.cancel()
        try:
            await sched_task
        except asyncio.CancelledError:
            pass
        await channel_manager.stop()
        await reasoner.stop()
        await adapter.stop()
        adapter.event_sink = None
        await pipeline.stop()
        await log_sink.stop()
        await bus.stop()
        await engine.dispose()
        logger.info("app.stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title="Home Agent", version=app_version(), lifespan=lifespan)

    if settings.cors_origin_list:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid4().hex
        structlog.contextvars.bind_contextvars(
            request_id=request_id, path=request.url.path, method=request.method
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["x-request-id"] = request_id
        return response

    @application.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        detail = None if isinstance(exc.detail, str) else exc.detail
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code, "message": message, "detail": detail},
        )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": 422, "message": "validation error", "detail": exc.errors()},
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("app.unhandled_error", error=str(exc))
        return JSONResponse(
            status_code=500, content={"code": 500, "message": "internal server error", "detail": None}
        )

    application.include_router(health.router)
    application.include_router(auth.router)
    application.include_router(members.router)
    application.include_router(config_router.router)
    application.include_router(events.router)
    application.include_router(memories.router)
    application.include_router(audit.router)
    application.include_router(timers.router)
    application.include_router(chat.router)
    application.include_router(channels.router)
    application.include_router(fastpath.router)
    application.include_router(voice.router)
    application.include_router(scenes.router)
    application.include_router(proposals.router)
    application.include_router(sse.router)
    application.include_router(stats.router)

    @application.get("/")
    async def root() -> dict[str, str]:
        return {"service": "home-agent", "version": app_version()}

    return application


app = create_app()
