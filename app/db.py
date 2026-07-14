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


class LeadRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    company: str
    phone: str
    # Lifecycle: "new" -> "unreachable" | "not_qualified" | "booked"
    status: str = "new"
    intent_score: int | None = None
    booked_slot: str | None = None
    # Optional, demo-only annotation the simulated providers read and real providers
    # ignore: "books" | "no_answer" | "not_qualified". Nullable so leads created via
    # the API need not carry one; a None profile follows the happy path.
    sim_profile: str | None = None
    created_at: datetime
    updated_at: datetime


class RunRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    spec_id: int
    lead_id: int
    status: str = "running"  # "running" -> "completed" | "failed"
    created_at: datetime
    updated_at: datetime


class RunStepRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int
    tool: str
    result_json: str  # serialized ToolResult
    created_at: datetime


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    seed_leads()  # demo needs inspectable leads on first boot; idempotent so restarts don't duplicate


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
        # Most-recently-edited first; id tiebreaker for same-timestamp saves.
        statement = select(SpecRecord).order_by(
            SpecRecord.updated_at.desc(), SpecRecord.id.desc()
        )
        return list(session.exec(statement))


def update_spec(spec_id: int, spec: AssistantSpec) -> SpecRecord | None:
    # Last-write-wins (rule #6): concurrent edits at this scale don't need locking.
    # None on missing — caller maps to 404; never raise here.
    with Session(engine) as session:
        record = session.get(SpecRecord, spec_id)
        if record is None:
            return None
        record.name = spec.name
        record.spec_json = spec.model_dump_json()
        record.updated_at = datetime.now(timezone.utc)
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def get_lead(lead_id: int) -> LeadRecord | None:
    with Session(engine) as session:
        return session.get(LeadRecord, lead_id)


def list_leads() -> list[LeadRecord]:
    with Session(engine) as session:
        statement = select(LeadRecord).order_by(
            LeadRecord.created_at.desc(), LeadRecord.id.desc()
        )
        return list(session.exec(statement))


def update_lead_outcome(
    lead_id: int,
    status: str,
    intent_score: int | None = None,
    booked_slot: str | None = None,
) -> LeadRecord | None:
    with Session(engine) as session:
        lead = session.get(LeadRecord, lead_id)
        if lead is None:
            return None
        lead.status = status
        # Only overwrite when a value is actually provided — a later status-only
        # update (e.g. marking unreachable) must not clobber an earlier score.
        if intent_score is not None:
            lead.intent_score = intent_score
        if booked_slot is not None:
            lead.booked_slot = booked_slot
        lead.updated_at = datetime.now(timezone.utc)
        session.add(lead)
        session.commit()
        session.refresh(lead)
        return lead


def create_run(spec_id: int, lead_id: int) -> RunRecord:
    now = datetime.now(timezone.utc)
    record = RunRecord(
        spec_id=spec_id,
        lead_id=lead_id,
        status="running",
        created_at=now,
        updated_at=now,
    )
    with Session(engine) as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def create_lead(name: str, company: str, phone: str, sim_profile: str | None = None) -> LeadRecord:
    now = datetime.now(timezone.utc)
    record = LeadRecord(
        name=name,
        company=company,
        phone=phone,
        sim_profile=sim_profile,
        created_at=now,
        updated_at=now,
    )
    with Session(engine) as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def get_run(run_id: int) -> RunRecord | None:
    with Session(engine) as session:
        return session.get(RunRecord, run_id)


def list_runs() -> list[RunRecord]:
    with Session(engine) as session:
        statement = select(RunRecord).order_by(
            RunRecord.created_at.desc(), RunRecord.id.desc()
        )
        return list(session.exec(statement))


def finish_run(run_id: int, status: str) -> RunRecord | None:
    with Session(engine) as session:
        run = session.get(RunRecord, run_id)
        if run is None:
            return None
        run.status = status
        run.updated_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


def add_run_step(run_id: int, tool: str, result_json: str) -> RunStepRecord:
    # Appended as each step completes — this is how a poll or SSE reconnect
    # rebuilds a live run; run state must never live only in the stream.
    record = RunStepRecord(
        run_id=run_id,
        tool=tool,
        result_json=result_json,
        created_at=datetime.now(timezone.utc),
    )
    with Session(engine) as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def list_run_steps(run_id: int) -> list[RunStepRecord]:
    with Session(engine) as session:
        # ASC by id: execution order, not recency
        statement = (
            select(RunStepRecord)
            .where(RunStepRecord.run_id == run_id)
            .order_by(RunStepRecord.id.asc())
        )
        return list(session.exec(statement))


def seed_leads() -> None:
    with Session(engine) as session:
        if session.exec(select(LeadRecord)).first() is not None:
            return  # idempotent: don't duplicate seed data on restart
        now = datetime.now(timezone.utc)
        demo_leads = [
            LeadRecord(
                name="Dana Reyes",
                company="Northwind Analytics",
                phone="+1-555-0142",
                sim_profile="books",
                created_at=now,
                updated_at=now,
            ),
            LeadRecord(
                name="Marcus Chen",
                company="Fieldstone Logistics",
                phone="+1-555-0198",
                sim_profile="no_answer",
                created_at=now,
                updated_at=now,
            ),
            LeadRecord(
                name="Priya Nair",
                company="Havenlight CRM",
                phone="+1-555-0173",
                sim_profile="not_qualified",
                created_at=now,
                updated_at=now,
            ),
        ]
        session.add_all(demo_leads)
        session.commit()
