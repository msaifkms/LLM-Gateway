"""GET /v1/usage: what the calling user has spent."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, PlainSerializer

from app.api.auth import authenticate
from app.auth.store import Principal
from app.usage.store import UsageStore

router = APIRouter(prefix="/v1")

#: Money is sent as a fixed-point string, so zero is "0.00000000", never "0E-8".
Money = Annotated[
    Decimal,
    PlainSerializer(
        lambda value: format(value, "f"), return_type=str, when_used="json"
    ),
]


def get_usage_store(request: Request) -> UsageStore | None:
    store: UsageStore | None = request.app.state.usage_store
    return store


class TotalsOut(BaseModel):
    requests: int
    cache_served: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Money
    saved_usd: Money


class ModelOut(BaseModel):
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Money


class DayOut(BaseModel):
    day: date
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Money


class UsageOut(BaseModel):
    days: int
    totals: TotalsOut
    by_model: list[ModelOut]
    by_day: list[DayOut]


@router.get("/usage", response_model=UsageOut)
async def get_usage(
    principal: Annotated[Principal, Depends(authenticate)],
    store: Annotated[UsageStore | None, Depends(get_usage_store)],
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> UsageOut:
    if store is None:
        raise HTTPException(status_code=503, detail="Usage reporting is unavailable")

    try:
        summary = await store.summary(principal.user_id, days)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
        raise HTTPException(
            status_code=503, detail="Usage reporting is unavailable"
        ) from None

    return UsageOut(
        days=days,
        totals=TotalsOut(**asdict(summary.totals)),
        by_model=[ModelOut(**asdict(m)) for m in summary.by_model],
        by_day=[DayOut(**asdict(d)) for d in summary.by_day],
    )
