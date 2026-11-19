from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    """App `a`'s settings: every field has a default, so a new installation is valid."""

    model_config = ConfigDict(extra="forbid")

    threshold_c: float = 0.0
