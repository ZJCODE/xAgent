# main.py
from fastapi import FastAPI

from api.health import router as health_router
from api.vocabulary import router as vocabulary_router


app = FastAPI()
app.include_router(health_router)
app.include_router(vocabulary_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
