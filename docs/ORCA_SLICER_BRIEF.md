# OrcaSlicer integration boundary

## Current direction

The plugin uses Orca's documented `orca.printer_agent.PrinterAgentBase` capability as a LAN adapter named **Grove Queue**. It advertises one logical device and receives the finished sliced archive through `start_local_print()` or, on the applicable Orca path, `start_local_print_with_record()`. It forwards `params.filename` as the unchanged local source path and Orca's 1-based `params.plate_index` into the existing Grove submission service. The Grove upload/display name is derived separately from `params.project_name` and normalized to a safe `.gcode.3mf` filename, with a fallback name when the project name is absent.

After Orca loads the Script capability, a saved Grove URL triggers a background sign-in attempt through Grove's hosted Orca connect page. Grove's existing browser session can return a fresh access token; if that session is missing or expired, the hosted login page is shown. The token remains in Runtime memory and is never persisted in plugin configuration.

The native callback returns `0` only after a validated Grove queue receipt. Orca's PrintJob implementation posts its completion event after a non-negative callback result; the plugin has no public API to close Orca's native Send print job dialog itself. Live verification is still needed to confirm the dialog closes in the target build after a confirmed queue submission.

Orca's native Send archive is a hidden temporary file ending in `.3mf`; `.gcode.3mf` is the usual manual-export name, not a requirement on the source path. The plugin validates the archive's sliced G-code, metadata, selected plate, and ZIP integrity regardless of that naming difference. It neither renames the source nor changes its contents. Raw G-code, model-only 3MFs, and corrupt or incomplete archives remain unsupported.

The adapter is not a physical printer. The first prototype targets a cloned P1S preset, whose model ID is `C12`. Orca requires a LAN host identity, connection callback, current status, FDM mode, model, version, and storage-ready state before enabling its Send workflow. The plugin supplies a fixed loopback identity and minimum compatibility status through the documented callbacks. Storage and `support_send_to_sd` fields reflect Grove upload readiness; they do not claim to report physical hardware, firmware, or storage telemetry. Every physical machine command is rejected. The loopback address is not served by a local network daemon.

The agent advertises a stable discovered LAN identity:

| Identity field | Prototype value |
| --- | --- |
| Printer Agent | `Grove Queue` |
| Agent ID | `grove-control-queue` |
| Discovered device ID | `127.0.0.1:49381` |
| Advertised host | `127.0.0.1` |

The user selects/binds this discovered device in Orca. The cloned P1S preset selects Grove Queue as its agent and retains its slicing settings. Host/port fields are not exposed in that preset's Basic information page in the inspected nightly; they belong to Orca's separate physical-printer settings. No manual preset-file editing is required for the discovered-device route.

Orca may prompt for a LAN access code; any non-empty compatibility value such as `grove-queue` is sufficient. The plugin ignores it and never sends it to Grove. The Grove account session comes only from the configuration panel.

Orca's Send dialog requests `get_version` and `pushall` before it may connect the selected LAN device. It can skip the explicit connection when that device is already selected. Supported metadata requests for the exact adapter ID therefore establish its local session and immediately return version/full-status replies. Explicit connections also work; they validate the device ID and loopback host while tolerating an empty or preset-derived port. This adapter never opens a socket.

The configured Grove base URL, including any reverse-proxy path, remains a separate plugin setting. Grove's authenticated upload and embedded Print form continue using the existing API and bridge. The form owns the real printer/model, filament, queue, scheduling, and print-option choices. Orca's printer-only send options are not forwarded.

The Print form requests the public `orca.host.ui.create_window(style=orca.host.ui.WINDOW_MODAL)` presentation. Orca's native Send dialog is modal, so a default modeless sibling can be disabled behind it. The host defers window creation to the UI loop and returns a handle immediately even for `WINDOW_MODAL`, allowing the plugin worker to keep its existing receipt and cancellation exchange. The callback stays pending until Grove finishes; window creation or dismissal is not a success receipt. This API was checked in build `3880f2c5`'s [Host UI implementation and Python bindings](https://github.com/OrcaSlicer/OrcaSlicer/blob/3880f2c5/src/slic3r/plugin/host/PluginHostUi.cpp). Live window ordering on macOS and Windows still needs verification. No host source changes are involved.

## Readiness and limits

- The user enables **Preferences → Developer → Experimental Features → Use printer agents instead of print hosts**, clones a P1S profile, selects Grove Queue, enables **Use 3MF instead of G-code**, and selects/binds the discovered Grove Queue LAN device. The original preset remains available for direct printing.
- Orca's LAN connection represents the adapter's local protocol endpoint. Metadata is answered independently of an earlier connection callback or Grove authentication. Status and capability replies report Grove upload readiness; the `verify_job` preflight and native print callback independently refuse to upload until the Grove session is verified. Signing in or out refreshes those readiness replies.
- The `verify_job` call to `start_send_gcode_to_sdcard()` is readiness-only. It does not upload, open the form, or create a queue item. Printer-storage jobs and Orca's cloud callback are rejected.
- A real job callback is single-flight guarded. The Grove submission validates the `.gcode.3mf`, uploads it once, opens the hosted form inside Orca, and reports success only after a matching account/file receipt. Partial and uncertain outcomes remain failures from Orca's callback perspective and are never automatically retried.
- Orca's Python permissions are separate from Grove authentication. Sign-in and connection checks prepare access with a read-only API call on the UI thread, entering the public native Script `execute` binding so the normal audit scope and permission prompts apply. Native sends use the agent's `queue_on_main` callback to prepare the same connection and read access to the supplied archive before starting the upload. Background configuration work also enters the audited Script binding. Permission denials remain failures, with no automatic upload retry or permission-store edits.
- The status model and storage fields are compatibility placeholders isolated in `plugin.py`; they must not be expanded to imply physical telemetry. Multiple-filament mapping and nozzle/model compatibility still require the real-nightly proof described below.
- Issue [#16565](https://github.com/OrcaSlicer/OrcaSlicer/issues/16565) is a separate Orca selector-rendering report. A populated Grove entry must be explicitly selected. This adapter does not claim to fix a popup that remains obscured or unclickable.

## Feasibility evidence and validation status

Source inspection used `/Users/edward/Documents/Code/OrcaSlicer` at `82aee1abdd5c475cff4e3adcb9d8d56078df2c10`. It confirms that Orca's plugin surface exposes LAN discovery, connection/status callbacks, and `start_local_print()`. Orca consumes advertised discovery IDs and local-message status/version replies, then routes LAN plate jobs to `start_local_print()`. It also has an automatic selection path deriving an identity from preset `print_host` plus port; that path is optional for this discovered adapter. This supports an implementation without an Orca source change.

The connection signature and metadata-before-connection ordering were also checked against the application log's build `3880f2c5`, using its [PrinterAgent trampoline](https://github.com/OrcaSlicer/OrcaSlicer/blob/3880f2c5/src/slic3r/plugin/pluginTypes/printerAgent/PrinterAgentPluginCapabilityTrampoline.hpp) and [Send dialog](https://github.com/OrcaSlicer/OrcaSlicer/blob/3880f2c5/src/slic3r/GUI/SendToPrinter.cpp#L1271). The local log contains failed version/full-status requests. The plugin's previous offline gate could reject those requests before Orca opened a connection. The configuration panel now exposes bounded callback counters and exception classes so the installed build's behavior can be checked without recording credentials, raw payloads, or local archive paths.

Subsequent user testing of `0.1.2` confirms that metadata synchronization now completes and the native Send action invokes the plugin. The local Orca log records network audit denials followed immediately by `print_job: failed, result = -1`. The remaining apparent hang is Orca's error view, which retains **Check the reason** and **Cancel** after a failed job. Successful upload, hosted-form opening, and receipt completion still require live validation. See [LAN adapter validation](LAN_ADAPTER_VALIDATION.md) for the checks completed and outstanding.

Permission handling was checked against build `3880f2c5`: [Script binding](https://github.com/OrcaSlicer/OrcaSlicer/blob/3880f2c5/src/slic3r/plugin/pluginTypes/script/ScriptPluginCapability.cpp), [audited trampoline](https://github.com/OrcaSlicer/OrcaSlicer/blob/3880f2c5/src/slic3r/plugin/pluginTypes/script/ScriptPluginCapabilityTrampoline.hpp), and [audit manager](https://github.com/OrcaSlicer/OrcaSlicer/blob/3880f2c5/src/slic3r/plugin/PluginAuditManager.cpp). A first permission request on a worker is denied while its prompt is scheduled asynchronously; on the UI thread, it can be approved inline. No public network-permission declaration API is exposed. The plugin therefore uses actual read-only operations through the normal audited entry point.

## Required nightly proof

Before describing the plugin as usable, validate in an unmodified plugin-enabled Orca nightly, without submitting a real Grove queue job unless the user authorizes a test destination:

1. On a clean install, clone a P1S preset, select Grove Queue, and enable **Use 3MF instead of G-code**. Select/bind the discovered Grove Queue LAN device using a non-empty dummy compatibility access code if prompted.
2. Open the populated printer dropdown and explicitly select **Grove Queue**. Automatic preselection does not count. If the popup remains obscured or unselectable, record it as the external #16565 blocker.
3. Confirm metadata synchronization completes without a physical printer, including while signed out. Sign in and use **Check connection**, approving Orca's network permission prompts. Confirm a verified Grove session enables upload preflight, and sign-out, session expiry, server loss, and reconnect update upload readiness.
4. Slice a test plate and verify the `verify_job` preflight creates no upload, window, or queue request.
5. Confirm the native callback receives the exact archive path and 1-based selected plate, permits Orca's normal archive-read prompt if required, then displays Grove's Print form in an Orca-owned window. Use a non-production test destination only with explicit user authorization. Denying a permission must fail before upload; close Orca's resulting error view with **Cancel**.
6. Verify single and multi-filament profiles, cancellation, upload denial, partial/unknown receipts, duplicate/repeat calls, preset switch, plugin unload/reload, and Windows behavior.

Do not patch Orca, add a file picker, or fall back to the cloud callback if a supported host behavior blocks this path. Report the exact host boundary and keep #16565's presentation symptom separate from agent eligibility/readiness.
