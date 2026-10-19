"""
FastAPI webhook server — receives TradingView alerts and forwards to IBKR.

Start with:
    uvicorn app:app --host 0.0.0.0 --port 8080

Then expose via ngrok (local dev):
    ngrok http 8080

Set the TradingView webhook URL to:
    https://<ngrok-id>.ngrok-free.app/webhook?token=YOUR_SECRET
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from config import settings
from models import parse_alert
from order_executor import OrderExecutor
from risk_manager import RiskManager

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("webhook.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── App lifecycle ──────────────────────────────────────────────────────────────
executor = OrderExecutor()
risk = RiskManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    executor.connect()
    yield
    executor.disconnect()


app = FastAPI(
    title="TradingView → IBKR Webhook",
    description="Receives TradingView alerts and executes ASX orders via IBKR.",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    connected = executor.ib.isConnected() if hasattr(executor.ib, "isConnected") else False
    return {"status": "ok", "ibkr_connected": connected}


# ── Webhook endpoint ───────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(
    request: Request,
    token: str = Query(default=""),
):
    """
    POST /webhook?token=YOUR_SECRET
    Body: JSON matching the Alert schema (see models.py for field definitions).
    """
    # ── 1. Authenticate ────────────────────────────────────────────────────────
    if settings.WEBHOOK_SECRET:
        if token != settings.WEBHOOK_SECRET:
            logger.warning("Webhook received with invalid token")
            raise HTTPException(status_code=401, detail="Invalid or missing token")

    # ── 2. Parse body ──────────────────────────────────────────────────────────
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")

    logger.info("Alert received: %s", body)

    try:
        alert = parse_alert(body)
    except ValidationError as exc:
        logger.warning("Alert validation failed: %s", exc)
        raise HTTPException(status_code=422, detail=exc.errors())

    # ── 3. Risk check ──────────────────────────────────────────────────────────
    passed, reason = risk.check(alert)
    if not passed:
        logger.warning("Order REJECTED by risk manager: %s", reason)
        return JSONResponse(
            status_code=200,
            content={"status": "rejected", "reason": reason},
        )

    # ── 4. Execute order ───────────────────────────────────────────────────────
    try:
        result = executor.execute(alert)
    except ValueError as exc:
        logger.error("Order execution error: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error during order execution: %s", exc)
        raise HTTPException(status_code=500, detail="Order execution failed")

    return {"status": "ok", "order": result}


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False)
