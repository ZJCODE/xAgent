from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.lookup import VocabularyService
from schemas.vocabulary import BaseVocabularyRecord, VocabularyRecord

router = APIRouter()
service = VocabularyService()

class LookupRequest(BaseModel):
    word: str
    user_id: str | None = None
    save: bool = False
    cache: bool = True  # 是否使用缓存
    # 允许额外参数
    extra: dict = {}

@router.post("/lookup", response_model=BaseVocabularyRecord)
def lookup_word_api(request: LookupRequest):
    try:
        result = service.lookup_word(
            word=request.word,
            user_id=request.user_id,
            save=request.save,
            cache=request.cache,
            **request.extra
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")
