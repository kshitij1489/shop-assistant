# Commerce API compatibility

This policy covers `/commerce/v1/connections/{connection_id}/`, command and event
envelopes (`schema_version: 1`), and the published JSON Schemas. Provider APIs
have their own versions. Menu snapshots and the stored pricing policy (schema
version 2) are separate from the API path version.

`GET schema/` publishes `event`, `acknowledgement`, `mapping`, `menu_snapshot`,
`command`, and `claim_response`. Checked-in copies:

- [command.schema.json](../../commerce/contracts/command.schema.json)
- [claim_response.schema.json](../../commerce/contracts/claim_response.schema.json)

They are generated from `commerce.command_schemas`. `command_id` and
`idempotency_key` stay stable across attempts. `lease_token`, `lease_until`,
and `attempt` may change. Do not include lease fields in a provider request
hash. Acknowledging a command does not prove a payment or POS outcome. Report
that with events.

## Changes allowed in v1

- Add optional response fields. Readers ignore unknown response properties and
  must not reconstruct an accepted snapshot from only the fields they recognize.
- Add optional request fields with backward-compatible defaults. Deploy servers
  first. Current validators reject unknown properties on incoming events,
  acknowledgements, and mappings.
- Add endpoints or new capabilities. A new command kind needs a capability the
  operator enables only after the adapter supports it. Existing capabilities
  must not start emitting unrecognized command kinds.
- Fix defects so behavior matches this contract. A change existing adapters
  could reasonably rely on is a breaking change.

Older payment-create payloads may omit `expires_at`. Payment reconciliation may
have a null or empty `external_id` until a provider ID is known. Tenant IDs in
snapshots are opaque strings. Treat every ID as an opaque value.

## Breaking changes

Removing or renaming fields or endpoints, adding required request fields,
changing types or nullability, changing command or status meanings, adding enum
values to existing capabilities, or changing money units, rounding, identity
scope, signature bytes, or idempotency requires a new major API path.

Unknown envelope versions or commands must fail closed, with no provider side
effects. For a major migration, publish notes, schemas, and a working reference
adapter before enabling the new contract. Run the old and new versions together
for at least **90 days** after the replacement is available and the deprecation
notice is published. The notice must state the affected versions, earliest
removal date, configuration, and rollback. No v1 removal date is scheduled.

Connection migration is an operator action. Drain outstanding leases and events,
or keep routing their retries to the original version. Never reinterpret an
outstanding v1 command as a new charge or order. Keep the original API until
unresolved deliveries have a documented outcome. Security fixes may use shorter
notice; state the reason, impact, and recovery steps.

Regenerate the checked-in command schemas from the repository root:

```sh
python - <<'PY'
import json
from pathlib import Path
from commerce.command_schemas import ClaimResponse, command_schema

for name, schema in {
    'command': command_schema.json_schema(),
    'claim_response': ClaimResponse.model_json_schema(),
}.items():
    Path(f'commerce/contracts/{name}.schema.json').write_text(
        json.dumps(schema, indent=2) + '\n'
    )
PY
```
