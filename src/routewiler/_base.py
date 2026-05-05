from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class RoutewilerModel(BaseModel):
    """Shared base for all Routewiler Pydantic models.

    - Attribute access uses snake_case (Python convention).
    - JSON serialisation uses camelCase (wire format, matching the TS schema).
    - Unknown fields are forbidden to catch schema drift early.

    Use ``RoutewilerLooseModel`` instead for inbound third-party wire formats
    (e.g. server 402 headers) where unknown fields must be tolerated for
    spec-evolution tolerance.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,  # allow construction via snake_case kwargs
        extra="forbid",
        frozen=False,
    )


class RoutewilerLooseModel(BaseModel):
    """Base for inbound third-party wire-format models.

    Identical to ``RoutewilerModel`` except ``extra="ignore"`` — unknown
    fields from a server response are silently dropped rather than raising
    a validation error.  Use this for any model parsed from external data
    (402 challenge payloads, receipt headers, etc.).
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        frozen=False,
    )
