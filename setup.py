import ast
from pathlib import Path

from setuptools import setup, find_packages


def _version() -> str:
    """Read __version__ from the package without importing it.

    Importing controller at build time would require its dependencies to already be
    installed, which is exactly what this file is declaring.
    """
    source = Path(__file__).parent / "crates" / "controller" / "__init__.py"
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(f"no __version__ found in {source}")


setup(
    name="control-center",
    version=_version(),
    description="Unified multi-OS control center for CUA actuation",
    author="Kartik A (NullVoider)",
    license="GPL-3.0",
    # The package lives at crates/controller, not at the repo root. Without
    # package_dir, find_packages() searches the root, finds nothing, and produces a
    # distribution containing no modules at all — while entry_points still installs a
    # `control-center` script, so the CLI installs and then fails on import.
    package_dir={"": "crates"},
    packages=find_packages(where="crates"),
    install_requires=[
        "grpcio>=1.60.0",
        "grpcio-tools>=1.60.0",
        "protobuf>=4.25.0",
        "click>=8.1.0",
        "requests>=2.31.0",
        "cryptography>=42.0.0",
        "keyring>=24.0.0",
        # Imported unconditionally by integrations/status.py and integrations/export.py,
        # and integrations/__init__.py imports export — so without psutil the whole
        # integrations package, including the gRPC client, fails to import.
        "psutil>=5.9.0",
        # The D-Bus client the Wayland actuation daemon speaks the XDG portal with.
        # Linux-only, and imported lazily behind a try/except so the CLI still runs
        # without it — but the Wayland backend does not, and the failure it produced
        # was a runtime "python3-jeepney is required" rather than a resolvable
        # dependency. Marked rather than made unconditional because macOS and Windows
        # have no portal to talk to.
        "jeepney>=0.8.0; sys_platform == 'linux'",
    ],
    # wayland_cursor.c is compiled on first use by CursorTracker.build, which locates
    # it next to wayland_portal.py. setuptools installs .py and nothing else, so
    # without this the source is absent from an installed copy and cursor readback
    # fails with "… is missing" — degrading every Wayland position report to the
    # unverified path, silently.
    package_data={"controller.os_specific": ["wayland_cursor.c"]},
    entry_points={
        'console_scripts': [
            'control-center=controller.management.cli:main',
            # The agent spawns this by name for Wayland actuation and for the
            # position readback, so it must be on PATH or the whole Wayland
            # backend fails with "No such file or directory". It is a console
            # script rather than a data file because the agent execs a name, not
            # an interpreter plus a path.
            'cc-wayland-actuate=controller.os_specific.wayland_portal:console_main',
        ],
    },
    python_requires=">=3.8",
)