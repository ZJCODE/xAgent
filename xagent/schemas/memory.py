from pydantic import BaseModel, Field


class DiaryEntry(BaseModel):
    """Structured output for a diary entry formatted by the LLM."""

    content: str = Field(default="", description="Diary entry text")


class SummaryOutput(BaseModel):
    """Structured output for a periodic summary (weekly/monthly/yearly)."""

    content: str = Field(default="", description="Summary text")


class PeopleProfileFact(BaseModel):
    """A quote-backed stable fact about one person."""

    person_key: str = Field(default="", description="Exact speaker label from the source transcript")
    display_name: str = Field(default="", description="Human-readable name for the person")
    fact: str = Field(default="", description="Stable reusable fact about the person")
    evidence: str = Field(default="", description="Short direct quote or exact evidence from the transcript")
    source: str = Field(default="", description="Brief source note, such as direct message or ambient context")


class PeopleProfileUpdates(BaseModel):
    """Structured output for people profile extraction."""

    updates: list[PeopleProfileFact] = Field(default_factory=list)
