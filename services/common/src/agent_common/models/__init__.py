"""Base request/response models: snake_case in Python, camelCase on the wire."""
from datetime import datetime
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

T = TypeVar("T")


class ApiModel(BaseModel):
    """Base for every API/tool payload. Accepts snake_case or camelCase input; serialises as camelCase."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True,
                              str_strip_whitespace=True, extra="forbid")

    def dump(self) -> dict:
        """JSON-ready dict with camelCase keys."""
        return self.model_dump(mode="json", by_alias=True)


class ApiResponse(ApiModel):
    """Base for responses: unknown attributes from ORM objects are ignored rather than rejected."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True,
                              extra="ignore")


class TimestampedResponse(ApiResponse):
    created_at: datetime
    updated_at: Optional[datetime] = None


class PageResponse(ApiResponse, Generic[T]):
    items: list[T]
    total: int
