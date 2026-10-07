"""Database engine and session factory.

The app creates one engine at startup (see `app.main.create_app`) and stores the session
factory on `app.state`. Request handlers get a request-scoped session; background jobs
open their own short-lived sessions from the same factory.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def create_db_engine(database_url: str) -> Engine:
    # pool_pre_ping checks a pooled connection before use, so a restarted database
    # does not cause errors on the first request afterwards.
    # hide_parameters keeps query values (recipient names, emails) out of logged errors.
    return create_engine(database_url, pool_pre_ping=True, hide_parameters=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False keeps loaded attributes readable after commit
    # (for example, building the response from a job we just created).
    return sessionmaker(bind=engine, expire_on_commit=False)
