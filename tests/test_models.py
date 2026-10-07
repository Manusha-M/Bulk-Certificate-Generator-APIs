"""Database schema: relationships, constraints, and migration/model consistency."""

import uuid
from datetime import date

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import Base
from app.db.models import Certificate, CertificateStatus, GenerationJob, JobStatus


def make_job(**overrides: object) -> GenerationJob:
    values: dict[str, object] = {
        "certificate_title": "Certificate of Completion",
        "course_name": "Course",
        "organization_name": "Org",
        "event_date": date(2026, 10, 7),
        "total_recipients": 1,
        "certificates": [
            Certificate(position=0, recipient_name="Ann", recipient_email="ann@example.com")
        ],
    }
    values.update(overrides)
    return GenerationJob(**values)


def test_job_has_certificates_in_request_order(db_session: Session) -> None:
    job = make_job(
        total_recipients=2,
        certificates=[
            Certificate(position=1, recipient_name="Bob", recipient_email="bob@example.com"),
            Certificate(position=0, recipient_name="Ann", recipient_email="ann@example.com"),
        ],
    )
    db_session.add(job)
    db_session.commit()
    db_session.expire_all()

    stored = db_session.get(GenerationJob, job.id)
    assert stored is not None
    assert [c.recipient_name for c in stored.certificates] == ["Ann", "Bob"]
    assert stored.certificates[0].job is stored
    assert stored.status == JobStatus.PENDING
    assert stored.certificates[0].status == CertificateStatus.PENDING
    assert stored.created_at is not None


def test_deleting_a_job_deletes_its_certificates(db_session: Session) -> None:
    job = make_job()
    db_session.add(job)
    db_session.commit()

    db_session.delete(job)
    db_session.commit()

    assert db_session.scalars(select(Certificate)).all() == []


def test_certificate_requires_existing_job(db_session: Session) -> None:
    db_session.add(
        Certificate(job_id=uuid.uuid4(), position=0, recipient_name="A", recipient_email="a@x.io")
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize(
    "certificate_values",
    [
        {"status": CertificateStatus.COMPLETED, "file_path": None},
        {"status": CertificateStatus.FAILED, "error_message": None},
        {"position": -1},
    ],
    ids=["completed-without-file", "failed-without-error", "negative-position"],
)
def test_certificate_constraints(db_session: Session, certificate_values: dict) -> None:
    values = {"position": 0, "recipient_name": "Ann", "recipient_email": "ann@example.com"}
    values.update(certificate_values)
    db_session.add(make_job(certificates=[Certificate(**values)]))

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_one_record_per_recipient_position(db_session: Session) -> None:
    db_session.add(
        make_job(
            total_recipients=2,
            certificates=[
                Certificate(position=0, recipient_name="Ann", recipient_email="a@example.com"),
                Certificate(position=0, recipient_name="Bob", recipient_email="b@example.com"),
            ],
        )
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize(
    "job_values",
    [
        {"total_recipients": 0, "certificates": []},
        {"successful_count": -1},
        {"successful_count": 1, "failed_count": 1},
    ],
    ids=["zero-recipients", "negative-count", "counts-exceed-total"],
)
def test_job_constraints(db_session: Session, job_values: dict) -> None:
    db_session.add(make_job(**job_values))

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_migrations_match_models(engine: Engine) -> None:
    """Tables, columns, types, nullability, indexes, unique and foreign keys built by the
    Alembic migration match the ORM models. (Alembic does not compare CHECK constraints;
    those are covered by the constraint tests above.)"""
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []
