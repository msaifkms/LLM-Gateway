"""Turn a finished chat request into a usage row, and write it safely."""

import logging
from decimal import Decimal

import asyncpg

from app.api.schemas import ChatCompletionResponse
from app.auth.store import Principal
from app.core import metrics
from app.usage.pricing import compute_cost
from app.usage.store import UsageEvent, UsageStore

logger = logging.getLogger(__name__)

#: Only these statuses involved a provider call, so only these cost money.
#: A cache HIT reuses an answer this user already paid for; a COALESCED follower
#: shares the leader's call.
BILLABLE_STATUSES = frozenset({"MISS", "BYPASS"})


def build_event(
    principal: Principal,
    route: str,
    cache_status: str,
    provider: str,
    response: ChatCompletionResponse,
) -> UsageEvent:
    usage = response.usage
    list_cost = compute_cost(
        response.model, usage.prompt_tokens, usage.completion_tokens
    )
    charged = list_cost.amount if cache_status in BILLABLE_STATUSES else Decimal(0)
    return UsageEvent(
        user_id=principal.user_id,
        api_key_id=principal.key_id,
        route=route,
        provider=provider,
        model=response.model,
        cache_status=cache_status,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cost_usd=charged,
        list_cost_usd=list_cost.amount,
        priced=list_cost.priced,
    )


async def record_usage(store: UsageStore | None, event: UsageEvent) -> None:
    """Write the row. A failure is logged, never raised.

    The provider has already answered, so failing the request now would punish the
    caller for our bookkeeping problem. The cost is an ERROR log line instead.
    Metrics are updated first, so spend stays visible even if Postgres is down.
    """
    metrics.observe_usage(
        model=event.model,
        billed=event.cache_status in BILLABLE_STATUSES,
        prompt_tokens=event.prompt_tokens,
        completion_tokens=event.completion_tokens,
        cost_usd=float(event.cost_usd),
        list_cost_usd=float(event.list_cost_usd),
        priced=event.priced,
    )
    if store is None:
        return
    if not event.priced:
        logger.warning("No price on file for model %r; recording cost 0", event.model)
    try:
        await store.record(event)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
        metrics.USAGE_WRITE_FAILURES.inc()
        logger.exception("Could not record usage for user %s", event.user_id)
