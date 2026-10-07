"""The PDF generator on its own (no API, no database)."""

import uuid
from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.services import certificate_generator
from app.services.certificate_generator import (
    CertificateData,
    CertificateGenerationError,
    CertificateGenerator,
    fit_font_size,
    font_can_display,
    format_certificate_date,
)
from tests.conftest import TEMPLATE_PATH


def make_data(**overrides: object) -> CertificateData:
    values: dict[str, object] = {
        "certificate_id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "recipient_name": "Jane Smith",
        "certificate_title": "Certificate of Completion",
        "course_name": "Advanced Python Backend Development",
        "organization_name": "Example Academy",
        "event_date": date(2026, 10, 7),
    }
    values.update(overrides)
    return CertificateData(**values)  # type: ignore[arg-type]


def pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(path).pages)


def test_generates_a_pdf_file(generator: CertificateGenerator, output_dir: Path) -> None:
    data = make_data()

    relative_path = generator.generate_certificate(data)

    path = output_dir / relative_path
    assert path.is_file()
    assert path.stat().st_size > 10_000
    assert path.read_bytes().startswith(b"%PDF-")
    assert len(PdfReader(path).pages) == 1


def test_pdf_contains_recipient_specific_information(
    generator: CertificateGenerator, output_dir: Path
) -> None:
    data = make_data(recipient_name="José Álvarez")

    text = pdf_text(output_dir / generator.generate_certificate(data))

    assert "José Álvarez" in text
    assert "Advanced Python Backend Development" in text
    assert "CERTIFICATE OF COMPLETION" in text
    assert "EXAMPLE ACADEMY" in text
    assert "7 October 2026" in text
    assert str(data.certificate_id) in text


def test_pdf_metadata_names_the_recipient(
    generator: CertificateGenerator, output_dir: Path
) -> None:
    path = output_dir / generator.generate_certificate(make_data())

    metadata = PdfReader(path).metadata
    assert metadata is not None
    assert metadata.title == "Certificate of Completion - Jane Smith"


def test_different_recipients_get_different_files(
    generator: CertificateGenerator, output_dir: Path
) -> None:
    job_id = uuid.uuid4()
    first = generator.generate_certificate(make_data(job_id=job_id, recipient_name="Ann"))
    second = generator.generate_certificate(make_data(job_id=job_id, recipient_name="Bob"))

    assert first != second
    assert "Ann" in pdf_text(output_dir / first)
    assert "Bob" in pdf_text(output_dir / second)


def test_file_name_uses_only_uuids(generator: CertificateGenerator, output_dir: Path) -> None:
    data = make_data(recipient_name="../../etc/passwd")

    relative_path = generator.generate_certificate(data)

    assert relative_path == f"{data.job_id}/{data.certificate_id}.pdf"
    assert (output_dir / relative_path).resolve().is_relative_to(output_dir.resolve())
    assert not list(output_dir.rglob("*.tmp"))


def test_very_long_texts_still_render(generator: CertificateGenerator, output_dir: Path) -> None:
    data = make_data(recipient_name="N" * 100, course_name="Course " * 28, organization_name="O" * 150)

    path = output_dir / generator.generate_certificate(data)

    assert "N" * 100 in pdf_text(path)


def test_unsupported_characters_raise_generation_error(
    generator: CertificateGenerator, output_dir: Path
) -> None:
    with pytest.raises(CertificateGenerationError, match="recipient name"):
        generator.generate_certificate(make_data(recipient_name="李雷"))

    assert not list(output_dir.rglob("*.pdf"))


def test_missing_template_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CertificateGenerator(tmp_path / "missing.png", tmp_path)


def test_unwritable_output_dir_raises_os_error(tmp_path: Path) -> None:
    blocked_dir = tmp_path / "out"
    blocked_dir.write_text("a file where the output directory should be")
    generator = CertificateGenerator(TEMPLATE_PATH, blocked_dir)

    with pytest.raises(OSError):
        generator.generate_certificate(make_data())


def test_write_failure_leaves_no_partial_file(
    generator: CertificateGenerator, output_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_replace(source: object, target: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(certificate_generator.os, "replace", failing_replace)

    with pytest.raises(OSError):
        generator.generate_certificate(make_data())

    assert list(output_dir.rglob("*.tmp")) == []
    assert list(output_dir.rglob("*.pdf")) == []


def test_delete_certificate_removes_file(generator: CertificateGenerator, output_dir: Path) -> None:
    relative_path = generator.generate_certificate(make_data())

    generator.delete_certificate(relative_path)

    assert not (output_dir / relative_path).exists()


def test_font_can_display() -> None:
    assert font_can_display("José Müller-Ørsted")
    assert not font_can_display("李雷")
    assert not font_can_display("Łódź")


def test_format_certificate_date() -> None:
    assert format_certificate_date(date(2026, 1, 5)) == "5 January 2026"


def test_fit_font_size_shrinks_long_text() -> None:
    assert fit_font_size("Short", "Helvetica", 20, 500) == 20
    assert fit_font_size("Long text " * 20, "Helvetica", 20, 500) < 20
