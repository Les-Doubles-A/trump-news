"""Moteur SQLite, initialisation du schéma, sessions transactionnelles et accès aux réglages."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Base, LogEntry, Setting

SCHEMA_VERSION = "1"


def create_db_engine(db_path: Path | str) -> Engine:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{Path(db_path).as_posix()}"
    else:
        url = "sqlite://"
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def init_db(engine: Engine) -> sessionmaker[Session]:
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, future=True)
    if get_setting(factory, "schema_version") is None:
        set_setting(factory, "schema_version", SCHEMA_VERSION)
    return factory


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_setting(factory: sessionmaker[Session], key: str, default: str | None = None) -> str | None:
    with session_scope(factory) as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(factory: sessionmaker[Session], key: str, value: str | None) -> None:
    with session_scope(factory) as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))


class DBLogHandler(logging.Handler):
    """Écrit les logs INFO+ dans la table `logs` (page Logs de l'UI). Ne lève jamais d'exception."""

    def __init__(self, factory: sessionmaker[Session], level: int = logging.INFO) -> None:
        super().__init__(level)
        self._factory = factory
        self._busy = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._busy or record.name.startswith("sqlalchemy"):
            return
        self._busy = True
        try:
            with session_scope(self._factory) as s:
                s.add(
                    LogEntry(
                        level=record.levelname,
                        logger=record.name,
                        message=record.getMessage()[:4000],
                        run_id=getattr(record, "run_id", None),
                    )
                )
        except Exception:
            pass
        finally:
            self._busy = False


def latest_logs(factory: sessionmaker[Session], limit: int = 50) -> list[LogEntry]:
    with session_scope(factory) as s:
        return list(s.scalars(select(LogEntry).order_by(LogEntry.id.desc()).limit(limit)))
