from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class RoutewilerModel(BaseModel):
    """Shared base for all Routewiler Pydantic models.

    - Attribute access uses snake_case (Python convention).
    - JSON serialisation uses camelCase (wire format, matching the TS schema).
    - Unknown fields are forbidden to catch schema drift early.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,  # allow construction via snake_case kwargs
        extra="forbid",
        frozen=False,
    )
