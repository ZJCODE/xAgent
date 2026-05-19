from pydantic import BaseModel, Field


class DiaryEntry(BaseModel):
    """Structured output for a diary entry formatted by the LLM."""

    content: str = Field(default="", description="Diary entry text")


class SummaryOutput(BaseModel):
    """Structured output for a periodic summary (weekly/monthly/yearly)."""

    content: str = Field(default="", description="Summary text")


class MemoryFact(BaseModel):
    """One durable fact extracted from experience with explicit attribution."""

    kind: str = Field(default="", description="Memory kind such as preference, commitment, or person_fact")
    subject_type: str = Field(default="", description="Subject type such as self, person, project, topic, room, or system")
    subject_key: str = Field(default="", description="Stable subject identifier")
    title: str = Field(default="", description="Short UI-friendly title for the fact")
    content: str = Field(default="", description="Durable fact content")
    evidence: str = Field(default="", description="Short direct quote or exact source phrase")
    source: str = Field(default="", description="Short provenance note")
    confidence: float = Field(default=0.85, description="Confidence score from 0.0 to 1.0")
    salience: float = Field(default=0.7, description="Importance score from 0.0 to 1.0")
    display_name: str = Field(default="", description="Optional display name when the subject is a person")


class MemorySynthesis(BaseModel):
    """Unified extraction result for one experience batch."""

    experience_summary: str = Field(default="", description="First-person episodic summary of the batch")
    facts: list[MemoryFact] = Field(default_factory=list, description="Durable attributed facts worth storing")


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
