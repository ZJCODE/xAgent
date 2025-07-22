# core/base.py

class BaseService:
    """基础服务类，供其他服务继承"""
    def health(self):
        return {"status": "ok"}
