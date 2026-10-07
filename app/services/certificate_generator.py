"""PDF rendering for the single predefined certificate design.

The background artwork (borders, corner ornaments, seal) is a PNG in `templates/`.
Recipient-specific text is drawn on top with ReportLab, so it stays sharp and searchable.
This module knows nothing about HTTP or the database.
"""

import os
import uuid
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path, PurePosixPath

from PIL import Image
from reportlab import rl_config
from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

# Write image data as raw binary instead of ASCII85 text: ~25% smaller and ~3x faster PDFs.
rl_config.useA85 = 0

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)
BACKGROUND_JPEG_QUALITY = 90
CENTER_X = PAGE_WIDTH / 2
TEXT_MAX_WIDTH = 640

NAVY = Color(31 / 255, 42 / 255, 68 / 255)
GOLD = Color(184 / 255, 145 / 255, 58 / 255)
GREY = Color(95 / 255, 99 / 255, 110 / 255)

# The built-in PDF fonts (Helvetica, Times) only cover the Windows-1252 character set.
FONT_ENCODING = "cp1252"

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def font_can_display(text: str) -> bool:
    """True when every character of `text` exists in the certificate fonts."""
    try:
        text.encode(FONT_ENCODING)
    except UnicodeEncodeError:
        return False
    return True


class CertificateGenerationError(Exception):
    """An expected, recipient-specific failure. The message is safe to show to API clients."""


@dataclass(frozen=True)
class CertificateData:
    """Everything printed on one certificate."""

    certificate_id: uuid.UUID
    job_id: uuid.UUID
    recipient_name: str
    certificate_title: str
    course_name: str
    organization_name: str
    event_date: date


def format_certificate_date(value: date) -> str:
    """'7 October 2026' — locale-independent, so output is the same on every machine."""
    return f"{value.day} {MONTH_NAMES[value.month - 1]} {value.year}"


def fit_font_size(
    text: str, font: str, max_size: float, max_width: float, char_space: float = 0
) -> float:
    """Largest font size (up to max_size) at which `text` fits in `max_width` points."""
    width_at_one_point = stringWidth(text, font, 1)
    spacing = char_space * max(len(text) - 1, 0)
    if width_at_one_point == 0:
        return max_size
    return min(max_size, (max_width - spacing) / width_at_one_point)


class CertificateGenerator:
    """Renders certificates as PDF files under `output_dir/<job_id>/<certificate_id>.pdf`."""

    def __init__(self, template_path: Path, output_dir: Path) -> None:
        if not template_path.is_file():
            raise FileNotFoundError(f"Certificate template not found: {template_path}")
        self._background = self._load_background(template_path)
        self._output_dir = output_dir

    @staticmethod
    def _load_background(template_path: Path) -> bytes:
        """Convert the PNG template to JPEG bytes once, at startup.

        ReportLab embeds JPEG data as-is, while PNG data is re-compressed for every PDF.
        This makes each certificate about 6x faster to render and half the size.
        The bytes are kept in memory and wrapped in a fresh ImageReader per certificate,
        so nothing mutable is shared between threads.
        """
        buffer = BytesIO()
        with Image.open(template_path) as image:
            image.convert("RGB").save(buffer, "JPEG", quality=BACKGROUND_JPEG_QUALITY)
        return buffer.getvalue()

    def generate_certificate(self, data: CertificateData) -> str:
        """Render one certificate and return its path relative to the output directory.

        The file name is built only from UUIDs, never from user input, so it cannot be
        used for path traversal. The PDF is written to a temporary file first and then
        renamed, so a half-written file is never visible under its final name.
        """
        texts = self._display_texts(data)
        self._ensure_renderable(texts)

        relative_path = PurePosixPath(str(data.job_id), f"{data.certificate_id}.pdf")
        target = self._output_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        try:
            pdf = Canvas(str(temporary), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
            self._draw(pdf, data, texts)
            pdf.save()
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return relative_path.as_posix()

    def delete_certificate(self, relative_path: str) -> None:
        """Remove a generated file (used when its database record could not be saved)."""
        (self._output_dir / relative_path).unlink(missing_ok=True)

    @staticmethod
    def _display_texts(data: CertificateData) -> dict[str, str]:
        return {
            "organization name": data.organization_name.upper(),
            "certificate title": data.certificate_title.upper(),
            "recipient name": data.recipient_name,
            "course name": data.course_name,
            "signature": data.organization_name,
            "date": format_certificate_date(data.event_date),
        }

    @staticmethod
    def _ensure_renderable(texts: dict[str, str]) -> None:
        for label, text in texts.items():
            if not font_can_display(text):
                raise CertificateGenerationError(
                    f"The {label} contains characters that the certificate font cannot display"
                )

    def _draw(self, pdf: Canvas, data: CertificateData, texts: dict[str, str]) -> None:
        pdf.setTitle(f"{data.certificate_title} - {data.recipient_name}")
        pdf.setAuthor(data.organization_name)
        pdf.setSubject(data.course_name)

        background = ImageReader(BytesIO(self._background))
        pdf.drawImage(background, 0, 0, width=PAGE_WIDTH, height=PAGE_HEIGHT)

        self._draw_header(pdf, texts["organization name"], texts["certificate title"])
        self._draw_recipient(pdf, texts["recipient name"])
        self._draw_course(pdf, texts["course name"])
        self._draw_footer(pdf, texts["date"], texts["signature"], data.certificate_id)

    @staticmethod
    def _draw_header(pdf: Canvas, organization: str, title: str) -> None:
        pdf.setFillColor(NAVY)
        size = fit_font_size(organization, "Helvetica-Bold", 13, TEXT_MAX_WIDTH, char_space=2)
        pdf.setFont("Helvetica-Bold", size)
        pdf.drawCentredString(CENTER_X, 488, organization, charSpace=2)

        size = fit_font_size(title, "Times-Bold", 36, TEXT_MAX_WIDTH, char_space=1)
        pdf.setFont("Times-Bold", size)
        pdf.drawCentredString(CENTER_X, 428, title, charSpace=1)

        pdf.setStrokeColor(GOLD)
        pdf.setLineWidth(1.2)
        pdf.line(CENTER_X - 130, 410, CENTER_X - 10, 410)
        pdf.line(CENTER_X + 10, 410, CENTER_X + 130, 410)
        pdf.setFillColor(GOLD)
        pdf.circle(CENTER_X, 410, 3, stroke=0, fill=1)

    @staticmethod
    def _draw_recipient(pdf: Canvas, name: str) -> None:
        pdf.setFillColor(GREY)
        pdf.setFont("Helvetica", 11)
        pdf.drawCentredString(CENTER_X, 374, "THIS CERTIFICATE IS PROUDLY PRESENTED TO", charSpace=2)

        pdf.setFillColor(NAVY)
        size = fit_font_size(name, "Times-BoldItalic", 44, TEXT_MAX_WIDTH)
        pdf.setFont("Times-BoldItalic", size)
        pdf.drawCentredString(CENTER_X, 318, name)

        pdf.setStrokeColor(GOLD)
        pdf.setLineWidth(0.8)
        pdf.line(CENTER_X - 230, 304, CENTER_X + 230, 304)

    @staticmethod
    def _draw_course(pdf: Canvas, course: str) -> None:
        pdf.setFillColor(GREY)
        pdf.setFont("Helvetica-Oblique", 13)
        pdf.drawCentredString(CENTER_X, 274, "for successfully completing")

        # Shrink long course names first; wrap onto two lines only when shrinking is not enough.
        size = 20.0
        lines = simpleSplit(course, "Helvetica-Bold", size, TEXT_MAX_WIDTH)
        while len(lines) > 1 and size > 14:
            size -= 2
            lines = simpleSplit(course, "Helvetica-Bold", size, TEXT_MAX_WIDTH)
        pdf.setFillColor(NAVY)
        pdf.setFont("Helvetica-Bold", size)
        for index, line in enumerate(lines):
            pdf.drawCentredString(CENTER_X, 244 - index * size * 1.25, line)

    @staticmethod
    def _draw_footer(pdf: Canvas, date_text: str, signature: str, certificate_id: uuid.UUID) -> None:
        for center_x, value, font, label in (
            (200, date_text, "Helvetica", "DATE"),
            (PAGE_WIDTH - 200, signature, "Times-Italic", "AUTHORIZED BY"),
        ):
            pdf.setFillColor(NAVY)
            pdf.setFont(font, fit_font_size(value, font, 15, 190))
            pdf.drawCentredString(center_x, 128, value)
            pdf.setStrokeColor(GOLD)
            pdf.setLineWidth(0.8)
            pdf.line(center_x - 95, 120, center_x + 95, 120)
            pdf.setFillColor(GREY)
            pdf.setFont("Helvetica", 8)
            pdf.drawCentredString(center_x, 106, label, charSpace=1.5)

        pdf.setFillColor(GREY)
        pdf.setFont("Helvetica", 7.5)
        pdf.drawCentredString(CENTER_X, 44, f"Certificate ID: {certificate_id}")
