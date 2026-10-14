import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_pulse.db")
if os.path.exists("./test_pulse.db"):
    os.remove("./test_pulse.db")

import pytest_asyncio

from app.database import init_db, get_session

_initialized = False


@pytest_asyncio.fixture
async def db():
    # One shared engine/db file for the whole session (recreating the file
    # per test confuses SQLAlchemy's pooled connections into "readonly
    # database" errors); each test uses its own topic/card rows instead.
    global _initialized
    if not _initialized:
        await init_db()
        _initialized = True
    async with get_session() as session:
        yield session
