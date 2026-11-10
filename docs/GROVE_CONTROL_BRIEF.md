# Grove Control change brief: hosted Orca login and Print form

**Goal:** let Orca's native **Print plate** flow pass its finished slice to the
Grove Queue PrinterAgent. The plugin uploads it as a logged-in Grove user,
then displays Grove's existing full Print modal inside Orca. Grove should own all
printer/model selection, job options, validation and queue writes so future
Print-modal changes reach the plugin automatically.

## Required changes

1. **Add `/integrations/orca`**, outside the main navigation layout, with
   `mode=connect|print`, `protocol_version=1` and an opaque `request_id`. Implement
   the [bridge protocol](BRIDGE_PROTOCOL.md). Send `ready` before waiting for
   login; an existing SPA catch-all page is not sufficient.
2. **Connect mode:** reuse normal Grove login, MFA/SSO and redirect handling.
   Return the user's JWT to the native bridge after authentication. Grove user
   authentication must be enabled. The plugin verifies the account through the
   existing `/auth/me` endpoint and revokes its token through `/auth/logout` on
   sign-out. No API-key setup or new login endpoint is needed.
3. **Print mode:** receive the session and library ID through the bridge, use
   the supplied JWT in memory, verify its actual user ID against the expected
   account, then mount the shared `PrintModal` in create mode. Initialize its
   plate selection from the optional 1-based plate ID; reject an invalid supplied
   plate instead of silently choosing another. Preserve the complete existing
   printer/model, copies, material, scheduling and print-option controls.
4. **Return complete submission receipts.** Add a backward-compatible
   `created_queue_item_ids` list to the existing `POST /api/v1/queue/` response,
   including every row created by `quantity`. Preserve its existing `id` and
   response fields. Extend the shared modal with an optional result callback
   that collects IDs across printer/plate requests and reports success, partial
   success or an uncertain outcome. Its existing `onSuccess`/`onClose` behavior
   for other callers can remain compatible. The integration page emits a
   matching user/file receipt; it never retries accepted requests automatically.
5. **Allow framing only for the integration Print page.** Orca hosts this page
   in an origin-checked iframe whose parent has a `file:` origin. Add that parent
   scheme to this route's `frame-ancestors` policy in print mode and omit its
   conflicting `X-Frame-Options` header. Keep the normal site and connect-mode
   framing policy. Preserve the other CSP directives. No global CORS relaxation
   is needed: the embedded application calls Grove's own origin.

The plugin's dock panel is configuration-only. Its native PrinterAgent callback
uses `GET /api/v1/auth/status`, `GET /api/v1/auth/me`,
`POST /api/v1/auth/logout` and `POST /api/v1/library/files?temporary=true`. Queue creation stays
in Grove. Give print-mode document authentication an in-memory option so it does
not overwrite a separate remembered browser account or write the bridged token
to storage. Mount user-dependent providers only after that session is verified.

## Acceptance

- Uploads and queue rows have the same authenticated `created_by_id`; account
  mismatches, expired sessions and missing permissions fail without anonymous
  fallback. Print mode requests reconnection rather than changing users inline.
- Single/multiple copies and multi-plate/printer submissions return every
  confirmed ID. Partial success and timeouts cannot be reported as full success
  or retried automatically; close/cancel behavior follows the protocol.
- Cancelling leaves the uploaded file in Temporary Files until Grove's
  configured retention period expires. Grove cleans up after dispatch once
  no other live queue item needs the file. Users can move it out to keep it.
- The ordinary Grove Print modal still works, and its later UI changes appear
  in Orca without copying UI code into the plugin. Verify authentication,
  framing and bridge delivery in real Orca nightlies on the supported platforms.

Likely touchpoints: the React router, authentication/API client, shared
`PrintModal`, queue response schema/route, and security-header middleware.
These Grove changes enable the plugin's native callback upload and hosted
Print-form flow. They do not depend on Orca adding a generic Send destination;
the plugin uses Orca's experimental LAN `PrinterAgent` route and
`start_local_print()` callback as described in the [Orca brief](ORCA_SLICER_BRIEF.md).
