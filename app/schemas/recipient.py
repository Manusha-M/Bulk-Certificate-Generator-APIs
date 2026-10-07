from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, EmailStr, Field

from app.db.models import RECIPIENT_NAME_MAX_LENGTH
from app.utils.text import normalize_text

RecipientName = Annotated[
    str,
    BeforeValidator(normalize_text),
    Field(min_length=1, max_length=RECIPIENT_NAME_MAX_LENGTH, examples=["John Doe"]),
]
RecipientEmail = Annotated[
    EmailStr,
    BeforeValidator(lambda value: value.strip() if isinstance(value, str) else value),
    AfterValidator(str.lower),
    Field(examples=["john@example.com"]),
]


class RecipientIn(BaseModel):
    """One person who should receive a certificate."""

    model_config = ConfigDict(extra="forbid")

    name: RecipientName
    email: RecipientEmail
