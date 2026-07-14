"""Spec Store — persists AssistantSpec records to SQLite via SQLModel.

ORM (rather than raw SQL) keeps the later SQLite -> Postgres swap trivial,
per the project's settled persistence decision.
"""

import os
from datetime import datetime, timezone

from sqlmodel import Field, Session, SQLModel, create_engine, select

from app.spec import AssistantSpec

# Read at import time so tests can point this at a temp file by setting the
# env var before app.db is first imported.
engine = create_engine(
    os.getenv("AI_CONDUCTOR_DB", "sqlite:///ai_conductor.db"),
    connect_args={"check_same_thread": False},
)


class SpecRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    spec_json: str  # the validated AssistantSpec, serialized
    created_at: datetime
    updated_at: datetime


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def save_spec(spec: AssistantSpec) -> SpecRecord:
    now = datetime.now(timezone.utc)
    record = SpecRecord(
        name=spec.name,
        spec_json=spec.model_dump_json(),
        created_at=now,
        updated_at=now,
    )
    with Session(engine) as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def get_spec(spec_id: int) -> SpecRecord | None:
    # None on missing — caller maps to 404; never raise here.
    with Session(engine) as session:
        return session.get(SpecRecord, spec_id)


def list_specs() -> list[SpecRecord]:
    with Session(engine) as session:
        # id tiebreaker: two saves can land on the same timestamp
        statement = select(SpecRecord).order_by(
            SpecRecord.created_at.desc(), SpecRecord.id.desc()
        )
        return list(session.exec(statement))
