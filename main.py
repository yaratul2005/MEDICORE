import logging
from contextlib import asynccontextmanager
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from medicore.core.config import settings
from medicore.core.database import init_db
from medicore.core.registry import registry
from medicore.core.routes import core_router
from medicore.core.settings import settings_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("medicore")

# Background scheduler for hospital background jobs
scheduler = AsyncIOScheduler()


def heartbeat_job():
    logger.debug("MediCore system heartbeat: operational.")


def appointment_reminders_job():
    try:
        from sqlmodel import Session
        from medicore.core.database import engine
        from medicore.modules.appointments.service import check_and_send_upcoming_reminders
        with Session(engine) as session:
            check_and_send_upcoming_reminders(session)
    except Exception as e:
        logger.error(f"Error checking appointment reminders: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.app_name} v{settings.app_version} [{settings.env}]...")
    # 1. Initialize DB tables
    init_db()

    # 2. Seed default system settings
    settings_registry.init_defaults()

    # 3. Auto-discover all modules in medicore.modules
    registry.auto_discover("medicore.modules")

    # 4. Attach active module routers
    registry.attach_routers(app)

    # 5. Start background scheduler
    scheduler.add_job(heartbeat_job, "interval", minutes=15)
    scheduler.add_job(appointment_reminders_job, "interval", hours=1)
    scheduler.start()

    logger.info("MediCore core services & plugin modules ready.")
    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("MediCore services shutdown cleanly.")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

# Static files
static_dir = Path(__file__).resolve().parent / "medicore" / "ui" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Core routes (settings, audit, palette, preferences, redirect root)
app.include_router(core_router)
