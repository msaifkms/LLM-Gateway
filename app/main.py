from fastapi import FastAPI

from app.api.chat import router as chat_router

app = FastAPI(
    title="LLM Gateway",
    version="0.1.0",
)


@app.get("/healthz")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(chat_router)
