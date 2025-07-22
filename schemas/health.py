# schemas/health.py
from pydantic import BaseModel

class HealthResponse(BaseModel):
    status: str
