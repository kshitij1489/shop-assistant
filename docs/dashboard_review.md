# Dashboard review implementation

The reported code fixes and component cleanup were implemented together on
2026-10-09. The review's placeholder features remain outside this work.

| Area | Result |
| --- | --- |
| Chat selection and polling | Ignore stale responses, reuse chat/message nodes, retain keyboard focus and reading position, and follow new messages when already at the bottom. |
| Voice basket | Render catalog values as text. Save metadata only for explicit lists: `[]` clears rows and totals; `None` leaves the previous basket intact, including when routing fails or omits the basket. |
| Microphone lifecycle | Clear pending timers and speech buffers on stop, ignore late recognition results, preserve restart handlers, invalidate queued speech/restarts after cancellation, and remove unconfirmed transcript rows when an in-flight send is cancelled. |
| Voice history and polling | Give new messages stable IDs, match overlapping history windows, and share one polling flow for transcript, basket, and speech. |
| Speaking voice | Apply selector changes, retain the selection when the browser refreshes its voices, and defer `resume()`/`speak()` until a later turn after `cancel()`. |
| Variant deletion | Redirect with actionable feedback when stock records protect the variant; leave its records intact. |
| Knowledge editing | Retain unsaved entry drafts across dtype, intent, and topic changes; protect page navigation and warn before a save/delete would discard another entry's edits. Drafts remain in memory until saved or the page is left. |
| Agent controls | Return HTTP 503 for failed Redis reads/writes and keep the displayed switch unchanged when polling fails. Skip bot routing when the global setting cannot be read. |
| Owner message delivery | Propagate Redis transcript-write errors. If Telegram delivery succeeded first, return HTTP 503 with `delivered: true`, clear the matching composer, and warn against resending. Bot transcript failures are logged without sending another reply. |
| Analytics | Average elapsed time from session creation to last interaction and display a duration. |
| Order history | Use accepted order snapshots for currency, totals, variants, and modifier prices. Retain legacy item names that already include a size. New local orders save currency, exponent, and purchased modifier names so catalog renames/deletion do not rewrite those names. |
| Login and commerce pagination | Preserve Django's validated `next` destination and all query parameters when advancing an individual paginator. |
| Navigation and actions | Keep Menu selected on related pages, expose `aria-current`, and give deletion/detachment danger styling and confirmation. |
| Public chat | Preserve line breaks, label the input, and announce completed replies without announcing every streamed token. |
| Notifications and switch sizing | Pause dismissal during hover/focus and retain the tenant switch's shared 44px minimum height in both states. |
| Shared components | Reuse auth layouts, catalog field definitions/defaults, tab styling/keyboard behavior, and native dialog lifecycle. Existing notifications, JSONEditor, approvals, and separate public/dashboard shells remain shared as appropriate. |

Apply [orders migration 0022](../orders/migrations/0022_orderitemaddon_addon_name.py)
with `python manage.py migrate` using the application's configured environment
before restarting web and background workers. It adds `OrderItemAddon.addon_name`;
existing rows receive an empty value. This session validated it against disposable
PostgreSQL but did not apply it to the application database.

Old Redis messages without IDs use overlapping content/timestamp matching until
they leave the history window. Old orders without saved currency retain the legacy
INR interpretation. Modifier names absent from both an accepted snapshot and the
new field display as “Customization (original name unavailable)”; they are not
inferred from renamed catalog entries or labeled as removed. Missing historical
facts cannot be reconstructed from today's catalog reliably.

## Validation

The initial implementation's offline run passed 938 Django tests with 13 skipped (951 discovered)
and all 31 frontend checks, including ten native-browser regression cases.
Another 65 focused dashboard/ordering tests passed with PostgreSQL and real
migrations. A migration upgrade check preserved an existing modifier's identity
and prices, initialized its missing name to empty, and verified new names can be
saved. Migration consistency, Django system checks, and `git diff --check` passed.

Run the offline Django and frontend suites using the commands in
[operations/testing.md](operations/testing.md). Browser regressions use simulated
recognition, voices, and HTTP responses with a disposable Chromium profile. They
cover cancellation/poll races and require speech startup to occur after cancellation
in a later event-loop turn; they do not establish audible playback on a device.

The review's remaining manual validation can be a separate session with access to
the target browser/device and configured services: desktop/mobile visual review,
screen-reader testing, real microphone and TTS behavior, connected-service flows,
and inspection of Django admin and evaluation reports. Those interfaces had no
specific implementation findings in the supplied review.

## Follow-up verification

The five follow-up findings and the consistency cleanup were checked on
2026-10-09:

| Finding | Verified behavior |
| --- | --- |
| Customer drafts | Switching customers clears the composer; reselecting the same customer retains the draft. |
| Duplicate sends | A pending send remains guarded across chat selections; completion preserves a newer draft and handles refreshed chat objects. |
| Global switch races | A confirmed toggle invalidates older status reads; subsequent polls can still update the switch. |
| Selected-chat switch | Polling refreshes the switch with its list badge. Confirmed toggles also update both immediately by chat ID, including after reselection or a failed follow-up refresh. Repeated toggles are guarded while pending. |
| Modal notifications | Validation feedback renders inside the active dialog and can receive keyboard focus and pointer input. Remaining notifications return to the page when the dialog closes. |
| Variant fields and login | Add/edit variants share Django form fields and one template partial, including nonnegative price validation. Login inputs use the shared styles without inline overrides. |

The verification found and fixed an additional per-chat toggle race: comparing
chat objects lost a successful result after reselection if the subsequent list
refresh failed. Its browser regression failed before the correction and passed
afterward.

All 39 frontend checks passed, including 17 native-browser cases. The focused
Django run passed 113 tests with 2 skipped (115 discovered), covering dashboard
review, dashboard UI, catalog, catalog ordering, signup, and menu synchronization.
Django system checks and `git diff --check` passed. This follow-up did not rerun
the full Django suite or PostgreSQL migration checks; the manual validation listed
above remains pending.
