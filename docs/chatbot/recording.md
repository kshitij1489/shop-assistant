# Conversation recording

Record conversation evidence, inspect sessions and turns in the tenant dashboard,
export saved events, and replay the conversation graph with captured dependencies.
Capture is optional and disabled by default.

## Enable recording

Apply the release migrations and collect static assets before enabling capture:

```sh
python manage.py migrate
python manage.py collectstatic --noinput
```

Configure these settings in the environment file used by your deployment:

| Setting | Default | Purpose |
| --- | --- | --- |
| `CONVERSATION_RECORDING_ENABLED` | `false` | Enable capture of new conversations |
| `CONVERSATION_RECORDING_REDIS_URL` | `redis://redis:6379/3` | Recording stream endpoint; keep separate from the application cache |
| `CONVERSATION_RECORDING_TIMEOUT` | `0.1` seconds | Producer connection and socket timeouts, without retries |
| `CONVERSATION_RECORDING_MAX_EVENT_BYTES` | `4194304` | Maximum serialized size per event; larger events produce an evidence gap |

Restart web and background workers with the updated environment, and start the
`recording_writer` Compose service. Outside Compose, run the writer as a supervised
process with the same database and recording Redis configuration:

```sh
python manage.py write_conversation_events
```

The writer drains existing events even when new capture is disabled. Its
`--batch-size` defaults to 100; `--reclaim-ms` defaults to 60000. `--once` performs
one consumption cycle for diagnostics. If changing the recording Redis endpoint,
disable capture and drain the old endpoint first; changing the URL does not move
pending entries.

## Inspect and export

Open **Recordings** in the tenant dashboard at
`/accounts/tenant-dashboard/recordings/`. Sign in as an active, approved tenant
account. A master or superuser must explicitly select an impersonated tenant.
Tenant authority comes from the authenticated account.

1. Search by channel, recording session digest, Telegram/WhatsApp chat ID, or turn
   UUID. Browser session credentials are hidden. New events appear after the
   writer commits them; saved recordings remain available when capture is disabled.
2. Open a session to browse turns, input/reply previews, event counts and status.
   Open a turn to see segment diagnostics and the event timeline. Expand an event
   for its complete saved envelope, including model calls, state and errors.
3. Filter the timeline by event name or segment UUID. Lists contain 25 entries per
   page; message previews contain at most 240 characters. Expanded envelopes and
   exports preserve the full saved content.
4. Use **Export turn JSON** or **Export session JSON** to download every persisted
   event in that scope, regardless of pagination or timeline filters.

Inspection and export are authenticated GET routes. Unknown or out-of-scope
sessions and turns return 404. Responses prohibit caching, and saved text is
escaped before display. Exporting does not run conversation steps or load live
session state.

Exports use `format: "studio-desk.conversation-recording"` and `format_version: 1`.
They include the saved envelopes, SQL recording timestamps, scope, event counts,
and completeness summaries from the same query. Events are ordered by turn UUID,
segment UUID and sequence; the dashboard timeline uses occurrence time. Sequence
numbers define the order within a segment. Exports larger than 1 MiB spill to a
private temporary file, which closes after delivery. Preparation needs time and
temporary disk space proportional to the export size.

## Captured evidence and status

Recording captures original and routed channel input; customer and session state;
published configuration; catalog and retrieved evidence; cache operations;
assembled model messages, settings, raw responses, validated output and failures;
workflow and checkout operations; selected follow-ups and final replies; session
publication; and delivery observations.

Accepted orders link later command, payment, POS, cancellation and expiry events
to their originating turn. This private provenance stays out of adapter wire
payloads. Commit notifications snapshot their values immediately and enqueue
after the business transaction commits. Audio files and raw credential headers
are not recorded; voice ingress captures metadata and the resulting transcript.

Each segment starts with `recording.started` and ends with `recording.completed`.
**Complete evidence** requires an original turn segment, contiguous sequences,
matching counts and no reported drops for every currently persisted segment.
Missing markers, dropped or oversized events, and gaps remain visible. Execution
status is separate: a failed execution can have complete evidence, and a recovered
failure can coexist with a successful turn.

Late delivery and commerce activity have independent segments under the same turn
ID. Later commits can change the displayed completeness. Entirely lost turns or
segments cannot be detected. Delivery statuses describe server observations;
they do not confirm that a customer received or read the message.

## Replay a graph or step

Open a recorded turn and choose **Replay** to run the conversation graph or one
captured workflow step. Replay requires authenticated tenant access and a
CSRF-protected POST. Results are transient and leave the saved recording unchanged.

Operators can also replay persisted evidence or an exported turn/session:

```sh
# Omit --step-id to replay the conversation graph.
python manage.py replay_conversation --tenant-id 123 --turn-id TURN_UUID \
  --step-id STEP_UUID --output replay-result.json

# Replay an export without loading application settings or querying the database.
python manage.py replay_conversation --settings=tests.settings.integration \
  --input recording-turn.json --tenant-id 123 --turn-id TURN_UUID \
  --output replay-result.json
```

The command's tenant ID is an explicit operator scope. A session export must
select one turn. The command exits nonzero for divergence, dependency mismatches,
execution errors or unavailable evidence.

Replay starts after session loading and checkout recovery. It reruns the current
workflow routing, action binding, follow-up selection, clarification budgeting
and graph edges. Model, retrieval and business-handler results are saved fixtures;
their internals are not reexecuted. Runner preparation, post-graph publication,
delivery and asynchronous commerce workers are outside this replay boundary.

Every dependency must match the next saved call's name and typed arguments.
Unexpected, reordered, changed or unused calls stop replay without a live fallback.
Fixtures mutate detached objects only. Database queries, network connections,
process launches and file writes are blocked during the supported workflow path.
These guards do not sandbox arbitrary Python code.

Reports distinguish `matched`, `diverged`, `mismatch` and `error`. A matched replay
can reproduce a supported recorded failure; it does not establish that the
original order or delivery succeeded. Reports include per-step comparisons,
consumed dependency counts and the isolated resulting state.

Replay requires a complete original turn segment, typed state checkpoints and
paired dependency calls. Recordings without those checkpoints remain inspectable
and exportable. Unsupported serialized values or unavailable redacted data make
replay unavailable rather than loading live credentials or state.

## Storage, privacy and durability

Credentials are redacted recursively, known tenant/application secrets are masked
in text, and browser session credentials become keyed digests. Events and exports
still contain customer messages, addresses and order data. Restrict database,
Redis and export access, and choose a retention policy before enabling sustained
collection. SQL evidence has no automatic expiry. Tenant deletion cascades to
saved evidence subject to the existing business-history deletion protections.

Requests wait for a short Redis `XADD` acknowledgement; SQL persistence is
asynchronous. After a handoff failure, capture stops ordinary enqueue attempts
and attempts one final completion marker. Capture errors do not retry model calls
or business operations. A crash before the first handoff can leave no discoverable
turn, and a crash after a business commit can lose its notification. There is no
transactional outbox or zero-loss guarantee.

The writer uses stream `conversation-recording:v1` and consumer group
`postgres-writers-v1`. It inserts immutable events idempotently, checks scope and
sequence conflicts, acknowledges only after SQL commit, then deletes acknowledged
stream entries. Abandoned pending entries are reclaimed. Malformed or conflicting
events remain pending for operator investigation without blocking unrelated rows.

Compose uses Redis AOF persistence and `noeviction`, with `REDIS_MAXMEMORY=512mb`
and `REDIS_MEMORY_LIMIT=1g`. Reserve headroom for AOF buffers and process overhead.
Databases share process memory even when the recording stream uses DB 3. At the
limit, Redis rejects writes; this can also affect sessions and the broker. Use a
separate Redis instance when memory isolation is needed.

`REDIS_APPENDFSYNC` defaults to `everysec`, so accepted events can still be lost
in a host/storage crash. `always` waits for an AOF fsync; account for its disk
latency in producer timeouts. Durability still depends on storage and the host.

Monitor capture warnings, writer errors, Redis memory, stream length, pending-entry
age and SQL disk usage. Investigate repeated malformed/conflicting events before
restarting consumers. Never trim unacknowledged events with `MAXLEN` or clear the
stream with `FLUSHDB` to relieve pressure; pause capture and repair/drain the writer.

See [testing](../operations/testing.md) for the offline test settings and disposable
PostgreSQL setup. Recording and replay regression modules are listed in
[tests/README.md](../../tests/README.md).
