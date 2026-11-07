from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backitup.config import get_settings


@lru_cache
def factory():
    engine = create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope():
    with factory()() as session:
        with session.begin():
            yield session


def get_session():
    with factory()() as session:
        yield session
