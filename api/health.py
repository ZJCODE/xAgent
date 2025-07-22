# api/health.py
from fastapi import APIRouter
from core.base import BaseService
from schemas.health import HealthResponse

router = APIRouter()
service = BaseService()

@router.get("/health", response_model=HealthResponse)
def health_check():
    return service.health()
