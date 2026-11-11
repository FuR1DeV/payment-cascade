from fastapi import APIRouter, HTTPException, Request, status

from app.modules.payments.application.ports import InvalidWebhookError, UnknownProviderError

from .dependencies import ContainerDep

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/{provider}")
async def receive_webhook(provider: str, request: Request, container: ContainerDep) -> dict:
    """One URL per provider (/webhooks/mock_alpha, /webhooks/stripe, ...). Each provider has
    its own secret and signature scheme; the raw body is verified before parsing."""
    body = await request.body()
    try:
        result = await container.handle_webhook(provider, request.headers, body)
    except UnknownProviderError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown provider") from None
    except InvalidWebhookError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return {"result": result.value}
