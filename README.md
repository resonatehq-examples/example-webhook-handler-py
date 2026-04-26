<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/banner-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="./assets/banner-light.png">
    <img alt="Webhook Handler — Resonate example" src="./assets/banner-dark.png">
  </picture>
</p>

# Webhook Handler

**Resonate Python SDK**

Exactly-once webhook processing with automatic deduplication. Models a Stripe-style payment webhook receiver: validate → charge → receipt → ledger. If the webhook is retried (network timeout, slow ACK), the payment is not processed twice — Resonate deduplicates via the event ID.

## What This Demonstrates

- **Idempotent webhook processing**: same `event_id` → same result, never executed twice
- **Exactly-once side effects**: payment charged once, receipt sent once, ledger updated once
- **Durable crash recovery**: if the process dies after charging but before sending the receipt, it resumes from the charge checkpoint — not from scratch
- **HTTP webhook pattern**: FastAPI endpoint returns 200 immediately; processing is asynchronous

## How It Works

The `event_id` from Stripe's webhook payload becomes the Resonate promise ID:

```python
resonate.begin_run(f"webhook/{event['event_id']}", process_payment, event, simulate_crash)
```

If Stripe retries the same `event_id`, Resonate finds the existing promise and returns it immediately — without re-executing. No database deduplication table required. No Redis lock. The durability guarantee comes for free.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

The Python SDK runs in embedded local mode — no Resonate server required for this example.

## Setup

```bash
git clone https://github.com/resonatehq-examples/example-webhook-handler-py
cd example-webhook-handler-py
uv sync
```

## Run It

**Deduplication mode** — same webhook delivered twice, processed once:
```bash
uv run python main.py
```

```
=== Webhook Handler Demo ===
Mode: DEDUPLICATION (same webhook sent twice, processed once)

--- First delivery of evt_1771882806897 ---

[webhook]    Received evt_1771882806897 (payment_intent.succeeded)
  [validate]  Checking signature for evt_1771882806897...
  [validate]  evt_1771882806897 OK — payment_intent.succeeded, $49.99 USD
  [charge]    Authorizing $49.99 for cus_alice (attempt 1)...
  [charge]    Charge ch_vvjywpy5 captured
  [receipt]   Emailing receipt to cus_alice for ch_vvjywpy5...
  [receipt]   Receipt sent
  [ledger]    Recording ch_vvjywpy5 in accounting ledger...
  [ledger]    Transaction recorded

--- Stripe retries evt_1771882806897 (simulating network timeout on first delivery) ---

[webhook]    Received evt_1771882806897 (payment_intent.succeeded)

=== Result ===
{
  "event_id": "evt_1771882806897",
  "charge_id": "ch_vvjywpy5",
  "status": "captured",
  "amount": 4999,
  "processed_at": "2026-04-25T21:40:07Z"
}

Notice: validate/charge/receipt/ledger each logged exactly ONCE.
The retry returned the cached result — no duplicate charge.
```

**Crash mode** — payment processor fails on first attempt, retries:
```bash
uv run python main.py --crash
```

```
Mode: CRASH (payment processor times out on first attempt, retries once)

  [validate]  Checking signature for evt_...
  [validate]  evt_... OK — payment_intent.succeeded, $49.99 USD
  [charge]    Authorizing $49.99 for cus_alice (attempt 1)...
  [charge]    Authorizing $49.99 for cus_alice (attempt 2)...
  [charge]    Charge ch_k0d09pgw captured
  [receipt]   Emailing receipt to cus_alice for ch_k0d09pgw...
  [receipt]   Receipt sent
  [ledger]    Recording ch_k0d09pgw in accounting ledger...
  [ledger]    Transaction recorded
```

## What to Observe

1. **Deduplication**: validate/charge/receipt/ledger each log exactly once, even though the webhook arrives twice. The second delivery doesn't trigger any reprocessing.
2. **The cached result**: the second webhook returns the same `charge_id` from the first run — not a new charge.
3. **Crash recovery**: in crash mode, validate runs once. Charge fails then succeeds. The customer is charged exactly once — the retry was at the function level, not the workflow level.
4. **No dedup table needed**: no database, no Redis, no distributed lock. The promise ID is the deduplication key.

## The Code

The entire workflow is a small generator in [`workflow.py`](workflow.py):

```python
def process_payment(ctx, event, simulate_crash):
    yield ctx.run(validate_event, event)
    charge_id = yield ctx.run(charge_card, event, simulate_crash)
    yield ctx.run(send_receipt, event, charge_id)
    result = yield ctx.run(update_ledger, event, charge_id)
    return result
```

The deduplication is in the entry point, one line:

```python
resonate.begin_run(f"webhook/{event['event_id']}", process_payment, event, False)
```

That's it. If `event_id` already exists in the promise store, Resonate returns the cached result. If not, it creates a new execution.

## File Structure

```
example-webhook-handler-py/
├── main.py        Entry point — FastAPI server + demo runner
├── workflow.py    Payment workflow + step handlers (validate, charge, receipt, ledger)
├── pyproject.toml
└── .python-version
```

## Dedup by promise ID

The deduplication mechanism is the promise ID itself. When the webhook handler calls `resonate.begin_run(event_id, process_payment, ...)`, the `event_id` becomes the durable promise ID. A second invocation with the same `event_id` returns the cached result of the first — no re-execution, no double-charge, no separate idempotency table to maintain.

The same mechanism that provides durability provides idempotency. That's it.

## Running against a Resonate Server

This example runs in embedded local mode by default. If you want to point it at a standalone server:

```bash
resonate serve --aio-store-sqlite-path :memory:
```

> The Python SDK (v0.6.x) speaks the legacy server protocol. Use `resonate serve` (legacy server), not `resonate dev`.

Then swap `Resonate.local()` for `Resonate.remote()` in `main.py` and start the worker normally.

## Learn More

- [Resonate documentation](https://docs.resonatehq.io)
- [Stripe webhook best practices](https://stripe.com/docs/webhooks/best-practices) — upstream guidance on webhook dedup at the provider boundary
