"""Point the Spec Store at a throwaway SQLite file for the whole test session.

app.db.engine is created at import time from the AI_CONDUCTOR_DB env var, so
this must run before anything imports app.db — hence setting it here, at
collection time, ahead of any test module's imports.
"""

import os
import tempfile

_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_db_file.close()
os.environ["AI_CONDUCTOR_DB"] = "sqlite:///" + _db_file.name.replace("\\", "/")

from app.db import init_db  # noqa: E402  (must follow the env var assignment above)

init_db()
