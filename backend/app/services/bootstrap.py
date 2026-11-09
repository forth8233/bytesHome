import secrets
from pathlib import Path

import structlog
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Member
from app.db.session import SessionFactory
from app.services.config_service import ConfigService

logger = structlog.get_logger(__name__)


async def bootstrap(config: ConfigService) -> None:
    await config.seed_defaults()
    await _ensure_admin()


async def _ensure_admin() -> None:
    settings = get_settings()
    async with SessionFactory() as session:
        existing = await session.scalar(select(func.count()).select_from(Member))
        if existing:
            return

        generated = not settings.initial_admin_password
        password = settings.initial_admin_password or secrets.token_urlsafe(12)
        session.add(
            Member(
                username=settings.initial_admin_username,
                display_name="Administrator",
                password_hash=hash_password(password),
                role="admin",
            )
        )
        await session.commit()

    if generated:
        # Written to a file rather than logged: the log redactor masks anything named "password".
        path = Path("./data/initial_admin_password.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(password, encoding="utf-8")
        logger.warning(
            "bootstrap.admin_created",
            username=settings.initial_admin_username,
            credentials_file=str(path.resolve()),
            note="change this password after first login, then delete the file",
        )
    else:
        logger.warning(
            "bootstrap.admin_created",
            username=settings.initial_admin_username,
            password_source="INITIAL_ADMIN_PASSWORD",
        )
