"""Orca Script and PrinterAgent capabilities for Grove Control."""

from __future__ import annotations

import json
import threading
import time
from importlib.resources import files
from pathlib import Path

from ._version import __version__
from .models import (
    CancelCallback,
    Cancelled,
    GroveError,
    Progress,
    ProgressCallback,
    SlicedJob,
    SubmissionResult,
    SubmissionStatus,
)
from .runtime import Runtime

AGENT_ID = "grove-control-queue"
# Orca keys LAN devices by the edited printer preset's print_host and port. This
# adapter address is loopback-only and is intercepted by this agent; no listener
# or physical printer is involved. Keep these values aligned with README setup.
LAN_HOST = "127.0.0.1"
LAN_PORT = "49381"
DEVICE_ID = f"{LAN_HOST}:{LAN_PORT}"
PRINTER_MODEL_ID = "C12"  # Orca's P1S model id; first adapter prototype targets P1S presets.
CONNECT_OK = 0
CONNECT_FAILED = 1
CONNECT_LOST = 2
_runtime: Runtime | None = None
_active_agent = None
_active_script = None
_state_lock = threading.RLock()


def _current_runtime() -> Runtime | None:
    with _state_lock:
        return _runtime


def connection_state() -> dict:
    """Return public connection state without credentials or local paths."""
    runtime = _current_runtime()
    if runtime is None:
        raise GroveError("Open the Grove Control panel from Orca's Plugins window first.")
    return runtime.snapshot()


def submit_sliced_job(
    path: str,
    *,
    filename: str | None = None,
    plate_id: int | None = None,
    progress: ProgressCallback = lambda _: None,
    cancelled: CancelCallback = lambda: False,
) -> SubmissionResult:
    """Submit the archive passed to Orca's native PrinterAgent callback."""
    runtime = _current_runtime()
    if runtime is None:
        raise GroveError("Open the Grove Control panel and connect to Grove before printing.")
    return runtime.submit_job(SlicedJob.from_native(Path(path), filename, plate_id), progress, cancelled)


def register_plugin(orca):
    """Register Grove configuration UI and the native Grove Queue agent."""
    global _active_agent

    pa = orca.printer_agent

    class GroveQueueAgent(pa.PrinterAgentBase):
        """LAN compatibility adapter that hands Orca's sliced archive to Grove."""

        def __init__(self):
            global _active_agent
            super().__init__()
            self._callbacks = {}
            self._selected_machine = DEVICE_ID
            self._job_active = False
            self._online = False
            self._connection_requested = False
            self._guard = threading.RLock()
            self._connect_calls = 0
            self._metadata_before_connect = 0
            self._metadata_requests = {}
            self._callback_calls = {}
            self._callback_errors = {}
            with _state_lock:
                _active_agent = self

        def get_name(self):
            return "Grove Queue"

        def get_agent_info(self):
            return pa.AgentInfo(
                AGENT_ID,
                "Grove Queue",
                __version__,
                "Send the sliced plate to Grove's print queue through Orca's LAN workflow.",
            )

        def _store(self, name, callback):
            with self._guard:
                self._callbacks[name] = callback
            return 0

        def _emit(self, name, *args):
            with self._guard:
                callback = self._callbacks.get(name)
            if callback is not None:
                try:
                    callback(*args)
                except Exception as exc:
                    # Callbacks cross Orca's C++ boundary; never let an error
                    # escape into the host. Keep only the exception class:
                    # its message could contain credentials or local paths.
                    with self._guard:
                        self._callback_errors[name] = type(exc).__name__
                    return False
                with self._guard:
                    self._callback_calls[name] = self._callback_calls.get(name, 0) + 1
                return True
            return False

        def diagnostics(self):
            """Expose callback progress without payloads, credentials, or paths."""
            with self._guard:
                return {
                    "plugin_version": __version__,
                    "adapter_connected": self._online,
                    "job_active": self._job_active,
                    "connect_calls": self._connect_calls,
                    "metadata_started_connection": self._metadata_before_connect,
                    "metadata_requests": dict(self._metadata_requests),
                    "callbacks_registered": sorted(k for k, v in self._callbacks.items() if v is not None),
                    "callbacks_returned": dict(self._callback_calls),
                    "callback_errors": dict(self._callback_errors),
                }

        def _ready(self):
            with self._guard:
                connected = self._online and not self._job_active
            runtime = _current_runtime()
            return connected and runtime is not None and runtime.ready_for_print()

        def _grove_ready(self):
            runtime = _current_runtime()
            return runtime is not None and runtime.ready_for_print()

        def _publish_adapter_state(self):
            """Supply Orca's documented LAN status callbacks for this logical adapter.

            These are compatibility fields, not telemetry: C12 identifies the P1S
            slicing profile used by this first prototype, sdcard and
            support_send_to_sd describe whether the Grove upload route is currently
            ready, and ota.sw_ver identifies this plugin. No physical printer,
            firmware, or storage is queried or represented. Orca's metadata
            handshake remains available when Grove is signed out so auth failures
            do not look like a device-info timeout.
            """
            with self._guard:
                connected = self._online
            if not connected:
                return
            upload_ready = self._grove_ready()
            status = {
                "print": {
                    "command": "push_status",
                    "msg": 0,
                    "printer_type": PRINTER_MODEL_ID,
                    "gcode_state": "IDLE",
                    "sdcard": upload_ready,
                    "device": {"type": 1},  # Orca DEVICE_MODE_FDM compatibility value.
                }
            }
            version = {
                "info": {
                    "command": "get_version",
                    "result": "success",
                    "module": [
                        {
                            "name": "ota",
                            "product_name": "Grove Queue adapter",
                            "sw_ver": __version__,
                        }
                    ],
                }
            }
            capabilities = {
                "info": {
                    "command": "get_capabilities",
                    "capabilities": {"flags": {"support_send_to_sd": upload_ready}},
                }
            }
            self._emit("local_message", DEVICE_ID, json.dumps(status))
            self._emit("local_message", DEVICE_ID, json.dumps(version))
            self._emit("local_message", DEVICE_ID, json.dumps(capabilities))

        def _set_connection(self, connected, message=""):
            with self._guard:
                changed = self._online != connected
                self._online = connected
            if changed:
                self._emit(
                    "local_connect",
                    CONNECT_OK if connected else CONNECT_LOST,
                    DEVICE_ID,
                    message or ("0" if connected else "grove_unavailable"),
                )
            if connected:
                self._publish_adapter_state()

        def refresh_availability(self):
            """Refresh Grove readiness without breaking Orca's LAN info handshake."""
            with self._guard:
                if self._job_active or not self._connection_requested:
                    return
            self._set_connection(True)

        # Host callbacks. Orca supplies these once the agent becomes live.
        def set_server_callback(self, fn):
            return self._store("server_error", fn)

        def set_on_ssdp_msg_fn(self, fn):
            return self._store("ssdp", fn)

        def set_on_printer_connected_fn(self, fn):
            return self._store("printer_connected", fn)

        def set_on_subscribe_failure_fn(self, fn):
            return self._store("subscribe_failure", fn)

        def set_on_message_fn(self, fn):
            return self._store("message", fn)

        def set_on_user_message_fn(self, fn):
            return self._store("user_message", fn)

        def set_on_local_connect_fn(self, fn):
            result = self._store("local_connect", fn)
            self.refresh_availability()
            return result

        def set_on_local_message_fn(self, fn):
            result = self._store("local_message", fn)
            self._publish_adapter_state()
            return result

        def set_queue_on_main_fn(self, fn):
            result = self._store("queue_on_main", fn)
            with _state_lock:
                script = _active_script
            if script is not None:
                script._schedule_startup_sign_in(self)
            return result

        def queue_on_main(self, action):
            with self._guard:
                queue = self._callbacks.get("queue_on_main")
            if queue is None:
                raise GroveError("Orca's UI dispatcher is unavailable. Restart Orca before sending again.")
            queue(action)

        # Advertise one loopback LAN identity so Orca routes Print plate through
        # start_local_print. All commands remain inside this agent.
        def start_discovery(self, start, sending):
            if start:
                self._emit(
                    "ssdp",
                    json.dumps(
                        {
                            "dev_id": DEVICE_ID,
                            "dev_name": "Grove Queue",
                            "dev_ip": LAN_HOST,
                            "dev_type": PRINTER_MODEL_ID,
                            "dev_signal": "",
                            "connect_type": "lan",
                            "bind_state": "free",
                        }
                    ),
                )
                self.refresh_availability()
            return True

        def connect_printer(self, params):
            dev_id = getattr(params, "dev_id", "")
            host = getattr(params, "host", "")
            with self._guard:
                self._connect_calls += 1
                self._selected_machine = dev_id
                valid_device = dev_id == DEVICE_ID and host == LAN_HOST
                self._connection_requested = valid_device
            if not valid_device:
                self._set_connection(False, "invalid_adapter_address")
                self._emit("local_connect", CONNECT_FAILED, DEVICE_ID, "invalid_adapter_address")
                return -1
            # This callback establishes the plugin's local protocol endpoint, not
            # Grove authentication. Orca requests version and status data after
            # ConnectStatusOk; answer those requests even if the user still needs
            # to sign in. Print preflight and the actual callback remain gated.
            # The port is intentionally not validated: Orca may populate it from
            # the active preset (or leave it empty) even though this device was
            # discovered under the adapter's stable host:port identity. This is
            # a callback-only adapter; it never opens a socket on that port.
            self._set_connection(True)
            return 0

        def disconnect_printer(self):
            with self._guard:
                self._connection_requested = False
            self._set_connection(False, "disconnected")
            return 0

        def get_user_selected_machine(self):
            return self._selected_machine

        def set_user_selected_machine(self, dev_id):
            if dev_id not in ("", DEVICE_ID):
                return -1
            self._selected_machine = dev_id
            return 0

        def _handle_host_message(self, dev_id, json_str):
            if dev_id != DEVICE_ID:
                return -1
            try:
                message = json.loads(json_str)
            except (TypeError, ValueError):
                return -7010
            if not isinstance(message, dict):
                return -7010
            # Orca asks for read-only snapshots during LAN selection, including
            # before an explicit connection. Every motion, heater, calibration,
            # and printer-control command remains unsupported.
            info = message.get("info")
            pushing = message.get("pushing")
            command = None
            if isinstance(info, dict) and info.get("command") in ("get_version", "get_capabilities"):
                command = info["command"]
            elif isinstance(pushing, dict) and pushing.get("command") in ("pushall", "start"):
                command = pushing["command"]
            if command is None:
                return -7010
            with self._guard:
                self._metadata_requests[command] = self._metadata_requests.get(command, 0) + 1
                connected = self._online
                if not connected:
                    self._metadata_before_connect += 1
                    self._connection_requested = True
            if not connected:
                # Orca's Send dialog asks for metadata before selecting/connecting
                # a LAN machine. If that machine is already selected it can skip
                # connect_printer entirely. This adapter has no socket to open:
                # a supported metadata request for its exact identity is enough
                # to establish the local session and answer the handshake.
                # Grove authentication still gates preflight and job submission.
                self._set_connection(True)
                return 0
            if isinstance(info, dict) and info.get("command") == "get_version":
                self._emit(
                    "local_message",
                    DEVICE_ID,
                    json.dumps(
                        {
                            "info": {
                                "command": "get_version",
                                "result": "success",
                                "module": [
                                    {
                                        "name": "ota",
                                        "product_name": "Grove Queue adapter",
                                        "sw_ver": __version__,
                                    }
                                ],
                            }
                        }
                    ),
                )
                return 0
            if isinstance(info, dict) and info.get("command") == "get_capabilities":
                self._publish_adapter_state()
                return 0
            if isinstance(pushing, dict) and pushing.get("command") in ("pushall", "start"):
                self._publish_adapter_state()
                return 0
            return -7010

        def send_message(self, dev_id, json_str, qos, flag):
            return self._handle_host_message(dev_id, json_str)

        def send_message_to_printer(self, dev_id, json_str, qos, flag):
            return self._handle_host_message(dev_id, json_str)

        def bind_detect(self, dev_ip, sec_link, detect):
            if dev_ip not in (LAN_HOST, DEVICE_ID, f"http://{LAN_HOST}:{LAN_PORT}"):
                return -1
            detect.dev_id = DEVICE_ID
            detect.dev_name = "Grove Queue"
            detect.model_id = PRINTER_MODEL_ID
            detect.version = __version__
            detect.connect_type = "lan"
            detect.bind_state = "free"
            return 0

        def check_cert(self):
            return 0

        def ping_bind(self, ping_code):
            return 0

        def bind(self, dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn):
            return 0

        def unbind(self, dev_id):
            return 0

        def request_bind_ticket(self):
            return (0, "")

        def start_send_gcode_to_sdcard(self, params, update_fn, cancel_fn, wait_fn):
            # Orca's LAN Send preflight can call this sentinel before the real
            # job callback. It is readiness-only: never upload, open Grove, or
            # create a queue item here.
            if getattr(params, "project_name", "") == "verify_job":
                if self._ready():
                    return 0
                self._update(
                    update_fn,
                    7,
                    -1,
                    "Open the Grove Control panel, sign in, and verify the Grove connection before sending.",
                )
                return -1
            return -7010

        def start_local_print(self, params, update_fn, cancel_fn):
            return self._submit_native_job(params, update_fn, cancel_fn)

        def start_local_print_with_record(self, params, update_fn, cancel_fn, wait_fn):
            return self._submit_native_job(params, update_fn, cancel_fn)

        def start_sdcard_print(self, params, update_fn, cancel_fn):
            return -7010

        @staticmethod
        def _update(update_fn, stage, code, message):
            if update_fn is not None:
                try:
                    update_fn(stage, code, message)
                except Exception:
                    pass

        def start_print(self, params, update_fn, cancel_fn, wait_fn):
            """Reject Orca's cloud route; Grove Queue is intentionally LAN-only."""
            self._update(update_fn, 7, -7010, "Grove Queue must use its LAN printer-agent route.")
            return -7010

        def _submit_native_job(self, params, update_fn, cancel_fn):
            """Receive the finished archive from either supported LAN print callback."""
            if getattr(params, "project_name", "") == "verify_job":
                return 0 if self._ready() else -1
            if not self._ready():
                self._update(
                    update_fn,
                    7,
                    -1,
                    "Connect and sign in to Grove in the Grove Control panel before printing.",
                )
                return -1
            with self._guard:
                if self._job_active:
                    self._update(update_fn, 7, -1, "A Grove submission is already in progress.")
                    return -1
                self._job_active = True

            self._update(
                update_fn,
                0,
                0,
                "Preparing Grove submission. Orca's printer-only options are ignored; "
                "configure the job in Grove.",
            )

            def report(value: Progress):
                if value.phase == "uploading" and value.total_bytes > 0:
                    percent = min(100, value.completed_bytes * 100 // value.total_bytes)
                    self._update(update_fn, 1, percent, "Uploading sliced plate to Grove")
                elif value.phase == "configuring":
                    self._update(update_fn, 2, 0, "Choose printer and queue options in Grove")
                else:
                    self._update(update_fn, 0, 0, "Checking Grove connection")

            def cancelled():
                if cancel_fn is None:
                    return False
                try:
                    return bool(cancel_fn())
                except Exception:
                    return True

            try:
                filename = getattr(params, "filename", "")
                plate_index = getattr(params, "plate_index", None)
                if not isinstance(filename, str) or not filename:
                    self._update(update_fn, 7, -1, "Orca did not provide the sliced plate archive.")
                    return -1
                if type(plate_index) is not int or plate_index <= 0:
                    self._update(update_fn, 7, -1, "Orca provided an invalid plate index.")
                    return -1
                result = submit_sliced_job(
                    filename,
                    filename=getattr(params, "project_name", None),
                    plate_id=plate_index,
                    progress=report,
                    cancelled=cancelled,
                )
                if result.status is SubmissionStatus.SUBMITTED:
                    self._update(update_fn, 6, 100, result.message)
                    return 0
                if result.status is SubmissionStatus.CANCELLED:
                    self._update(update_fn, 7, -18, result.message)
                    return -18
                self._update(update_fn, 7, -1, result.message)
                return -1
            except GroveError as exc:
                self._update(update_fn, 7, -1, str(exc))
                return -1
            except Exception:
                self._update(
                    update_fn,
                    7,
                    -1,
                    "The Grove submission could not be confirmed. Check Grove before trying again.",
                )
                return -1
            finally:
                with self._guard:
                    self._job_active = False
                self.refresh_availability()

        def on_unload(self):
            global _active_agent
            with self._guard:
                self._connection_requested = False
            self._set_connection(False, "plugin_unloaded")
            with self._guard:
                self._callbacks.clear()
            with _state_lock:
                if _active_agent is self:
                    _active_agent = None

    class GroveControlScript(orca.script.ScriptPluginCapabilityBase):
        def __init__(self):
            super().__init__()
            self._runtime: Runtime | None = None
            self._panel = None
            self._worker_lock = threading.Lock()
            self._startup_lock = threading.Lock()
            self._startup_sign_in_pending = False
            self._startup_sign_in_scheduled = False
            self._audit_dispatch = threading.local()
            self._unloaded = False

        def get_name(self):
            return "Grove Control"

        def get_default_config(self):
            return {"grove_url": ""}

        def on_load(self):
            global _runtime, _active_script
            self._unloaded = False
            old_runtime = self._runtime
            if old_runtime is not None:
                old_runtime.close()
            try:
                config = json.loads(self.get_config())
            except (TypeError, ValueError):
                config = {}
            url = config.get("grove_url", "") if isinstance(config, dict) else ""
            if not isinstance(url, str):
                url = ""
            try:
                runtime = Runtime(
                    orca.host.ui,
                    url,
                    changed=self._state_changed,
                    prepare_job=self._prepare_native_job_access,
                )
            except GroveError:
                runtime = Runtime(
                    orca.host.ui, changed=self._state_changed, prepare_job=self._prepare_native_job_access
                )
            self._runtime = runtime
            with _state_lock:
                _runtime = runtime
                _active_script = self
            with self._startup_lock:
                self._startup_sign_in_pending = bool(url)
                self._startup_sign_in_scheduled = False
            # Plugin loading runs in Orca's restricted worker context. Defer
            # network access and thread creation until the live agent provides
            # its UI-thread dispatcher.
            self._schedule_startup_sign_in()

        def execute(self):
            """Open the configuration-only panel in Orca's host UI."""
            pending = getattr(self._audit_dispatch, "pending", None)
            if pending is not None:
                pending()
                return orca.ExecutionResult.success("Grove action completed.")
            if self._unloaded:
                return orca.ExecutionResult.failure(
                    orca.PluginResult.RecoverableError, "Reload the Grove Control plugin and try again."
                )
            if self._runtime is None:
                self.on_load()
            if self._startup_sign_in_pending:
                # This is also a safe fallback if the Grove agent has not
                # attached yet: execute() is invoked from Orca's UI entry point.
                self._start_startup_sign_in()
            if self._panel is not None and self._panel.is_open():
                self._panel.show()
                return orca.ExecutionResult.success("Grove Control panel is open.")
            self._panel = orca.host.ui.create_dock_panel(
                html=files("grove_control_orca_plugin")
                .joinpath("resources/connection.html")
                .read_text("utf-8"),
                title="Grove Control",
                width=360,
                height=440,
                dock="right",
                on_message=self.on_message,
                on_close=self._panel_closed,
            )
            return orca.ExecutionResult.success("Grove Control panel opened.")

        def _schedule_startup_sign_in(self, agent=None):
            """Queue startup authentication after Orca leaves its on_load audit scope."""
            if self._unloaded:
                return
            with _state_lock:
                active_agent = _active_agent
            if agent is None:
                agent = active_agent
            if agent is None or agent is not active_agent:
                return
            with self._startup_lock:
                if not self._startup_sign_in_pending or self._startup_sign_in_scheduled:
                    return
                with agent._guard:
                    if agent._callbacks.get("queue_on_main") is None:
                        return
                self._startup_sign_in_scheduled = True
            try:
                agent.queue_on_main(self._start_startup_sign_in)
            except Exception:
                with self._startup_lock:
                    self._startup_sign_in_scheduled = False

        def _start_startup_sign_in(self):
            """Prepare permissions and start Grove sign-in from Orca's UI thread."""
            with self._startup_lock:
                if not self._startup_sign_in_pending or self._unloaded:
                    self._startup_sign_in_scheduled = False
                    return
                self._startup_sign_in_pending = False
                self._startup_sign_in_scheduled = False
            runtime = self._runtime
            if runtime is None:
                return

            def start():
                runtime.prepare_access()
                self._work(runtime.sign_in)

            try:
                # Orca's native Script trampoline establishes its audit scope.
                # Running this through it on the UI thread lets Orca show its
                # normal network/thread permission prompts after plugin load.
                self._invoke_audited(start)
            except PermissionError:
                runtime._publish("Orca denied a permission. Open Grove Control and sign in manually.")
            except GroveError as exc:
                runtime._publish(str(exc))
            except Exception:
                runtime._publish("Auto sign-in could not start. Open Grove Control and sign in manually.")

        def _invoke_audited(self, action):
            """Enter the public C++ Script binding so host callbacks keep Orca's audit scope.

            Webview handlers and Python-created threads do not acquire that scope
            themselves. Calling the base binding dispatches execute() through
            Orca's normal audited trampoline. No permission state is edited here.
            """
            if self._unloaded:
                raise GroveError("Reload the Grove Control plugin before continuing.")
            if getattr(self._audit_dispatch, "pending", None) is not None:
                raise GroveError("A Grove action is already being dispatched on this thread.")
            outcome = {}

            def run():
                try:
                    outcome["value"] = action()
                except Exception as exc:
                    outcome["error"] = exc
                outcome["done"] = True

            self._audit_dispatch.pending = run
            try:
                orca.script.ScriptPluginCapabilityBase.execute(self)
            except Exception as exc:
                raise GroveError(
                    "Orca could not dispatch the Grove action. Restart Orca and try again."
                ) from exc
            finally:
                del self._audit_dispatch.pending
            if not outcome.get("done"):
                raise GroveError("Orca did not run the Grove action. Restart Orca and try again.")
            if "error" in outcome:
                raise outcome["error"]
            return outcome.get("value")

        def _prepare_native_job_access(self, job, cancelled):
            """Resolve normal host permissions on the UI thread before uploading."""
            with _state_lock:
                agent = _active_agent
            runtime = self._runtime
            if agent is None or runtime is None:
                raise GroveError("Open Grove Control and select the Grove Queue agent before sending.")
            done, abandoned = threading.Event(), threading.Event()
            outcome = {}

            def prepare():
                try:
                    if abandoned.is_set() or cancelled() or self._unloaded:
                        raise Cancelled("Submission cancelled before upload.")
                    self._invoke_audited(lambda: runtime.prepare_access(job))
                except Exception as exc:
                    outcome["error"] = exc
                finally:
                    done.set()

            agent.queue_on_main(prepare)
            deadline = time.monotonic() + 120
            while not done.wait(0.1):
                if cancelled() or self._unloaded:
                    abandoned.set()
                    raise Cancelled("Submission cancelled before upload.")
                if time.monotonic() >= deadline:
                    abandoned.set()
                    raise GroveError("Orca permission setup timed out. Cancel Send and use Check connection.")
            if "error" in outcome:
                raise outcome["error"]

        def _panel_closed(self):
            self._panel = None

        def _state_changed(self, state):
            with _state_lock:
                agent = _active_agent
            if agent is not None:
                agent.refresh_availability()
            panel = self._panel
            if not self._unloaded and panel is not None and panel.is_open():
                panel.post(
                    {
                        "type": "state",
                        **state,
                        "plugin_version": __version__,
                        "agent_diagnostics": agent.diagnostics() if agent is not None else None,
                    }
                )

        def _notice(self, message):
            panel = self._panel
            if not self._unloaded and panel is not None and panel.is_open():
                panel.post({"type": "notice", "message": message})

        def _work(self, action):
            if not self._worker_lock.acquire(blocking=False):
                self._notice("A Grove action is already in progress.")
                return

            def run():
                try:
                    self._invoke_audited(action)
                except GroveError as exc:
                    self._notice(str(exc))
                except Exception:
                    self._notice("The Grove action could not complete. Reopen the panel and try again.")
                finally:
                    self._worker_lock.release()

            worker = threading.Thread(target=run, name="grove-control", daemon=True)
            try:
                worker.start()
            except Exception:
                self._worker_lock.release()
                raise

        def on_message(self, message):
            if self._unloaded:
                return
            try:
                data = json.loads(message) if isinstance(message, str) else message
                if not isinstance(data, dict):
                    return
                runtime = self._runtime
                if runtime is None:
                    self.on_load()
                    runtime = self._runtime
                if data.get("type") in ("ready", "refresh"):
                    state = runtime.snapshot()
                    self._state_changed(state)
                    if runtime.connection_check_due():
                        self._work(runtime.check_connection)
                elif data.get("type") == "configure":
                    url = data.get("grove_url")
                    if not isinstance(url, str):
                        raise GroveError("Enter a Grove URL.")
                    runtime.configure(
                        url,
                        lambda value: self.save_config(json.dumps({"grove_url": value})),
                    )
                elif data.get("type") == "sign_in":
                    self._invoke_audited(runtime.prepare_access)
                    self._work(runtime.sign_in)
                elif data.get("type") == "sign_out":
                    self._work(runtime.sign_out)
                elif data.get("type") == "check_connection":
                    self._invoke_audited(runtime.prepare_access)
                    self._work(runtime.check_connection)
            except PermissionError:
                self._notice("Orca denied a permission. Allow the plugin permission and try again.")
            except (ValueError, GroveError) as exc:
                notice = str(exc) if isinstance(exc, GroveError) else "Orca sent an invalid panel message."
                self._notice(notice)

        def on_unload(self):
            global _runtime, _active_script
            self._unloaded = True
            self._panel = None
            runtime, self._runtime = self._runtime, None
            if runtime is not None:
                runtime.close()
            with _state_lock:
                if _runtime is runtime:
                    _runtime = None
                if _active_script is self:
                    _active_script = None

    @orca.plugin
    class GroveControlPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(GroveControlScript)
            orca.register_capability(GroveQueueAgent)

    return GroveControlPlugin
