from fastapi import FastAPI

app = FastAPI(title="LLM Gateway")


@app.get("/healthz")
def health_check():
    return {"status": "ok"}
