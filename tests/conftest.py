import os
import pytest

os.environ.setdefault("AIMO_DATA_DIR", "data")


@pytest.fixture(scope="session", autouse=True)
def tmp_db(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("db") / "aimo.db")
    os.environ["AIMO_DB"] = p
    os.environ["AIMO_DB_PATH"] = p
    return p


@pytest.fixture(scope="session")
def client(tmp_db):
    pytest.importorskip("aimo.app")
    from fastapi.testclient import TestClient
    from aimo.app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def agent_key():
    m = pytest.importorskip("aimo.mandates")
    for v in vars(m).values():
        if isinstance(v, dict):
            for k, t in v.items():
                if isinstance(k, str) and "hotel-aurora" in repr(t):
                    return k
    pytest.skip("no hotel-aurora key in aimo.mandates")
