# UI notifications

Every configuration change must report its result through the shared notification:
save, add, edit, update, delete, detach, import, publish, activation, token generation
and secret rotation. JSON validation and formatting also report success or failure.

Notifications slide in at the top right, remain for five seconds, then slide out.
They support success, error, warning and info levels, manual dismissal, screen reader
announcements and reduced motion. Multiple results stack with independent timers.
While a modal dialog is open, notifications render inside that dialog instead.
A modal makes the rest of the page inert, so a toast outside it cannot be focused
or dismissed. Closing the dialog returns any notification that is still visible
to the top-right stack.
Automatic dismissal pauses while a notification is hovered or contains keyboard
focus. Once both interactions end, it remains visible for another five seconds.

For Django views, add `messages.success(request, 'Menu Items Saved')` after the
operation succeeds. On failure, use `messages.error` with a short, direct result.
Forms retain field errors. For errors followed by a redirect, use
`users.notifications.action_error(request, 'Menu Import Failed', exception)` to
keep detailed validation guidance on the destination page without a long popup.

For browser actions, call `notify('Token Copied')` or
`notify('Token Not Copied', 'error')`. Await the operation and check its response
before reporting success. Do not infer success from a button click or show a save
notification when a user cancels. Background polling does not generate repeated
notifications.

JSON editors use `JSONEditor.check(field)` for validation and
`JSONEditor.check(field, { format: true })` for formatting. Validate a submission
with `{ notifySuccess: false }` so that only the confirmed save reports success.
Declarative buttons use `data-json-action="validate"` (or `"format"`) and
`data-json-target="field-id"`. Forms can use `data-json-validate="textarea.json"`.

Use brief messages such as `Menu Items Saved`, `Knowledge Draft Saved`, or
`Invalid JSON`. Keep longer explanations in the form or validation details.
Do not add browser alerts or another notification renderer.
