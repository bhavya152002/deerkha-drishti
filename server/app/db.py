"""
db.py -- SQLAlchemy engine + session for Postgres.

CONNECTION ENDPOINT
-------------------
DATABASE_URL points at Postgres running on this same host:

    postgresql://deerkha:<pw>@127.0.0.1:5432/deerkha

The database used to be a Supabase project in ap-northeast-1 (Tokyo) while the
devices, the operators and this server are all in India. Every statement
crossed ~130 ms of ocean, and assemble_config() issues 8 sequential statements
-- so a cold config assembly cost roughly a second. Locally that is ~1 ms.

Nothing in this codebase used Supabase beyond plain Postgres: no auth, no
storage, no realtime, no edge functions. So the project carried Supabase's
constraints (storage cap, connection ceiling, IPv6-only direct connections) in
exchange for features it never called.

POOL SIZING
-----------
The old pool was deliberately tiny (5+5) because connections were a scarce,
shared, per-project resource that the field devices were also drawing on. On a
local cluster with max_connections=100 they are cheap and the round-trip is
sub-millisecond, so the pool exists only to avoid reconnect churn.

pool_pre_ping stays: it is what turns a connection dropped by a Postgres
restart (an unattended-upgrade, say) into a transparent reconnect rather than a
500. pool_recycle is dropped -- it existed to stay ahead of Supavisor's idle
timeout, and a local server does not close idle connections behind our back.
"""

from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .settings import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
Base = declarative_base()


@contextmanager
def session_scope():
    """Transactional session context: commit on success, rollback on error."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    """FastAPI dependency -- yields a session, always closed."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
