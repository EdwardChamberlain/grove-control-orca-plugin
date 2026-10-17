"""Grove Control integration package for OrcaSlicer."""

from importlib.metadata import PackageNotFoundError, version

try:
    from ._version import version as __version__
except ImportError:
    try:
        __version__ = version("grove-control-orca-plugin")
    except PackageNotFoundError:
        __version__ = "0+unbuilt"

from .plugin import connection_state, submit_sliced_job  # noqa: E402

__all__ = ["__version__", "connection_state", "submit_sliced_job"]

try:
    import orca as _orca
except ModuleNotFoundError as _exc:
    if _exc.name != "orca":
        raise
else:
    from .plugin import register_plugin as _register_plugin

    _register_plugin(_orca)
