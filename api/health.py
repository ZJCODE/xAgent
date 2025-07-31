# api/health.py
from fastapi import APIRouter
from api.base import BaseService
from api.schemas.health import HealthResponse

router = APIRouter()
service = BaseService()

@router.get("/health", response_model=HealthResponse)
def health_check():
    return service.health()
