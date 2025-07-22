class BaseService:
    """
    Base service class for health check and common service logic.
    """
    def health(self):
        return {"status": "ok"}
