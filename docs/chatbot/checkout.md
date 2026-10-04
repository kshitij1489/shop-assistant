# Checkout

Open **Tenant Settings → Checkout settings** and save the form. That enables the
flow below. A tenant with no `CheckoutSettings` row does not collect these
fields; confirmation places the current basket when the customer is already known.

The form covers delivery, pickup, and dine-in; required contact and fulfillment
fields per mode; cash or online payment; preparation minutes; optional scheduling
and advance limits; minimum basket values and fixed fees; weekly hours in an
IANA timezone; and delivery coverage by postal code. Empty coverage means
unrestricted delivery. Minimums exclude fees. Overnight hours must be split
across days. Delivery always requires an address. A required pickup time also
requires scheduling.

Cash does not call a payment provider. Online checkout needs commerce enabled
and an active payment adapter that can create and reconcile payments. Zero-total
online orders are rejected; use cash for a free order. Gateway credentials and
provider webhooks live in the adapter, not in this app.

## Conversation

`checkout`, or a mode word such as `delivery`, `pickup`, or `dine-in`, starts a
draft and asks for the next required field. It does not place an order. Use
explicit fields such as `address: 42 Main Street`, `postal code: 110001`, and
`table: 12`. Replies such as `hmm` do not fill a field. Reply `cash` or `online`
when offered a choice. Schedule with `YYYY-MM-DD HH:MM` in the configured
timezone, or say `as soon as possible` to drop an optional time.

After the current quote is shown, an affirmative reply can authorize placement.
Agreement to a different question is not order confirmation. A payment request
alone does not place the order. The quote must already have been offered before
this turn. Missing fields, or a changed basket, mode, or price, require a fresh
quote. Ambiguous local times around daylight-saving changes are rejected.

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

Without commerce, catalog variant taxes are tax-exclusive: percentage taxes
apply to the variant plus modifiers, and fixed taxes apply per item, rounded
per line. Fees are untaxed on that path. Use commerce pricing for inclusive
taxes, fee taxes, and discounts.
