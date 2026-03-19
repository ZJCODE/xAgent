from pydantic import BaseModel, Field


class DiaryEntry(BaseModel):
    """Structured output for a diary entry formatted by the LLM."""

    content: str = Field(default="", description="Diary entry text")


class SummaryOutput(BaseModel):
    """Structured output for a periodic summary (weekly/monthly/yearly)."""

    content: str = Field(default="", description="Summary text")
