# Grove–Orca bridge, version 1

The implementation is `bridge.RemoteExchange`. One exchange has one random
`request_id`, one window, and either `connect` or `print` mode. Every message
has `type`, integer `protocol_version: 1`, and the exact `request_id`.
An unsupported version fails the exchange; unrelated request IDs are ignored.

The Print exchange requests `orca.host.ui.WINDOW_MODAL` through `create_window`,
so the host presents it as a modal dialog during Orca's existing modal Send flow.
The host returns a window handle immediately and runs its modal UI loop separately;
the plugin worker continues processing the bridge, cancellation, and receipts.
The send callback stays pending until the exchange produces its terminal result.
Connect mode uses `WINDOW_MODELESS` as before.

## Entry page and transports

The plugin opens:

```text
{grove_base_url}/integrations/orca?mode=connect|print&protocol_version=1&request_id=...
```

The configured base URL may include a reverse-proxy path. No credential or file
path is placed in the URL. The page must send `ready` within 20 seconds, before
waiting for the user to log in. Once ready, an interactive exchange has no login
deadline. Reloading the page may send `ready` again and receive initialization
again; initialization must not create a queue item.

**Connect** navigates the top-level Orca webview to Grove. Grove sends messages
through `window.orca.postMessage(payload)` and receives initialization with
`window.orca.onMessage(handler)`. Normal login/SSO navigation must preserve the
integration return URL and request ID. This mode receives no session token.

**Print** loads Grove in an iframe inside a small local Orca page. Grove sends
messages with `window.parent.postMessage(payload, '*')`; the parent has a file
origin, so a normal web origin cannot be used as the target. Only receipts and
control messages are sent this way. Grove receives initialization with a browser
`message` listener that requires `event.source === window.parent`, then validates
the protocol version and request ID. Register that listener before sending ready.

The local parent forwards a frame event to Python only if both its browser-
reported origin and source match the configured Grove origin and that iframe.
Python initialization is forwarded using `postMessage(payload, groveOrigin)`.
A redirected or unrelated frame cannot receive the user's token. This policy is
needed because Orca's host-window message callback does not expose a trusted
browser sender origin. Grove must allow a `file:` parent for **print mode on this route only**;
its normal pages and connect flow retain their existing framing policy.

## Connection exchange

Grove sends `ready`. The plugin responds:

```json
{"type":"initialize","protocol_version":1,"request_id":"...","mode":"connect"}
```

Grove uses its existing user login, including MFA/SSO when configured. Once
authenticated, it sends:

```json
{"type":"authenticated","protocol_version":1,"request_id":"...","access_token":"<user JWT>"}
```

The plugin checks the token with `GET /api/v1/auth/me`. It accepts an active
user with a positive ID and username; it rejects API keys. A successful login
closes the window and retains the session in memory. `cancelled` closes without
changing an established session. Passwords and MFA answers never enter Python.

## Print exchange

The plugin first verifies `/auth/status` and `/auth/me`, then uploads with
`POST /api/v1/library/files`, using `Authorization: Bearer <same user JWT>`.
After Grove sends `ready`, the plugin responds:

```json
{
  "type":"initialize", "protocol_version":1, "request_id":"...", "mode":"print",
  "access_token":"<user JWT>", "expected_user_id":7,
  "library_file_id":42, "filename":"bracket.gcode.3mf", "plate_id":1
}
```

`plate_id` is a positive 1-based initial selection or `null`. Treat the filename
as display context; Grove loads and authorizes the file by its library ID.

Grove must use the supplied token in memory for this document, validate it with
`/auth/me`, compare the actual account ID with `expected_user_id`, then mount its
existing full Print modal. Do not silently reuse another browser account, save
the supplied token in browser storage, or open a new login within a print flow.
Report `session_expired` and return the user to the separate connection flow.

After all intended queue requests are confirmed, Grove sends:

```json
{
  "type":"submitted", "protocol_version":1, "request_id":"...",
  "user_id":7, "library_file_id":42, "queue_item_ids":[101,102]
}
```

The IDs must enumerate all created rows, including quantity batches. They must
be positive and unique. The plugin matches the user and source file before
returning `submitted`. Use `partial` with the same receipt fields when some
requests succeeded and others definitively failed. Both are terminal events;
the plugin closes the window and never repeats accepted requests.

Other terminal events:

| Type | Meaning |
| --- | --- |
| `cancelled` | No queue requests remain in flight, no jobs were accepted, and no result is uncertain. The library file remains available. |
| `session_expired` | The user session cannot authorize this form. The plugin clears its login and reports an unknown outcome conservatively. |
| `error` | The form cannot complete or a request outcome is uncertain. The plugin reports `unknown`; a successful-looking close is insufficient. |

If the caller requests cancellation, the plugin sends `cancel_requested`. The page may answer
`cancelled` only when the condition above is true; otherwise it should finish
the in-flight operation and report its actual result. If no reply arrives within
three seconds, the plugin closes and reports `unknown`. Closing or unloading a
print window without a receipt also reports `unknown`. A receipt already
received wins a later cancellation. Do not retry queue POSTs automatically.

Tokens and full bridge payloads must not be written to logs. A missing bridge,
invalid receipt or interrupted form must never be interpreted as queue success.
