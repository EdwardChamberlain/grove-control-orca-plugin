import json
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from grove_control_orca_plugin import plugin
from grove_control_orca_plugin.models import (
    Cancelled,
    GroveError,
    HostPermissionRequired,
    Progress,
    SubmissionResult,
    SubmissionStatus,
)


class ScriptCapability:
    audit_scope = threading.local()

    def __init__(self):
        self.saved = {}

    def get_config(self):
        return json.dumps(self.saved)

    def save_config(self, config):
        self.saved = json.loads(config)
        return True

    def execute(self):
        # Stand in for Orca's public native binding: virtual dispatch enters the
        # trampoline's audit scope before it invokes the Python override.
        previous = getattr(self.audit_scope, "active", False)
        self.audit_scope.active = True
        try:
            return type(self).execute(self)
        finally:
            self.audit_scope.active = previous


class PrinterAgentBase:
    def __init__(self):
        pass


class PagesCapability:
    def __init__(self):
        self.messages = []

    def post_message(self, data):
        self.messages.append(json.loads(json.dumps(data)))


class FakePanel:
    def __init__(self):
        self.messages = []
        self.open = True
        self.acknowledged = threading.Condition()

    def is_open(self):
        return self.open

    def post(self, message):
        with self.acknowledged:
            self.messages.append(message)
            self.acknowledged.notify_all()

    def show(self):
        self.open = True


def make_export(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for plate_id in (1, 3):
            prefix = f"Metadata/plate_{plate_id}"
            archive.writestr(f"{prefix}.gcode", b"G28\n")
            archive.writestr(f"{prefix}.gcode.md5", b"hash")
            archive.writestr(f"{prefix}.json", b"{}")


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.capabilities = []
        self.ui = object()
        orca = types.SimpleNamespace(
            base=object,
            pages=types.SimpleNamespace(PagesPluginCapabilityBase=PagesCapability),
            script=types.SimpleNamespace(ScriptPluginCapabilityBase=ScriptCapability),
            printer_agent=types.SimpleNamespace(
                PrinterAgentBase=PrinterAgentBase,
                AgentInfo=lambda *values: types.SimpleNamespace(
                    id=values[0], name=values[1], version=values[2], description=values[3]
                ),
            ),
            host=types.SimpleNamespace(ui=self.ui),
            plugin=lambda cls: cls,
            register_capability=self.capabilities.append,
            ExecutionResult=types.SimpleNamespace(success=lambda message: message),
        )
        package = plugin.register_plugin(orca)
        package().register_capabilities()
        self.script = self.capabilities[0]()
        self.agent = self.capabilities[1]()
        self.page = self.capabilities[2]()
        self.script.on_load()
        self.script._panel = FakePanel()
        self.page.on_load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "plate.gcode.3mf"
        make_export(self.path)
        self.addCleanup(self.agent.on_unload)
        self.addCleanup(self.script.on_unload)
        self.addCleanup(self.page.on_unload)

    def test_registers_grove_tab_script_and_stable_native_queue_agent(self):
        self.assertEqual(len(self.capabilities), 3)
        self.assertEqual(self.page.get_name(), "Grove")
        self.assertEqual(self.script.get_name(), "Grove Control")
        self.assertEqual(self.agent.get_name(), "Grove Queue")
        info = self.agent.get_agent_info()
        self.assertEqual(info.id, "grove-control-queue")
        self.assertFalse(hasattr(self.agent, "execute_slicing_pipeline"))
        self.assertIs(plugin._active_agent, self.agent)

    def test_startup_prepares_access_and_dispatches_sign_in_from_audited_ui_callback(self):
        script = self.capabilities[0]()
        script.saved = {"grove_url": "https://grove.example"}
        self.addCleanup(script.on_unload)
        queue, steps = [], []
        ui_thread = threading.get_ident()

        def prepare_access(_runtime):
            steps.append(("prepare", ScriptCapability.audit_scope.active, threading.get_ident()))

        def start_work(_action):
            steps.append(("start", ScriptCapability.audit_scope.active, threading.get_ident()))

        with (
            patch.object(plugin.Runtime, "prepare_access", prepare_access),
            patch.object(script, "_work", side_effect=start_work) as work,
        ):
            script.on_load()
            self.agent.set_queue_on_main_fn(queue.append)

            self.assertEqual(steps, [])
            self.assertEqual(len(queue), 1)
            queue[0]()
            work.assert_called_once_with(script._runtime.sign_in)

        self.assertEqual(steps, [("prepare", True, ui_thread), ("start", True, ui_thread)])

    def test_startup_thread_permission_denial_leaves_manual_sign_in_available(self):
        script = self.capabilities[0]()
        script.saved = {"grove_url": "https://grove.example"}
        self.addCleanup(script.on_unload)
        queue = []

        with (
            patch.object(plugin.Runtime, "prepare_access") as prepare,
            patch.object(threading.Thread, "start", side_effect=PermissionError) as start,
        ):
            script.on_load()
            self.agent.set_queue_on_main_fn(queue.append)
            self.assertEqual(len(queue), 1)
            queue[0]()
            prepare.assert_called_once_with()
            start.assert_called_once_with()

        self.assertFalse(script._worker_lock.locked())
        self.assertIn("sign in manually", script._runtime.snapshot()["message"])

    def test_startup_does_not_open_grove_sign_in_without_saved_server_url(self):
        script = self.capabilities[0]()
        queue = []
        self.addCleanup(script.on_unload)

        script.on_load()
        self.agent.set_queue_on_main_fn(queue.append)

        self.assertEqual(queue, [])

    def test_tab_is_connection_configuration_only(self):
        html = self.page.get_ui()
        self.assertIn("Print plate", html)
        self.assertIn("Sign in", html)
        self.assertNotIn('type="file"', html)
        self.assertNotIn("Send to Grove</button>", html)
        self.assertNotIn("import_begin", html)

    def test_configuration_persists_only_the_server_url(self):
        self.page.on_message({"type": "configure", "grove_url": "https://grove.example/"})
        self.assertEqual(self.script.saved, {"grove_url": "https://grove.example"})
        self.assertEqual(plugin.connection_state()["grove_url"], "https://grove.example")
        self.assertFalse(plugin.connection_state()["ready_for_print"])
        self.assertEqual(self.page.messages[-1], self.script._panel.messages[-1])

    def test_opening_the_tab_restores_the_saved_connection_without_a_live_agent(self):
        self.script.saved = {"grove_url": "https://grove.example"}
        self.script.on_load()
        with (
            patch.object(plugin.Runtime, "prepare_access") as prepare,
            patch.object(self.script, "_work") as work,
        ):
            self.page.on_message({"type": "ready"})
            self.page.on_message({"type": "ready"})

        prepare.assert_called_once_with()
        work.assert_called_once_with(self.script._runtime.sign_in)
        self.assertEqual(self.page.messages[-1]["grove_url"], "https://grove.example")
        self.assertFalse(self.page.messages[-1]["orca_connected"])

    def test_tab_refresh_reports_orca_connection_without_publishing_agent_metadata(self):
        self.agent.set_queue_on_main_fn(lambda _: None)
        self.agent.connect_printer(types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST))
        with patch.object(self.agent, "refresh_availability") as refresh:
            self.page.on_message({"type": "refresh"})

        refresh.assert_not_called()
        self.assertTrue(self.page.messages[-1]["orca_connected"])
        self.agent.disconnect_printer()
        self.page.on_message({"type": "refresh"})
        self.assertFalse(self.page.messages[-1]["orca_connected"])

    def test_tab_recovers_when_the_script_capability_is_enabled_again(self):
        self.script.on_unload()
        self.page.on_message({"type": "ready"})
        self.assertTrue(self.page.messages[-1]["unavailable"])
        self.script.on_load()
        self.page.on_message({"type": "refresh"})
        self.assertNotIn("unavailable", self.page.messages[-1])

        message_count = len(self.page.messages)
        self.page.on_unload()
        self.script._runtime.configure("https://grove.example")
        self.assertEqual(len(self.page.messages), message_count)

    def test_configuration_work_crosses_the_native_audited_entry(self):
        def action():
            self.assertTrue(ScriptCapability.audit_scope.active)
            return "checked"

        self.assertEqual(self.script._invoke_audited(action), "checked")
        self.assertFalse(ScriptCapability.audit_scope.active)

    def test_native_access_preparation_runs_audited_on_ui_and_propagates_denial(self):
        for denied in (False, True):
            with self.subTest(denied=denied):
                queue, queued, failures = [], threading.Event(), []
                cancelled = threading.Event()
                ui_thread = threading.get_ident()

                def enqueue(action):
                    queue.append(action)
                    queued.set()

                def prepare(job):
                    self.assertEqual(threading.get_ident(), ui_thread)
                    self.assertTrue(ScriptCapability.audit_scope.active)
                    self.assertEqual(job.path, self.path)
                    if denied:
                        raise HostPermissionRequired()

                self.agent.set_queue_on_main_fn(enqueue)
                runtime, self.script._runtime = (
                    self.script._runtime,
                    types.SimpleNamespace(prepare_access=prepare),
                )

                def worker():
                    try:
                        self.script._prepare_native_job_access(
                            types.SimpleNamespace(path=self.path), cancelled.is_set
                        )
                    except Exception as exc:
                        failures.append(exc)

                thread = threading.Thread(target=worker)
                thread.start()
                try:
                    self.assertTrue(queued.wait(2))
                    queue[0]()
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(failures), int(denied))
                    if denied:
                        self.assertIsInstance(failures[0], HostPermissionRequired)
                finally:
                    cancelled.set()
                    thread.join(2)
                    self.script._runtime = runtime

    def test_cancelled_permission_preparation_cannot_resume_when_ui_queue_runs_later(self):
        queue, queued, cancelled = [], threading.Event(), threading.Event()
        failures, prepared = [], []

        def enqueue(action):
            queue.append(action)
            queued.set()

        self.agent.set_queue_on_main_fn(enqueue)
        runtime, self.script._runtime = (
            self.script._runtime,
            types.SimpleNamespace(prepare_access=prepared.append),
        )

        def worker():
            try:
                self.script._prepare_native_job_access(
                    types.SimpleNamespace(path=self.path), cancelled.is_set
                )
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(queued.wait(2))
            cancelled.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            queue[0]()
            self.assertEqual(prepared, [])
            self.assertIsInstance(failures[0], Cancelled)
        finally:
            cancelled.set()
            thread.join(2)
            self.script._runtime = runtime

    def test_agent_discovers_one_lan_queue_destination_with_host_derived_identity(self):
        discovered = []
        self.agent.set_on_ssdp_msg_fn(discovered.append)
        self.assertTrue(self.agent.start_discovery(True, False))
        item = json.loads(discovered[0])
        self.assertEqual(item["dev_id"], plugin.DEVICE_ID)
        self.assertEqual(item["dev_name"], "Grove Queue")
        self.assertEqual(item["connect_type"], "lan")
        self.assertEqual(item["dev_ip"], plugin.LAN_HOST)
        self.assertEqual(item["dev_type"], plugin.PRINTER_MODEL_ID)
        detect = types.SimpleNamespace()
        self.assertEqual(self.agent.bind_detect(plugin.LAN_HOST, "", detect), 0)
        self.assertEqual(detect.dev_id, item["dev_id"])
        self.assertEqual(detect.connect_type, "lan")
        self.assertEqual(self.agent.bind_detect("192.0.2.20", "", types.SimpleNamespace()), -1)

    def test_lan_handshake_stays_available_while_grove_readiness_changes(self):
        class RuntimeStub:
            ready = False

            def ready_for_print(self):
                return self.ready

        runtime = RuntimeStub()
        old_runtime = plugin._runtime
        plugin._runtime = runtime
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        connections, messages = [], []
        self.agent.set_on_local_connect_fn(lambda *args: connections.append(args))
        self.agent.set_on_local_message_fn(lambda *args: messages.append(args))
        params = types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST, port=plugin.LAN_PORT)

        # Orca must be able to complete its local device-info handshake before
        # Grove auth is considered. Readiness still gates print callbacks.
        self.assertEqual(self.agent.connect_printer(params), 0)
        self.assertEqual(connections[-1][0], plugin.CONNECT_OK)
        self.assertFalse(self.agent._ready())
        self.assertEqual(len(messages), 3)
        unavailable = json.loads(messages[0][1])["print"]
        self.assertFalse(unavailable["sdcard"])
        unavailable_capabilities = json.loads(messages[2][1])["info"]["capabilities"]["flags"]
        self.assertFalse(unavailable_capabilities["support_send_to_sd"])

        runtime.ready = True
        self.agent.refresh_availability()
        self.assertEqual(connections[-1][0], plugin.CONNECT_OK)
        self.assertTrue(self.agent._ready())
        self.assertGreaterEqual(len(messages), 6)
        status = json.loads(messages[-3][1])["print"]
        self.assertEqual(status["printer_type"], "C12")
        self.assertTrue(status["sdcard"])
        self.assertEqual(status["device"]["type"], 1)

        runtime.ready = False
        self.agent.refresh_availability()
        self.assertEqual(connections[-1][0], plugin.CONNECT_OK)
        self.assertFalse(self.agent._ready())
        runtime.ready = True
        self.agent.refresh_availability()
        self.assertEqual(connections[-1][0], plugin.CONNECT_OK)
        self.agent.disconnect_printer()
        self.assertEqual(connections[-1][0], plugin.CONNECT_LOST)
        self.assertFalse(self.agent._ready())

    def test_lan_connection_rejects_wrong_address_and_device_identity(self):
        class RuntimeStub:
            def ready_for_print(self):
                return True

        old_runtime = plugin._runtime
        plugin._runtime = RuntimeStub()
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        failures = []
        self.agent.set_on_local_connect_fn(lambda *args: failures.append(args))
        wrong = types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host="192.0.2.5", port=plugin.LAN_PORT)
        self.assertEqual(self.agent.connect_printer(wrong), -1)
        self.assertEqual(failures[-1][0], plugin.CONNECT_FAILED)
        self.assertFalse(self.agent._ready())

    def test_lan_connection_accepts_loopback_when_preset_port_is_blank_or_different(self):
        class RuntimeStub:
            def ready_for_print(self):
                return False

        old_runtime = plugin._runtime
        plugin._runtime = RuntimeStub()
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        connections = []
        self.agent.set_on_local_connect_fn(lambda *args: connections.append(args))

        for preset_port in ("", "8883"):
            with self.subTest(port=preset_port):
                params = types.SimpleNamespace(
                    dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST, port=preset_port
                )
                self.assertEqual(self.agent.connect_printer(params), 0)
                self.assertEqual(connections[-1][0], plugin.CONNECT_OK)
                self.agent.disconnect_printer()

    def test_native_callback_forwards_archive_and_plate_and_reports_progress(self):
        class RuntimeStub:
            def __init__(self):
                self.job = None

            def ready_for_print(self):
                return True

            def submit_job(self, job, progress, cancelled):
                self.job = job
                progress(Progress("uploading", 50, 100))
                return SubmissionResult(SubmissionStatus.SUBMITTED, "Added to Grove", 8, (19,))

        runtime = RuntimeStub()
        old_runtime = plugin._runtime
        plugin._runtime = runtime
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        self.assertEqual(
            self.agent.connect_printer(
                types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST, port=plugin.LAN_PORT)
            ),
            0,
        )
        updates = []
        params = types.SimpleNamespace(filename=str(self.path), plate_index=3)

        result = self.agent.start_local_print(params, lambda *args: updates.append(args), lambda: False)

        self.assertEqual(result, 0)
        self.assertEqual(runtime.job.path, self.path)
        self.assertEqual(runtime.job.filename, "plate.gcode.3mf")
        self.assertEqual(runtime.job.plate_id, 3)
        self.assertIn((1, 50, "Uploading sliced plate to Grove"), updates)
        self.assertEqual(updates[-1][0], 6)

    def test_native_lan_callbacks_accept_hidden_3mf_with_project_upload_name(self):
        jobs = []

        class RuntimeStub:
            def ready_for_print(self):
                return True

            def submit_job(self, job, progress, cancelled):
                jobs.append(job.validate())
                return SubmissionResult(SubmissionStatus.SUBMITTED, "Added to Grove", 8, (19,))

        old_runtime = plugin._runtime
        plugin._runtime = RuntimeStub()
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        self.agent.connect_printer(types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST))
        native_path = self.path.with_name(".52871.0.3mf")
        make_export(native_path)
        original = native_path.read_bytes()
        params = types.SimpleNamespace(filename=str(native_path), project_name="3DBenchy", plate_index=3)
        for callback, extra in (
            (self.agent.start_local_print, ()),
            (self.agent.start_local_print_with_record, (None,)),
        ):
            with self.subTest(callback=callback.__name__):
                updates = []
                self.assertEqual(
                    callback(params, lambda *args: updates.append(args), lambda: False, *extra), 0
                )
                self.assertEqual(jobs[-1].path, native_path)
                self.assertEqual(jobs[-1].filename, "3DBenchy.gcode.3mf")
                self.assertEqual(jobs[-1].plate_id, 3)
                self.assertEqual(updates[-1][0], 6)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(native_path.read_bytes(), original)

    def test_verify_job_preflight_is_readiness_only(self):
        class RuntimeStub:
            calls = 0

            def ready_for_print(self):
                return True

            def submit_job(self, *args):
                self.calls += 1
                raise AssertionError("preflight must never submit")

        runtime = RuntimeStub()
        old_runtime = plugin._runtime
        plugin._runtime = runtime
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        self.agent.connect_printer(
            types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST, port=plugin.LAN_PORT)
        )
        self.assertEqual(
            self.agent.start_send_gcode_to_sdcard(
                types.SimpleNamespace(project_name="verify_job"), None, None, None
            ),
            0,
        )
        self.assertEqual(
            self.agent.start_send_gcode_to_sdcard(
                types.SimpleNamespace(project_name="ordinary_upload"), None, None, None
            ),
            -7010,
        )
        self.assertEqual(runtime.calls, 0)

    def test_adapter_answers_status_requests_and_rejects_physical_commands(self):
        class RuntimeStub:
            def ready_for_print(self):
                return True

        old_runtime = plugin._runtime
        plugin._runtime = RuntimeStub()
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        messages = []
        self.agent.set_on_local_message_fn(lambda *args: messages.append(args))
        self.agent.connect_printer(
            types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST, port=plugin.LAN_PORT)
        )
        self.assertEqual(
            self.agent.send_message(
                plugin.DEVICE_ID,
                json.dumps({"pushing": {"command": "pushall"}}),
                1,
                0,
            ),
            0,
        )
        self.assertEqual(
            self.agent.send_message_to_printer(
                plugin.DEVICE_ID,
                json.dumps({"print": {"command": "set_bed", "target_temp": 60}}),
                1,
                0,
            ),
            -7010,
        )
        self.assertEqual(
            self.agent.send_message(
                plugin.DEVICE_ID,
                json.dumps({"system": {"command": "home"}}),
                1,
                0,
            ),
            -7010,
        )
        self.assertGreaterEqual(len(messages), 4)

    def test_orca_metadata_requests_are_answered_when_grove_is_signed_out(self):
        class RuntimeStub:
            def ready_for_print(self):
                return False

        old_runtime = plugin._runtime
        plugin._runtime = RuntimeStub()
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        messages, updates, connections = [], [], []
        self.agent.set_on_local_connect_fn(lambda *args: connections.append(args))
        self.agent.set_on_local_message_fn(lambda *args: messages.append(args))
        # Match the Send dialog: metadata requests arrive before connect_printer,
        # which Orca may skip when this is already its selected LAN machine.
        self.assertEqual(
            self.agent.send_message_to_printer(
                plugin.DEVICE_ID, json.dumps({"info": {"command": "get_version"}}), 0, 0
            ),
            0,
        )
        self.assertEqual(
            self.agent.send_message_to_printer(
                plugin.DEVICE_ID, json.dumps({"pushing": {"command": "pushall"}}), 0, 0
            ),
            0,
        )
        replies = [json.loads(item[1]) for item in messages]
        version = next(
            item["info"] for item in replies if item.get("info", {}).get("command") == "get_version"
        )
        full_status = next(item["print"] for item in replies if "print" in item)
        self.assertEqual(version["module"][0]["name"], "ota")
        self.assertEqual(version["result"], "success")
        self.assertEqual(full_status["command"], "push_status")
        self.assertEqual(full_status["msg"], 0)
        self.assertEqual(connections[0][0], plugin.CONNECT_OK)
        diagnostic = self.agent.diagnostics()
        self.assertEqual(diagnostic["connect_calls"], 0)
        self.assertEqual(diagnostic["metadata_started_connection"], 1)
        self.assertEqual(diagnostic["metadata_requests"], {"get_version": 1, "pushall": 1})
        self.assertTrue(diagnostic["adapter_connected"])
        self.assertEqual(
            self.agent.start_send_gcode_to_sdcard(
                types.SimpleNamespace(project_name="verify_job"),
                lambda *args: updates.append(args),
                None,
                None,
            ),
            -1,
        )
        self.assertIn("sign in", updates[-1][2].lower())

    def test_later_explicit_connection_preserves_metadata_started_session(self):
        messages = []
        self.agent.set_on_local_message_fn(lambda *args: messages.append(args))
        self.assertEqual(
            self.agent.send_message_to_printer(
                plugin.DEVICE_ID, json.dumps({"info": {"command": "get_version"}}), 0, 0
            ),
            0,
        )
        self.assertEqual(
            self.agent.connect_printer(types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST)),
            0,
        )
        self.assertEqual(self.agent.diagnostics()["connect_calls"], 1)
        self.assertTrue(self.agent.diagnostics()["adapter_connected"])
        self.assertEqual(len(messages), 6)

    def test_physical_commands_and_other_devices_do_not_start_metadata_session(self):
        self.assertEqual(
            self.agent.send_message_to_printer(
                plugin.DEVICE_ID, json.dumps({"print": {"command": "set_bed", "target_temp": 60}}), 0, 0
            ),
            -7010,
        )
        self.assertEqual(
            self.agent.send_message_to_printer(
                "other-device", json.dumps({"info": {"command": "get_version"}}), 0, 0
            ),
            -1,
        )
        self.assertFalse(self.agent.diagnostics()["adapter_connected"])
        self.assertEqual(self.agent.diagnostics()["metadata_requests"], {})

    def test_callback_failures_are_visible_without_payloads_or_exception_messages(self):
        def failing_callback(*args):
            raise RuntimeError("sensitive-value-in-exception")

        self.agent.set_on_local_message_fn(failing_callback)
        self.agent.send_message_to_printer(
            plugin.DEVICE_ID,
            json.dumps({"info": {"command": "get_version"}, "extra": "sensitive-payload-value"}),
            0,
            0,
        )
        self.script._state_changed(self.script._runtime.snapshot())
        diagnostic = self.script._panel.messages[-1]["agent_diagnostics"]
        self.assertEqual(diagnostic["callback_errors"], {"local_message": "RuntimeError"})
        self.assertEqual(diagnostic["metadata_requests"], {"get_version": 1})
        self.assertNotIn("sensitive", json.dumps(diagnostic))

    def test_cloud_fallback_is_rejected_without_second_submission(self):
        class RuntimeStub:
            calls = 0

            def ready_for_print(self):
                return True

            def submit_job(self, *args):
                self.calls += 1
                return SubmissionResult(SubmissionStatus.SUBMITTED, "Added", 8, (19,))

        runtime = RuntimeStub()
        old_runtime = plugin._runtime
        plugin._runtime = runtime
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        self.agent.connect_printer(
            types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST, port=plugin.LAN_PORT)
        )
        params = types.SimpleNamespace(filename=str(self.path), plate_index=1)
        self.assertEqual(self.agent.start_local_print(params, None, None), 0)
        self.assertEqual(self.agent.start_print(params, None, None, None), -7010)
        self.assertEqual(runtime.calls, 1)

    def test_local_callbacks_share_single_flight_submission_guard(self):
        entered, release = threading.Event(), threading.Event()

        class RuntimeStub:
            calls = 0

            def ready_for_print(self):
                return True

            def submit_job(self, job, progress, cancelled):
                self.calls += 1
                entered.set()
                self.assert_job(job)
                release.wait(2)
                return SubmissionResult(SubmissionStatus.SUBMITTED, "Added", 8, (19,))

            @staticmethod
            def assert_job(job):
                assert job.path == self_path
                assert job.plate_id == 3

        runtime = RuntimeStub()
        self_path = self.path
        old_runtime = plugin._runtime
        plugin._runtime = runtime
        self.addCleanup(setattr, plugin, "_runtime", old_runtime)
        self.agent.connect_printer(
            types.SimpleNamespace(dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST, port=plugin.LAN_PORT)
        )
        params = types.SimpleNamespace(filename=str(self.path), plate_index=3)
        first_result = []
        worker = threading.Thread(
            target=lambda: first_result.append(self.agent.start_local_print(params, None, None))
        )
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            duplicate_result = self.agent.start_local_print_with_record(params, None, None, None)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first_result, [0])
        self.assertEqual(duplicate_result, -1)
        self.assertEqual(runtime.calls, 1)

    def test_native_callback_gates_disconnected_state_and_never_uploads(self):
        updates = []
        params = types.SimpleNamespace(filename=str(self.path), plate_index=1)
        result = self.agent.start_print(params, lambda *args: updates.append(args), None, None)
        self.assertNotEqual(result, 0)
        self.assertEqual(updates[-1][0], 7)

    def test_native_callback_maps_cancelled_partial_and_unknown_outcomes(self):
        class RuntimeStub:
            def __init__(self, result):
                self.result = result

            def ready_for_print(self):
                return True

            def submit_job(self, job, progress, cancelled):
                return self.result

        for status, expected in (
            (SubmissionStatus.CANCELLED, -18),
            (SubmissionStatus.PARTIAL, -1),
            (SubmissionStatus.UNKNOWN, -1),
        ):
            with self.subTest(status=status):
                old_runtime = plugin._runtime
                plugin._runtime = RuntimeStub(SubmissionResult(status, f"{status.value} result", 8))
                try:
                    self.agent.connect_printer(
                        types.SimpleNamespace(
                            dev_id=plugin.DEVICE_ID, host=plugin.LAN_HOST, port=plugin.LAN_PORT
                        )
                    )
                    result = self.agent.start_local_print(
                        types.SimpleNamespace(filename=str(self.path), plate_index=1), None, None
                    )
                finally:
                    plugin._runtime = old_runtime
                self.assertEqual(result, expected)

    def test_unload_disconnects_public_adapter(self):
        self.script.on_unload()
        with self.assertRaises(GroveError):
            plugin.connection_state()


if __name__ == "__main__":
    unittest.main()
