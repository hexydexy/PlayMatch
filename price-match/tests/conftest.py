import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config, db, service


@pytest.fixture(autouse=True)
def _no_request_delay(monkeypatch):
    monkeypatch.setattr(config, "REQUEST_DELAY", 0)


@pytest.fixture()
def db_session():
    """A fresh in-memory database with the three stores seeded."""
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from app import models  # noqa: F401  (registers tables)
    db.Base.metadata.create_all(eng)
    with sessionmaker(bind=eng, expire_on_commit=False)() as sess:
        service.seed_stores(sess)
        yield sess


@pytest.fixture()
def api(db_session):
    """A TestClient whose app reads and writes the same database as `db_session`."""
    from fastapi.testclient import TestClient

    from app import main

    Sess = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)

    def override():
        with Sess() as x:
            yield x

    main.app.dependency_overrides[main.get_session] = override
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
