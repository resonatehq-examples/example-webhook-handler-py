"""Webhook payment workflow + step handlers.

Processes a Stripe-style payment webhook exactly once.

The promise ID is ``webhook/{event_id}`` — the natural deduplication key.
When Stripe retries a webhook (network timeout, slow ACK, 5xx response),
the same event_id arrives again. Resonate detects the promise already exists
and returns the cached result immediately — without re-executing.

Without this: customer charged twice.
With Resonate: charge runs once, period.

The crash recovery story is equally important: if the process dies after
charge_card() succeeds but before update_ledger() runs, Resonate resumes
from the charge_card checkpoint — no second charge.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Generator, TypedDict

from resonate import Context


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class WebhookEvent(TypedDict):
    event_id: str
    type: str           # "payment_intent.succeeded" | "payment_intent.failed"
    amount: int         # in cents
    currency: str
    customer_id: str


class PaymentResult(TypedDict):
    event_id: str
    charge_id: str
    status: str         # "captured"
    amount: int
    processed_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Track charge attempts per event_id — Resonate retries functions in the same
# process, so on attempt 2 this counter is 2 and the simulated crash is skipped.
_charge_attempts: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Step 1: Validate event structure and signature
# ---------------------------------------------------------------------------

def validate_event(_ctx: Context, event: WebhookEvent) -> None:
    print(f"  [validate]  Checking signature for {event['event_id']}...")
    time.sleep(0.05)
    # In production: verify Stripe-Signature HMAC against your webhook secret
    amount = event["amount"] / 100
    print(
        f"  [validate]  {event['event_id']} OK — {event['type']}, "
        f"${amount:.2f} {event['currency'].upper()}"
    )


# ---------------------------------------------------------------------------
# Step 2: Charge the card (idempotent — checkpoint prevents double charge)
# ---------------------------------------------------------------------------

def charge_card(
    _ctx: Context, event: WebhookEvent, simulate_crash: bool
) -> str:
    attempt = _charge_attempts.get(event["event_id"], 0) + 1
    _charge_attempts[event["event_id"]] = attempt

    amount = event["amount"] / 100
    print(
        f"  [charge]    Authorizing ${amount:.2f} for "
        f"{event['customer_id']} (attempt {attempt})..."
    )
    time.sleep(0.2)

    if simulate_crash and attempt == 1:
        # Simulate payment processor timeout on first attempt.
        # Resonate retries this step. The validate step is NOT re-run.
        raise RuntimeError("Payment processor timeout — will retry")

    charge_id = f"ch_{uuid.uuid4().hex[:8]}"
    print(f"  [charge]    Charge {charge_id} captured")
    return charge_id


# ---------------------------------------------------------------------------
# Step 3: Send receipt to customer
# ---------------------------------------------------------------------------

def send_receipt(
    _ctx: Context, event: WebhookEvent, charge_id: str
) -> None:
    print(
        f"  [receipt]   Emailing receipt to {event['customer_id']} "
        f"for {charge_id}..."
    )
    time.sleep(0.08)
    print("  [receipt]   Receipt sent")


# ---------------------------------------------------------------------------
# Step 4: Record transaction in accounting ledger
# ---------------------------------------------------------------------------

def update_ledger(
    _ctx: Context, event: WebhookEvent, charge_id: str
) -> PaymentResult:
    print(f"  [ledger]    Recording {charge_id} in accounting ledger...")
    time.sleep(0.06)
    result: PaymentResult = {
        "event_id": event["event_id"],
        "charge_id": charge_id,
        "status": "captured",
        "amount": event["amount"],
        "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print("  [ledger]    Transaction recorded")
    return result


# ---------------------------------------------------------------------------
# Workflow orchestrator
# ---------------------------------------------------------------------------

def process_payment(
    ctx: Context, event: WebhookEvent, simulate_crash: bool
) -> Generator[Any, Any, PaymentResult]:
    # Step 1: Validate signature and event structure
    yield ctx.run(validate_event, event)

    # Step 2: Charge the card — checkpointed.
    # If this crashes and retries, we call the payment processor exactly once.
    # If a duplicate webhook arrives with the same event_id, this step is
    # returned from cache — the processor is never called again.
    charge_id = yield ctx.run(charge_card, event, simulate_crash)

    # Step 3: Send receipt
    yield ctx.run(send_receipt, event, charge_id)

    # Step 4: Update accounting ledger
    result = yield ctx.run(update_ledger, event, charge_id)

    return result
