from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from app.api.deps import ConfigDep, DbSession, HADep
from app.core.config import app_version

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/info")
async def app_info() -> dict[str, str]:
    return {"service": "home-agent", "version": app_version()}


@router.get("/readyz")
async def readyz(session: DbSession, config: ConfigDep, ha: HADep) -> dict:
    checks: dict[str, bool] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:  # noqa: BLE001 - readiness must report, not raise
        checks["database"] = False

    checks["ha_connected"] = ha.connected
    checks["llm_configured"] = bool(config.llm_settings("primary").get("model_name"))

    if not all(checks.values()):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"status": "not_ready", "checks": checks},
        )
    return {"status": "ready", "checks": checks}
