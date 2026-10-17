import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("hatch_vcs"), "Install the dev extra for packaging checks")
class PackagingTests(unittest.TestCase):
    def test_tagged_wheel_version_resources_and_orca_package_discovery(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for name in ("pyproject.toml", "README.md", ".gitignore"):
                shutil.copy2(root / name, project / name)
            shutil.copytree(
                root / "src", project / "src", ignore=shutil.ignore_patterns("__pycache__", "_version.py")
            )

            def run(*args):
                return subprocess.run(args, cwd=project, check=True, text=True, capture_output=True)

            run("git", "init", "-q")
            run("git", "add", ".")
            run(
                "git",
                "-c",
                "user.name=Packaging check",
                "-c",
                "user.email=check@example.invalid",
                "commit",
                "-qm",
                "Scaffold",
            )
            run("git", "tag", "v2.3.4")
            run(sys.executable, "-m", "build", "--wheel", "--no-isolation")
            wheel = next((project / "dist").glob("*.whl"))
            self.assertIn("-2.3.4-py3-none-any.whl", wheel.name)
            with zipfile.ZipFile(wheel) as archive:
                self.assertIn("grove_control_orca_plugin/resources/connection.html", archive.namelist())
                metadata = next(name for name in archive.namelist() if name.endswith("/METADATA"))
                self.assertIn("Version: 2.3.4\n", archive.read(metadata).decode())
            # An embedded Orca import registers the Script panel and PrinterAgent.
            script = """
import json, sys, types
sys.path.insert(0, sys.argv[1])
packages, capabilities = [], []
class Capability:
    def get_config(self): return '{}'
    def save_config(self, value): return True
class AgentBase: pass
def decorate(cls):
    packages.append(cls)
    return cls
orca = types.ModuleType('orca')
orca.base = object
orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=Capability)
orca.printer_agent = types.SimpleNamespace(
    PrinterAgentBase=AgentBase,
    AgentInfo=lambda *v: types.SimpleNamespace(id=v[0], name=v[1], version=v[2], description=v[3]),
)
orca.host = types.SimpleNamespace(ui=object())
orca.plugin = decorate
orca.register_capability = capabilities.append
sys.modules['orca'] = orca
import grove_control_orca_plugin as plugin
assert len(packages) == 1
packages[0]().register_capabilities()
assert len(capabilities) == 2
page = capabilities[0]()
agent = capabilities[1]()
page.on_load()
assert page.get_name() == 'Grove Control'
assert agent.get_agent_info().id == 'grove-control-queue'
assert plugin.connection_state()['ready_for_print'] is False
page.on_unload()
agent.on_unload()
print(json.dumps({'version': plugin.__version__}))
"""
            result = run(sys.executable, "-I", "-c", script, str(wheel))
            self.assertEqual(json.loads(result.stdout)["version"], "2.3.4")
