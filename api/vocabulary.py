from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from tenacity import RetryError
from core.vocabulary import VocabularyService
from schemas.vocabulary import BaseVocabularyRecord, VocabularyRecord

router = APIRouter()
service = VocabularyService()

class LookupRequest(BaseModel):
    word: str
    user_id: str | None = None
    save: bool = True
    cache: bool = True  # 是否使用缓存


class GetVocabularyRequest(BaseModel):
    user_id: str
    n: int = 10
    exclude_known: bool = False

@router.post("/lookup", response_model=VocabularyRecord)
def lookup_word_api(request: LookupRequest):
    try:
        result = service.lookup_word(
            word=request.word,
            user_id=request.user_id,
            save=request.save,
            cache=request.cache
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RetryError as e:
        last_exc = e.last_attempt.exception()
        if isinstance(last_exc, ValueError):
            raise HTTPException(status_code=400, detail=str(last_exc))
        raise HTTPException(status_code=500, detail="Internal server error")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")


# 新增 get_vocabulary 接口
@router.post("/get_vocabulary", response_model=list[VocabularyRecord])
def get_vocabulary_api(request: GetVocabularyRequest):
    try:
        result = service.get_vocabulary(user_id=request.user_id, n=request.n, exclude_known=request.exclude_known)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")
