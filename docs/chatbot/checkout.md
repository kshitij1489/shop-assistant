# Checkout

Open **Settings → Order options** and **Pricing & limits** to edit the saved policies.
Signup creates both policies before publishing conversational routes. Add a menu
item with a priced size, then finish the short setup form to confirm hours and
publish ordering. JSON import/export is optional.

Scheduling, advance booking limits, and required pickup times are disabled with
**Coming soon** labels. Checkout imports reject scheduling activation. Saving
Order options or completing setup clears old scheduling flags and required pickup
times; new chatbot checkout also ignores those old settings and drops scheduled
times from unfinished drafts. Confirmed orders retain their original details.
International delivery is also **Coming soon**: coverage and setup accept only
six-digit Indian pincodes. WhatsApp chatbot connection and Telegram bot changes
or disconnection are disabled in Integrations. Saving a WhatsApp contact number,
connecting a new Telegram bot, and re-registering its webhook remain available.

Both business and demo presets deliberately adopt these editable starter limits:
20 units per line, 30 per item, 60 per basket, 20 lines, ₹5,000 item subtotal and
₹6,000 payable. New businesses start with pickup, cash, scheduling off, zero
minimums/fees, no tax/discount rules and untracked stock. Daily 09:00–18:00
Asia/Kolkata is prefilled for confirmation; delivery requires coverage during
setup. Enable tracked stock after adding stock records. Defaults are defined in
`orders/settings_defaults.py`; the form displays saved values, including existing
tenants' intentionally absent limits. Opening Settings fills missing records
without replacing existing policies. A tenant with no `CheckoutSettings` row
keeps its legacy confirmation flow until it opens Settings or imports checkout.
Initialization leaves policy pricing inactive. Setup preserves always-open,
split and per-day schedules; edit those in Opening hours. Legacy address coverage
remains active while ordering setup is required, including after a rules save.

Pricing & limits groups currency and charges, order limits, taxes, discounts, and
advanced stock and payment settings. Add tax and Add discount create rule rows,
including item and fulfillment restrictions. The global minimum basket value and
the order-type minimum both apply before discounts, fees and added taxes; the
basket must meet the higher amount.
Prices use the selected currency (whole yen for JPY); forms convert them to integer
minor units. Existing tenants explicitly adopt policy pricing when saving Pricing
& limits. External POS and payment activation remains on the integration page.
Saving rules validates local stock readiness even with integrations disabled.
Once setup is complete, adoption also switches address validation to Checkout
coverage: pickup-only rejects delivery and an empty delivery postal-code list
allows every valid Indian pincode.

Ordering policy imports immediately update stored limits (and pricing if policy
pricing is already active). They preserve both activation flags: importing does
not adopt local policy taxes or discounts for a legacy tenant. Save Pricing
& limits or complete setup to adopt them. Integrations created without onboarding
retain the conservative model policy: absent limits and strict stock.

The form covers delivery, pickup, and dine-in; required contact and fulfillment
fields per mode; cash or online payment; preparation minutes; optional scheduling
and advance limits shown as unavailable; minimum basket values and fixed fees;
weekly hours in an IANA timezone; and delivery coverage by Indian pincode. Empty
coverage means unrestricted delivery within India. Minimums exclude fees.
Overnight hours must be split across days. Delivery always requires an address.

Cash does not call a payment provider. Online checkout needs commerce enabled
and an active payment adapter that can create and reconcile payments. Zero-total
online orders are rejected; use cash for a free order. Gateway credentials and
provider webhooks live in the adapter, not in this app.

## Conversation

`checkout`, or a mode word such as `delivery`, `pickup`, or `dine-in`, starts a
draft and asks for the next required field. It does not place an order. Use
explicit fields such as `address: 42 Main Street`, `postal code: 110001`, and
`table: 12`. Replies such as `hmm` do not fill a field. Reply `cash` or `online`
when offered a choice. Scheduling requests refer the customer to the store.

After the current quote is shown, an affirmative reply can authorize placement.
Agreement to a different question is not order confirmation. A payment request
alone does not place the order. The quote must already have been offered before
this turn. Missing fields, or a changed basket, mode, or price, require a fresh
quote.

A mode change clears mode-specific fields, scheduling, payment selection, and
the previous quote. Contact details remain. After the mode and its fees are
known, the payable total is checked against `max_payable_minor`.

`cancel checkout` drops the draft and keeps the basket. `new order` and
`start a new order` clear an unfinished basket, checkout draft, selected
address, and pending tasks. A placed order is not cancelled by that phrase.
Prefixes such as `new order status?` do not reset anything.

`RECOVER_PAYMENT` returns the existing payment link for this chat's order. It
does not create another order or another provider charge.

Changing a confirmed order means contacting the café. The accepted amount is not
edited behind an issued payment link.

## Delivery addresses

The address flow collects one free-form street line plus city, state, country,
and pincode. City, state, and country must contain letters. The pincode must be
six ASCII digits and cannot start with `0`. Tenant delivery coverage must pass.
This is format and coverage validation, not a map lookup. The chat does not
geocode, accept GPS pins, or require coordinates.

A new address is provisional until the customer confirms it. Denying it, or
starting a fresh address while it is still a draft, lets the next save replace
that row. Saved addresses are not replaced that way. Changed details invalidate
the quote.

## Orders

The checkout draft lives on the chat session. Creating the order and linking it
to the session happen in one transaction. A repeated confirmation reuses that
order. Fulfillment details are frozen on the order. External POS adapters
receive the accepted commerce snapshot.

For existing tenants that have not adopted policy pricing, catalog variant taxes are tax-exclusive: percentage taxes
apply to the variant plus modifiers, and fixed taxes apply per item, rounded
per line. Fees are untaxed on that path. New local checkout and tenants that save
Pricing & limits use policy pricing for inclusive taxes, fee taxes, and discounts,
even with external integrations disabled. Local cash orders consume local stock
atomically and create no provider commands or missing-POS issues.

Cache expiry does not drop an unfinished checkout draft, because the database holds that
draft. An empty checkout postal-code list allows delivery that passes format
checks. Local checkout uses the saved checkout coverage for address verification
as well. Legacy tenants still use `serviceable_pincodes`: a missing or malformed value does
not save or confirm an address, and an empty list means the café does not
deliver to that code. Published fee bands are not delivery coverage.
