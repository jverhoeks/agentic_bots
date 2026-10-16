from contextlib import contextmanager
from pathlib import Path

from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine

from app.config import get_settings

_url = get_settings().database_url
if _url.startswith("sqlite:///"):
    Path(_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(_url, connect_args={"check_same_thread": False} if _url.startswith("sqlite") else {})


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    SQLModel.metadata.create_all(engine)


@contextmanager
def db():
    with DBSession(engine, expire_on_commit=False) as s:
        yield s
