# Grove Control OrcaSlicer Plugin

A Python plugin that routes OrcaSlicer's native **Print plate** action to Grove Control. Orca passes its completed sliced 3MF archive directly to the plugin, which uploads it with a `.gcode.3mf` name. The user does not export, locate, select, or re-import a file.

## How it works

- **Grove** is a main-window tab in Orca. It provides server setup, sign-in, connection status, and submission activity.
- **Grove Queue** is a LAN `PrinterAgent` adapter. The printer preset selects this agent; its discovered Grove Queue device supplies a stable loopback identity. The plugin answers connection and status callbacks and receives the archive through `start_local_print`.
- Orca needs a small compatibility status to enable its LAN Send workflow. The first prototype is limited to the P1S model (`C12`): its model, FDM, adapter-version, and upload-route status fields are synthetic Orca compatibility values. They do not describe a physical printer or storage device. The loopback address has no listening service and the plugin sends no movement, heater, calibration, or other machine-control commands.
- The plugin validates the archive, checks the Grove user session, uploads once, and opens Grove's hosted full Print form inside an Orca-owned modal window. Grove owns printer/model selection, readiness, materials, copies, scheduling, print options, and queue creation.
- Native Send supplies a temporary `.3mf` path. Its upload name comes from Orca's project name, with the `.gcode.3mf` suffix Grove expects. The source file stays in place and its contents are unchanged; the plugin still checks for sliced G-code, required metadata, the selected plate, and ZIP integrity before uploading.
- Success requires a receipt matching the uploaded file and signed-in account. Partial, cancelled, and uncertain outcomes are not reported as success and are never automatically retried.

The PrinterAgent workflow is experimental and off by default in Orca. This LAN adapter is a prototype and has not passed the real-nightly workflow check yet. See [the integration brief](docs/ORCA_SLICER_BRIEF.md) and [validation report](docs/LAN_ADAPTER_VALIDATION.md).

## Setup and use

1. Install and activate the plugin from Orca's **Plugins → Install local plugin** flow.
2. Open the **Grove** tab in Orca's main window. If other plugins fill the tab bar, choose **Grove** from the tab overflow menu. On builds without plugin tabs, open **Plugins** and run the **Grove Control** script to open the same controls in a dock panel.
3. Save your Grove server URL and sign in once. Allow Orca's plugin permission prompts for connecting to that server. These are separate from signing in to Grove. After the URL is saved, the plugin opens Grove's hosted connect flow each time Orca loads it. If Grove recognizes an active browser session, it returns a fresh token automatically; otherwise, its login page appears. The access token stays in memory and is not saved in Orca configuration or window URLs.
4. Enable **Preferences → Developer → Experimental Features → Use printer agents instead of print hosts**, then restart Orca if it asks.
5. Clone the **Bambu Lab P1S** printer preset you want to use. Keep the original preset assigned to its normal agent for direct printing.
6. In the cloned preset's **Basic information → Advanced**, select **Printer Agent: Grove Queue**, enable **Use 3MF instead of G-code**, and leave **Use 3rd-party print host** disabled. Save the preset.
7. Select the discovered **Grove Queue (LAN)** device through Orca's **Device** tab. If Orca prompts for a LAN access code, enter any non-empty compatibility value such as `grove-queue`. The plugin ignores this value. Use the **Grove** tab to sign in to Grove. An entry already available in the Print plate printer selector has already passed this binding step.
8. Select the cloned preset, slice, and use **Print plate**. Select **Grove Queue** in Orca's printer selector if it is not already selected. The LAN preflight is readiness-only. When you send, allow Orca's read-permission prompt for its temporary sliced archive if shown. The completed archive then uploads to Grove and Grove opens its full job form inside Orca.

The adapter advertises `127.0.0.1:49381` itself. No host or port field needs to be entered in **Basic information**; the earlier setup instructions incorrectly placed those fields there. Orca may ask for metadata before calling the connection callback, so the adapter answers those read-only requests immediately while retaining Grove authentication checks for uploads.

To replace an earlier local build, install the new `.whl` from **Plugins → Install local plugin**, confirm **Overwrite**, then restart Orca so it reloads the package. The plugin version may stay the same for local builds from a checkout without Git tags.

After installing a replacement, restart Orca and check the loaded version in the **Grove** tab. When a server URL is saved, startup sign-in is queued through Orca's UI dispatcher once the Grove LAN agent attaches. Opening the **Grove** tab also starts the attempt if it is still pending. If Grove's browser session has expired, sign in through the Grove window. Orca may ask for network and background-thread permissions after replacement.

The first implementation targets the P1S slicing profile to prove the route. Other printer models and filament combinations still need compatibility checks before they are supported.

If **Print plate** times out, close the Send dialog and expand **Orca connection diagnostics** in the **Grove** tab. Copy the displayed text. It includes the loaded version, metadata request counts, connection callback count, and callback errors; it excludes credentials, filenames, and request payloads. A callback returning successfully confirms it was invoked, while Orca's Send dialog establishes whether it accepted the reply.

### Empty callbacks after restarting Orca

If diagnostics show `callbacks_registered: []` and no metadata requests, the displayed agent instance has not been attached to Orca's live printer connection. This happens before the Grove network or archive path. In the inspected nightly, loading a plugin refreshes the agent dropdown without re-running live-agent selection; a saved Grove preset can therefore be restored before its agent is available.

Close the Send dialog, switch the printer preset to the ordinary P1S preset, then select the saved Grove P1S preset again after the plugin has loaded. Confirm **Printer Agent: Grove Queue**, select **Grove Queue (LAN)** again if needed, and reopen Print plate. The callback list should now be populated and metadata requests should appear when Orca requests synchronization. User testing after these instructions reached Grove's Print form. Replacing the wheel or changing the Grove URL does not address an unattached agent.

### Grove Print form behind the Send dialog

Builds through `0.1.4` opened a modeless Print window, which Orca's modal Send dialog could leave behind and disable. Build `0.1.5` uses the host's public `create_window(style=WINDOW_MODAL)` API for Print. It is intended to present Grove as the active modal dialog while the native send waits for the matching queue receipt. Finish or cancel in Grove's form; closing it without a receipt remains an uncertain outcome. The login window keeps its existing modeless presentation. The window-ordering correction needs a live retry on the target Orca build.

### Orca Send print job dialog after queue confirmation

The native callback returns success (`0`) only after the plugin validates Grove's receipt for the submitted file and account. Orca's Print plate job handler uses that return value to post its completion event and close the Send print job dialog. The plugin has no public API to dismiss that native dialog directly. If Grove confirms the queue entry but Orca keeps the dialog open, check **Activity** in the **Grove** tab for a submitted result, then inspect Orca's log: `print_job: send ok.` means the callback completed successfully and points to an Orca dialog/event issue; `print_job: failed, result = ...` means Orca received a failure instead. The native dialog close behavior still needs live verification with the installed build.

### Orca permission errors when sending

Version `0.1.2` could show “Could not reach Grove” when Orca had denied a Python network permission. In `0.1.3`, **Sign in** and **Check connection** first perform a read-only API request through Orca's audited Script entry on its UI thread, using a short socket timeout. Orca can then complete its normal permission prompts before the background action starts. Sending also prepares read access to the exact archive supplied by Orca. Network operations stay in Orca's audit scope; the plugin does not alter grants or retry uploads.

If a permission is denied, the error explicitly identifies Orca's permission block. A failed send remains in Orca's error view with **Check the reason** and **Cancel**; choose **Cancel** to close that attempt, approve the required permissions, and use **Check connection** before sending again. The error view remaining open does not mean the plugin's send callback is still running.

## Important outcomes and limits

- Orca's printer-only options are not forwarded as physical-printer commands. Configure the actual destination and job options in Grove's form.
- Grove does not currently expose a separate upload-permission preflight; the upload response is authoritative.
- A partial receipt includes the queue IDs Grove confirmed. Check Grove before taking further action.
- On Grove servers with Temporary Files, Orca uploads go into that folder. Grove removes them after dispatch when no queued job still needs them. Cancelling leaves the file available there until Grove's configured retention period expires. Move it to another folder to keep it.
- Closing the Grove form without a receipt, invalid receipts, and interrupted submissions are uncertain. Check Grove's queue before manually trying again.
- Orca issue [#16565](https://github.com/OrcaSlicer/OrcaSlicer/issues/16565) reports the printer selector popup obscured or clipped on macOS and Windows. LAN eligibility is separate from that rendering symptom; the populated selector still needs to be explicitly opened and tested.
- Automated validation does not submit jobs. Use an authorized test destination for live queue tests.

The Grove-side hosted login and Print form bridge, along with receipt changes, are described in [the Grove Control brief](docs/GROVE_CONTROL_BRIEF.md) and [bridge protocol](docs/BRIDGE_PROTOCOL.md).

## Development

Install Node.js to run the JavaScript bridge test. Pull requests to `main` require the **Required CI** check, which runs the tests on Linux, macOS, and Windows and checks lint, formatting, and packaging on Linux.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m build --no-isolation
```

The wheel is pure Python, includes the Grove tab, and has no runtime pip dependencies. Install `dist/*.whl` through Orca's local plugin installer.

## Versioning

The package version is derived from Git tags by `hatch-vcs`. Use tags in `vMAJOR.MINOR.PATCH` form, such as `v0.1.0`. Before a tag is available, builds use the `0.1.7` fallback configured in `pyproject.toml`. The **Grove** tab displays the loaded version. Builds generate `src/grove_control_orca_plugin/_version.py`; do not edit that generated file.

## Publishing a release

Push a version tag such as `v0.1.8` to run the **Publish plugin** workflow. After CI passes, the workflow builds the wheel and source distribution, confirms their versions match the tag, and attaches both files to a GitHub prerelease. If the release already exists, rerunning the workflow marks it as a prerelease and replaces both assets.

```sh
git tag v0.1.8
git push origin v0.1.8
```
