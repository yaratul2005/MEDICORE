import os
from pathlib import Path
from typing import Generator
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine
from medicore.core.config import settings

# Ensure target directory exists for SQLite DB
db_file = settings.db_path
if db_file and not db_file.startswith(":memory:"):
    parent_dir = Path(db_file).parent
    parent_dir.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False}
engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args=connect_args
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def init_db():
    """Create tables if they don't exist (used for fast tests or pre-migrations)"""
    SQLModel.metadata.create_all(engine)
