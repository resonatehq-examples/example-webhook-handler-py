"""Webhook handler — entry point.

Mirrors the TypeScript reference: a FastAPI server that receives Stripe-style
payment webhooks, plus a demo runner that exercises deduplication and crash
recovery against the running server.

Run modes::

    python main.py            # deduplication demo
    python main.py --crash    # crash + retry demo

The server runs on port 3000. Each demo POSTs to /webhook, polls /status,
and prints the result.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from resonate import Resonate
import uvicorn

from workflow import WebhookEvent, process_payment

# ---------------------------------------------------------------------------
# Resonate setup — local mode (in-memory, no server required)
# ---------------------------------------------------------------------------

resonate = Resonate.local()
resonate.register(process_payment)

# Whether the demo should ask the workflow to simulate a payment-processor
# crash on the first attempt. Set from CLI args at startup.
SIMULATE_CRASH = "--crash" in sys.argv

# ---------------------------------------------------------------------------
# FastAPI webhook server
# ---------------------------------------------------------------------------

app = FastAPI()


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, Any]:
    """Receives Stripe-style payment events.

    The event_id becomes the Resonate promise ID — the deduplication key.
    If the same event_id arrives twice (Stripe retry), the second call finds
    the existing promise and returns immediately. No double-processing.
    """
    event: WebhookEvent = await request.json()

    if not event.get("event_id") or not event.get("type"):
        raise HTTPException(status_code=400, detail="Missing event_id or type")

    print(f"\n[webhook]    Received {event['event_id']} ({event['type']})")

    # Fire and forget — Stripe needs a fast 200 OK (within 5 seconds).
    # Processing happens durably in the background.
    resonate.begin_run(
        f"webhook/{event['event_id']}",
        process_payment,
        event,
        SIMULATE_CRASH,
    )

    # Acknowledge receipt immediately
    return {"received": True}


@app.get("/status/{event_id}")
def status(event_id: str) -> dict[str, Any]:
    """Poll for processing result."""
    try:
        handle = resonate.get(f"webhook/{event_id}")
    except Exception as exc:  # pragma: no cover - not_found path
        raise HTTPException(status_code=404, detail="not_found") from exc

    if not handle.done():
        return {"status": "processing"}

    return {"status": "done", "result": handle.result()}


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------

PORT = 3000


def _post_webhook(event: WebhookEvent) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/webhook",
        data=json.dumps(event).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()


def _get_status(event_id: str) -> dict[str, Any]:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/status/{event_id}"
    ) as resp:
        return json.loads(resp.read())


def run_demo() -> None:
    # Wait for the uvicorn server to come up.
    time.sleep(0.5)

    event: WebhookEvent = {
        "event_id": f"evt_{int(time.time() * 1000)}",
        "type": "payment_intent.succeeded",
        "amount": 4999,
        "currency": "usd",
        "customer_id": "cus_alice",
    }

    if SIMULATE_CRASH:
        # ---------------------------------------------------------------
        # Crash demo: payment processor fails on attempt 1, Resonate retries.
        # validate() runs once. charge_card() fails then succeeds.
        # send_receipt() and update_ledger() only run after charge succeeds.
        # ---------------------------------------------------------------
        print("=== Webhook Handler Demo ===")
        print(
            "Mode: CRASH (payment processor times out on first attempt, "
            "retries once)\n"
        )
        print(f"--- Sending webhook {event['event_id']} ---")
        _post_webhook(event)

        # Wait for retry to complete (~4 seconds with retry backoff)
        time.sleep(5)

        result = _get_status(event["event_id"])
        print("\n=== Result ===")
        print(json.dumps(result.get("result"), indent=2))
        print(
            "\nNotice: validate ran once. Charge failed -> retried -> "
            "succeeded.\nThe customer was charged exactly once."
        )
    else:
        # ---------------------------------------------------------------
        # Deduplication demo: same webhook arrives twice (Stripe retry).
        # The payment runs once — the second webhook returns from cache.
        # ---------------------------------------------------------------
        print("=== Webhook Handler Demo ===")
        print(
            "Mode: DEDUPLICATION (same webhook sent twice, processed once)\n"
        )

        print(f"--- First delivery of {event['event_id']} ---")
        _post_webhook(event)

        # Wait for processing to complete
        time.sleep(0.7)

        print(
            f"\n--- Stripe retries {event['event_id']} "
            f"(simulating network timeout on first delivery) ---\n"
        )
        _post_webhook(event)

        # No new logs should appear — the workflow is already done
        time.sleep(0.3)

        result = _get_status(event["event_id"])
        print("\n=== Result ===")
        print(json.dumps(result.get("result"), indent=2))
        print(
            "\nNotice: validate/charge/receipt/ledger each logged exactly "
            "ONCE.\nThe retry returned the cached result — no duplicate charge."
        )

    # Stop the server thread once the demo is finished.
    import os
    os._exit(0)


def main() -> None:
    threading.Thread(target=run_demo, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
