from fastapi import APIRouter, Request, Response

from app.core import metrics

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    gateway_router = getattr(request.app.state, "router", None)
    if gateway_router is not None:
        # Breaker state changes on the clock, so read it at scrape time.
        metrics.update_breaker_states(gateway_router.breaker_states())
    return Response(metrics.render(), media_type=metrics.CONTENT_TYPE)
