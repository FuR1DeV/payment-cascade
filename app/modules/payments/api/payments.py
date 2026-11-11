import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Response, status

from app.modules.payments.application.create_payment import (
    CreatePaymentCommand,
    IdempotencyKeyReusedError,
)
from app.modules.payments.domain.entities import Payment
from app.modules.payments.domain.enums import PaymentMethodType, PaymentStatus
from app.modules.payments.domain.money import Money

from .dependencies import ContainerDep
from .schemas import AlertOut, CreatePaymentRequest, ErrorOut, PaymentOut

router = APIRouter(tags=["payments"])

# Keep references to running cascades so they are not garbage-collected (see below).
_running: set[asyncio.Task[Payment]] = set()


@router.post(
    "/payments",
    status_code=status.HTTP_201_CREATED,
    response_model=PaymentOut,
    responses={
        200: {"model": PaymentOut, "description": "Replay: the key was used before"},
        400: {"model": ErrorOut},
        409: {"model": ErrorOut, "description": "The original request is still in progress"},
        422: {"model": ErrorOut, "description": "The key was used with a different body"},
    },
)
async def create_payment(
    body: CreatePaymentRequest,
    response: Response,
    container: ContainerDep,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=8, max_length=255)
    ] = None,
) -> PaymentOut:
    """Create a payment and run the cascade. The response carries the final state
    (succeeded / failed / requires_review) and every attempt."""
    if not idempotency_key:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "idempotency_key_required", "message": "Idempotency-Key header"},
        )
    command = CreatePaymentCommand(
        customer_id=body.customer_id,
        idempotency_key=idempotency_key,
        money=Money(amount_minor=body.amount, currency=body.currency),
        country=body.country,
        method=PaymentMethodType(body.payment_method.type),
        payment_token=body.payment_method.token,
        description=body.description,
    )
    try:
        result = await container.create_payment(command)
    except IdempotencyKeyReusedError:
        raise HTTPException(
            422,
            detail={
                "code": "idempotency_key_reused",
                "message": "this Idempotency-Key was used with a different request body",
            },
        ) from None

    if not result.created:
        payment = result.payment
        if payment.status in (PaymentStatus.CREATED, PaymentStatus.PROCESSING):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "request_in_progress", "message": "retry the same request later"},
                headers={"Retry-After": "1"},
            )
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
        return PaymentOut.of(payment)

    # The cascade runs in its own task and is shielded: if the client disconnects, the
    # cascade still reaches a consistent state instead of being cancelled mid-request.
    task = asyncio.create_task(container.process_payment(result.payment.id))
    _running.add(task)
    task.add_done_callback(_running.discard)
    payment = await asyncio.shield(task)
    return PaymentOut.of(payment)


@router.get(
    "/payments/{payment_id}", response_model=PaymentOut, responses={404: {"model": ErrorOut}}
)
async def get_payment(payment_id: UUID, container: ContainerDep) -> PaymentOut:
    """Payment state with the full attempt history."""
    payment = await container.queries.get_payment(payment_id)
    if payment is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"code": "not_found", "message": "no such payment"}
        )
    return PaymentOut.of(payment)


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(
    container: ContainerDep,
    payment_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[AlertOut]:
    """Alerts: extra charges, late approvals, failed refunds, stuck reviews."""
    return [AlertOut.of(a) for a in await container.queries.list_alerts(payment_id, limit)]
