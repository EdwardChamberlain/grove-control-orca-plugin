# LAN adapter validation report

## Native Print transport behind IIS (2026-11-11)

- A reported Print-page connection error after successful sign-in is consistent
  with IIS rejecting the old iframe. The actual server's response headers were
  not inspected, so its specific cause is not confirmed.
- Print now opens the saved Grove URL directly in the modal webview with
  `transport=native`. The matching Grove frontend supports the native bridge
  while retaining the iframe transport for older plugins.
- The browser signs in normally. The plugin validates its account against
  Grove before sending file details, never sends a token into the document,
  and checks the verified browser token on every terminal receipt. Existing
  file/account matching, cancellation, and no-retry behavior remain in place.
- A Playwright test opens the actual Grove Print form with mocked APIs and
  `X-Frame-Options: SAMEORIGIN` plus CSP `frame-ancestors 'none'`, confirms there
  is no iframe, and cancels with no queue POST. No real login or job was sent.
- The user's IIS server and native Orca webview still need a live retry after
  installing both updates. The sections below describe earlier builds.

**Status (2026-10-17):** the user reports that Grove's modal Print form now works and accepts the job. Orca's native **Send print job** dialog remains open after Grove confirms queue creation. Plugin inspection shows it returns `0` after validating Grove's matching receipt; live Orca logs are needed to determine whether Orca receives that return and processes its dialog-close event.

## Implemented

- Replaced the cloud-shaped logical device with a LAN discovery record for `127.0.0.1:49381` and the P1S model ID `C12`.
- Matched the device ID Orca derives from the cloned preset's host and port.
- Kept Orca's LAN protocol connection separate from Grove authentication so its `get_version` and `pushall` handshake can complete while signed out.
- Supplied the minimum P1S/FDM, adapter-version, idle, and Grove-upload-readiness fields required by Orca's Send flow. These are documented compatibility fields, not physical printer telemetry.
- Refreshes Orca status/capabilities when Grove sign-in, sign-out, or connection verification changes. The `verify_job` preflight and native callback still block uploads until Grove is ready.
- Routed `start_local_print` and `start_local_print_with_record` through the existing guarded archive/upload/form/receipt path. The `verify_job` preflight only checks readiness; unsupported physical commands and cloud fallback are rejected.
- Updated install steps and LAN contract documentation. No Orca source or Grove server code was changed.

## Automated validation (2026-10-16)

- Ruff lint: passed.
- Ruff formatting check: passed.
- Unit suite: **57 tests passed**, including artifact validation, account and receipt handling, LAN metadata synchronization while signed out, readiness transitions, device identity, readiness-only preflight, unsupported command rejection, and single-flight submission.
- Wheel: built successfully as `dist/grove_control_orca_plugin-0.1.0-py3-none-any.whl`.
- Source distribution: built as `dist/grove_control_orca_plugin-0.1.0.tar.gz`; the local virtual environments are excluded.
- The wheel version is `0.1.0` because this checkout has no Git metadata/tag; `pyproject.toml` still uses `hatch-vcs`, and its packaging test confirms a `v2.3.4` tag produces version `2.3.4`.

## Live validation still required

- The Orca source contract was inspected at commit `82aee1abdd5c475cff4e3adcb9d8d56078df2c10`. That source has the LAN callbacks and routing path used here, but source inspection does not prove a live build accepts the adapter state.
- Orca marks device information ready only after receiving a version module and at least one full push-status message. The screenshot's timeout means this handshake was not completed by the installed build. The adapter now answers these local metadata requests independently of Grove authentication; verify this change in the target nightly.
- No controlled plugin-enabled nightly session was used to open the populated selector, explicitly select Grove Queue, or confirm that Print plate reaches the plugin callback. The open Orca session already had an unrelated Send workflow in progress and was left untouched. The exact nightly version/build is therefore **not recorded**.
- No real Grove upload or queue job was submitted. The hosted form, upload permissions, and receipt behavior still need testing against an authorized test destination.
- Windows has not been tested. macOS GUI readiness, multiple-filament mapping, cancellation during upload, session expiry, server loss/recovery, plugin reload, and the visible selector behavior remain unverified in Orca.
- Orca issue [#16565](https://github.com/OrcaSlicer/OrcaSlicer/issues/16565) remains a separate selector-rendering blocker. A populated entry still must be explicitly selected. This implementation does not claim to fix the clipped/obscured popup.

Until those GUI checks pass, treat this build as a prototype for testing, not as ready for routine printing. Follow the P1S preset settings in the [README](../README.md); do not use a real queue destination until the user authorizes that test.

## Follow-up build (2026-10-16)

- The user reports that the previous rebuilt plugin still showed the same device-information timeout. That report confirms the previous live attempt failed; it does not establish which callback or reply Orca missed.
- The earlier local wheel was still version `0.1.0`, making it hard to tell installed builds apart. The current fallback build is `0.1.1`.
- The LAN connection gate now requires the advertised adapter ID and loopback host, but tolerates an empty or preset-derived port. The adapter also marks its version response as successful. This addresses a plausible mismatch in Orca's connection parameters, but remains a hypothesis until a live handshake succeeds.
- Automated validation now passes **58 tests**, Ruff lint/format, and a new wheel/sdist build. The wheel is `dist/grove_control_orca_plugin-0.1.1-py3-none-any.whl`.
- Subsequent user testing still reported a timeout; see the `0.1.2` correction below. The plugin manager's generic “active and ready” status does not include PrinterAgent callback traffic.

## Metadata ordering correction: 0.1.2 (2026-10-16)

- The user reports the same synchronization failure after installing `0.1.1`.
- The installed `0.1.1` package on disk matches the workspace source. A local Orca log identifies application build `3880f2c5` and records failed `get_version` and `pushall` requests. That log spans an earlier plugin session and a later install, so it cannot confirm the exact callback sequence in the latest screenshot.
- Source from build `3880f2c5` confirms that the Send dialog requests metadata before it may connect the LAN device, and can skip explicit connection when the device is already selected. The previous tests connected the adapter before every metadata request and missed this ordering.
- Supported read-only metadata requests for the exact adapter ID now initialize the logical local session and return version/full-status messages. Authentication, upload preflight, archive validation, and receipt checks remain required for sending jobs.
- The configuration panel displays the loaded plugin version and bounded diagnostics: explicit connection count, metadata request counts, successful callback invocations, registered callbacks, and exception classes. Diagnostics exclude payloads, credentials, filenames, and exception messages. A returned callback does not prove Orca accepted its contents.
- The setup guide now uses the discovered Grove Queue LAN device. It corrects the earlier instruction to enter host/port in Basic information, which does not expose those fields in the inspected UI.
- Regression suite: **61 tests passed**, including metadata-before-connection, omitted/later explicit connection, signed-out upload gating, and safe callback-error diagnostics. The current package uses fallback version `0.1.2`.
- The live `0.1.2` Send workflow remains unverified. If synchronization still fails, copy **Orca connection diagnostics** from the Grove panel immediately after reproducing.

## Host permission correction: 0.1.3 (2026-10-17)

- The user reports that synchronization now completes and Send becomes enabled. Sending then reports “Could not reach Grove” while Orca retains its Send dialog.
- The local log identifies the cause: Orca blocks `http.client.connect`, `socket.getaddrinfo`, and `socket.__new__` for plugin `0.1.2`, each followed immediately by `print_job: failed, result = -1`. These are permission denials, not evidence of a Grove outage. The client previously caught `PermissionError` as `OSError` and hid the distinction.
- Build `3880f2c5` sources confirm that first-time audit requests from a worker are denied while scheduling the permission prompt asynchronously. The public Script `execute` binding enters Orca's audited trampoline. Webview handlers and newly created Python workers do not themselves enter that scope.
- Sign-in and **Check connection** now perform an actual read-only auth-status GET through that audited binding on the UI thread, with a three-second socket timeout. This allows normal inline permission approval before background work. Configuration workers explicitly enter the audited binding as well.
- A native Send uses Orca's `queue_on_main` callback and the same audited binding to prepare network access and read one byte from the exact archive Orca supplied. Upload stays on the native worker. Cancelling or unloading while permission preparation is queued prevents later submission; a pending preparation has a finite wait. No permission store is edited, no synthetic audit event is generated, and no upload is retried automatically.
- Network and file permission denials now carry an explicit Orca permission message without credentials, local paths, or the underlying exception text. Connection cleanup and normal native error returns are preserved. Diagnostics include the current `job_active` state.
- Orca's native error view retains **Check the reason** and **Cancel** after a failed job. It is not evidence that the callback is still running; choose **Cancel** to leave it. The plugin does not report false success to dismiss that view.
- Automated validation: **69 tests passed**, including audited dispatch, UI permission preparation, denial propagation, cancellation of queued preparation, archive permission errors, and upload cleanup without retries. The suite includes tagged-wheel packaging and discovery coverage.
- Live validation of `0.1.3` is still required: install/restart, confirm the panel version, sign in, use **Check connection**, allow Orca's required network/archive permissions, and retry Send. No real Grove upload or queue submission was performed during this correction.

## Native temporary-archive naming correction: 0.1.4 (2026-10-17)

- The user reports that `0.1.3` now fails with “Orca did not provide a sliced .gcode.3mf archive.” This message comes from the upload filename check, before archive content validation or upload.
- Source inspection confirms that Orca's `Plater::send_gcode` replaces the temporary G-code extension with `.3mf`, then `PrintJob` passes that path as `params.filename`. The callback does not receive the manual-export filename. The `PrintJob` assignment was also checked against build `3880f2c5`.
- The actual temporary `.3mf` from the failed user attempt was read locally. It contains `Metadata/plate_1.gcode`, `.gcode.md5`, and `.json`; the existing content and CRC validation passes when supplied with a `.gcode.3mf` upload name. The original file was not changed or uploaded.
- The native adapter now keeps the source path separate from the upload name. It derives a safe `.gcode.3mf` name from Orca's project name, with a fallback for unnamed projects. Both native LAN callbacks use this path, and Grove's hosted form receives the same upload name and original 1-based plate selection.
- Content checks remain in place. A `.3mf` suffix alone does not admit model-only, corrupt, incomplete, or wrong-plate archives; native raw-G-code inputs are rejected with a preset-configuration message. No file picker, staging copy, source rename, or automatic upload retry was added.
- Automated validation: **76 tests passed**, including both native callback variants with a hidden `.3mf` source, project-name normalization, unchanged multipart archive bytes, upload/form name consistency, and invalid-archive rejection. Ruff lint and formatting passed.
- Successful live upload, hosted-form opening, and receipt completion still need the user's retry with `0.1.4`. No Grove upload or queue submission was made during this correction.

## Startup attachment investigation: 0.1.4 (2026-10-17)

- The next user attempt reports an empty callback list, zero connection/metadata calls, and `adapter_connected: false`. This is a failure before the upload filename path can run.
- The latest local startup log records `NetworkAgent::set_user_selected_machine: no printer agent`, followed by failed `get_version` and `pushall` sends. The saved selected preset is the Grove P1S clone, its stored agent remains `grove-control-queue`, and experimental printer-agent mode remains enabled. No preset files were edited during investigation.
- Source checked against build `3880f2c5` shows that plugin/capability load registers the agent and refreshes the dropdown without re-running live-agent selection. Its factory reuses the registered capability instance. This supports a startup ordering explanation rather than a new archive-validation failure, but the snapshot alone does not prove the exact order of every startup event.
- The documented recovery is to close Send, switch to the ordinary P1S preset, then back to the saved Grove preset after plugin load, and reselect the Grove LAN device if needed. This forces Orca to run its normal live-agent selection and attach its callbacks. Live confirmation is pending. No plugin runtime change or new wheel was made for this investigation.

## Modal Print window correction: 0.1.5 (2026-10-17)

- Following the startup recovery guidance, the user reports that sending reaches Grove's hosted Print form, but the window opens behind Orca's modal Send dialog and cannot be used. This establishes progress through upload and form opening; it does not confirm a queue submission or matching receipt.
- The bridge used `create_window` without a style, whose default is `WINDOW_MODELESS`. Build `3880f2c5` exposes public `WINDOW_MODAL` and `WINDOW_MODELESS` constants and a `style` parameter. For modal windows it defers `ShowModal()` to the UI loop and returns a pre-bound handle immediately, so the worker can continue processing messages and close the modal after a terminal result.
- Print now explicitly requests `WINDOW_MODAL`; Connect explicitly retains `WINDOW_MODELESS`. This is a host-owned window with the existing origin-checked iframe and receipt protocol. No native Send window is manipulated, and success still requires the matching Grove account/file receipt.
- Automated validation: **78 tests passed**, including modal style selection, pending send until a Grove terminal event, cancellation messaging, close-versus-cancel handling, handshake-timeout cleanup, and existing receipt/account checks. Ruff lint and formatting passed. Native modal stacking and input are outside these unit tests.
- Live verification remains necessary: install/restart, reselect the Grove preset after plugin load if callbacks are empty, open Print plate, and check that Grove's form appears above Send and accepts input. Exercise submit, cancel, and title-bar close. macOS and Windows window ordering are not proven by unit tests. No real Grove job was submitted during this correction.

## Startup sign-in and native Send completion: 0.1.6 (2026-10-17)

- The user reports that Grove accepts a queued job but Orca's **Send print job** dialog does not close, and that the plugin starts Orca signed out.
- With a saved Grove URL, `GroveControlScript.on_load()` now starts the existing hosted connect flow in a background worker. Grove can return a fresh token from an active browser session or present its normal sign-in page. The token remains in Runtime memory; only the server URL is saved. A missing URL does not launch a sign-in window.
- The LAN callback already returns `0` only after Runtime validates a receipt matching the submitted file and account. Orca source at `82aee1abdd5c475cff4e3adcb9d8d56078df2c10` posts its PrintJob completion event after a non-negative callback result; its Python plugin API has no method for a plugin to close that native dialog. This build keeps the safe receipt-gated return and does not force-dismiss the dialog or treat a partial/unknown result as success.
- To isolate the remaining close issue in a live run, check the Grove panel's Activity and Orca's application log. `print_job: send ok.` means Orca received callback success; `print_job: failed, result = ...` means it received a failure. The dialog/event path still needs target-build verification.
- Automated tests cover startup sign-in with a saved URL, no sign-in without a URL, and the existing native callback success return. Live startup reauthentication and native dialog closure remain unverified with `0.1.6`.

## Orca restricted `on_load` correction: 0.1.7 (2026-10-17)

- The user reports that Orca rejects startup with `PermissionError` for `_thread.start_new_thread`; the trace points to the plugin's worker start from `GroveControlScript.on_load()`.
- Orca runs plugin loading in a restricted worker context, so startup cannot start a Python thread or perform network permission setup there.
- Startup sign-in is now deferred until the live Grove LAN agent provides Orca's UI-thread dispatcher. The deferred action enters the public Script audit trampoline on the UI thread, performs bounded network permission preparation, then starts the existing sign-in worker. Opening the Grove Control panel is a UI-thread fallback if the LAN agent has not attached yet.
- If Orca denies thread creation, the exception is handled as a connection status message and the worker lock is released; plugin loading remains successful and the user can still sign in manually.
- Real startup loading, permission prompts, and automatic sign-in require verification in Orca. No Grove login or print submission was performed while preparing this correction.
