"""GET /v1/models: the model aliases a caller can send to /v1/chat/completions."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import authenticate
from app.api.chat import get_router
from app.auth.store import Principal
from app.core.router import Router

router = APIRouter(prefix="/v1")


class ModelOut(BaseModel):
    id: str
    object: Literal["model"] = "model"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelOut]


@router.get("/models", response_model=ModelList)
async def list_models(
    _principal: Annotated[Principal, Depends(authenticate)],
    routing: Annotated[Router, Depends(get_router)],
) -> ModelList:
    # Aliases only: which providers sit behind each one is the gateway's business.
    return ModelList(data=[ModelOut(id=alias) for alias in routing.aliases()])
