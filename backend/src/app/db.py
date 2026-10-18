from contextlib import contextmanager
from pathlib import Path

from sqlmodel import Session as DBSession
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from app.config import get_settings

_url = get_settings().database_url
if _url.startswith("sqlite:///"):
    Path(_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(_url, connect_args={"check_same_thread": False} if _url.startswith("sqlite") else {})


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all does not alter existing tables: add new nullable columns (no migration tool yet)."""
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            have = {c["name"] for c in inspect(conn).get_columns(table.name)}
            for col in table.columns:
                if col.name not in have and col.nullable:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'))


@contextmanager
def db():
    with DBSession(engine, expire_on_commit=False) as s:
        yield s
