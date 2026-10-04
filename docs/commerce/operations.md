# Commerce recovery

Use **Commerce settings → Integration failures and resolution history**
(`/commerce/operations/`) for open issues, closure history, parked commands, and
unprocessed inbox events. An issue shows the order, original command UUIDs,
connection and environment, amounts, provider references, and stock reservations.
Operators for that tenant record resolutions. Administrators have read-only
ledger access.

Protocol and limits: [integration](integration.md).

## Limits that block a fix

- One checkout location per tenant. Outstanding orders stay on the original connection, account, and environment. Switching providers does not move them.
- Cash, or one online attempt, per accepted order. Authorization alone does not fulfill. No split tenders, gift cards, tips, partial captures, or FX.
- Refund targets are cumulative. The adapter performs the provider refund.
- Refunds and POS cancellation do not restock. Do not zero `reserved` or `pending_consumed` to make an item sellable.
- There is no dashboard button that resets a command or marks an order paid.

Disabling commerce does not stop queued work or adapter workers. Pause ordering
and stop the relevant workers together before a manual refund. Stopping a worker
does not undo a provider call already in flight.

## First checks

1. Record the issue, order, accepted-order, and original command UUIDs. Compare currency, amount, expiry, and snapshot hash with the adapter receipt. The provider idempotency key is the original command UUID.
2. Confirm tenant, location, role, connection UUID, provider account, and test versus live before opening provider records. A missing webhook or an empty search does not prove that nothing happened.
3. For payments, compare identity, authorized versus captured amounts, cumulative refunds, and pending refunds. For POS, confirm acceptance, external ID, total, and whether the store has started preparation. For stock, confirm which reservation IDs the observation already includes.
4. Check live leases before any manual action. Do not delete receipts or rotate a connection into another account to skip an unresolved delivery.
5. Save ticket references and verification time. Do not store card data, secrets, payment links, or full provider bodies.

## Automatic recovery

```sh
python manage.py reconcile_commerce
```

Schedule that every minute, or rely on Celery beat. It expires holds, retries
inbox rows below ten attempts, queues submission for a confirmed order whose POS
was unconfigured, and enqueues provider read commands for eligible unresolved
payments and pending POS orders. It does not reissue parked money-moving
commands. Running it does not mean a particular issue was fixed.

Have the adapter send verified `payment.updated`, `order.updated`, or
`inventory.updated` events on the original connection. An acknowledgement is
only a delivery outcome. Do not edit snapshots or counters in SQL.

## Replay

| Situation | What to do |
| --- | --- |
| Inbox processing failed; the event was correct | Fix the dependency and let reconciliation retry, or redeliver the same event ID and payload with a fresh signature. After ten attempts, automatic retry stops; an exact redelivery can still be processed. |
| The event described the wrong state | Send a new event ID and a higher sequence. Never reuse an event ID with different content. Money totals cannot decrease. |
| A provider read failed | Read the original reference again. A read must not create a resource. |
| Create or refund definitely did not apply, or the provider deduplicates | Resume the original receipt only after checking expiry, the same account and request, key retention, and no concurrent work. Do not mint a new command. Do not recreate an expired checkout. |
| Timeout, `unknown`, or exhausted delivery | Look up the original provider operation first. If search is eventually consistent, absence is not permission to replay. |
| Ambiguous refund | Reconcile the cumulative target against completed and pending refunds. Do not refund the full target again because an acknowledgement was lost. |

Command leases last 120 seconds. Only the current lease can acknowledge. Ten
attempts, or an `unknown` or `failed` acknowledgement, parks the command.

## Common issues

| Issue | Action |
| --- | --- |
| `adapter_unknown`, `adapter_failed`, `command_exhausted` | Recover the provider observation, or document that the operation did not apply. A parked row can remain after a later successful observation. |
| `pos_unconfigured` | Restore the POS connection. Reconciliation queues submission and may close this issue as `system:pos_submit`. Queuing is not acceptance. |
| `payment_identity_mismatch` | Compare currency, payment ID, external ID, and account. Never bind another charge to this order. |
| `payment_amount_mismatch`, `late_payment`, `stock_commitment_shortfall` | The order stays unpaid and in review. Held stock is released. Inspect any queued refund before acting. Without `payment.refund`, recover the money at the provider and report the cumulative refund. |
| `refund_received` | Confirm the cumulative amount and whether the order was fulfilled. Do not restock automatically. |
| `pos_rejected`, `pos_cancelled`, `pos_status_conflict` | Ask the store about preparation and the refund obligation. Capture and consumed stock stay recorded. |
| Failed or stale `inventory.updated` | Send a new authoritative count, or edit locally owned stock in the dashboard. Acknowledge reservations only when that count includes those sales. |

## Closure

Fill **Provider verification and evidence** and **Actions taken and final
disposition**, then confirm the checkbox. Include account, environment, amounts,
verification time, ticket, replay justification, refund IDs, recovery event IDs,
and the stock decision. Leave the issue open while the provider outcome is
uncertain.

Closure records the signed-in user and server time. It does not refund, replay,
fulfill, restock, or clear other issues. A later occurrence opens a new issue.
If the customer still needs the goods, take a new confirmed checkout only after
the old order's provider obligations are settled.
