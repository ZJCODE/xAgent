# api/health.py
from fastapi import APIRouter
from xagent.core.base import BaseService
from xagent.schemas.health import HealthResponse

router = APIRouter()
service = BaseService()

@router.get("/health", response_model=HealthResponse)
def health_check():
    return service.health()
