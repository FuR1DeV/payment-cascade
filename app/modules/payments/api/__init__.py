from fastapi import APIRouter

from . import payments, stats, webhooks

router = APIRouter()
router.include_router(payments.router)
router.include_router(webhooks.router)
router.include_router(stats.router)
