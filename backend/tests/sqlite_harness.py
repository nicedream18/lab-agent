"""Small, isolated database harness for service and Agent unit tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from evals.offline_env import configure_offline

configure_offline()

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def entities(db_session):
    from app.models.lab import Lab
    from app.models.user import User

    user = User(username="unit", password="x", name="Unit", role="student", status=1)
    lab = Lab(name="计算机实验室", location="201", capacity=20, open_time="08:00", close_time="20:00", status=1)
    db_session.add_all([user, lab])
    db_session.commit()
    return user, lab
