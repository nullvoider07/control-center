"""The distribution must actually contain the code, and import it cleanly.

Every property here was broken before it was written, and each failed silently:

* `setup.py` used `find_packages()` from the repo root while the package lives at
  crates/controller, so it resolved to nothing and built a distribution with no
  modules — while `entry_points` still installed a `control-center` script. The
  documented `make install` therefore produced a command that died on
  `ModuleNotFoundError: No module named 'controller'`.
* The version lived in both setup.py and cli.py, and v1.2.0 shipped with the CLI
  still reporting 1.1.0.
* grpc_tools emits a bare `import control_center_pb2` into the _grpc stub, which
  resolves only with the proto directory on sys.path. The fallback that arranged
  that injected a site-packages subdirectory into sys.path and bound the generic
  name `control_center_pb2` as a top-level module.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

from setuptools import find_packages

REPO_ROOT = Path(__file__).resolve().parents[2]
CRATES = REPO_ROOT / "crates"
PROTO_DIR = CRATES / "controller" / "integrations" / "proto"

EXPECTED_PACKAGES = {
    "controller",
    "controller.core",
    "controller.integrations",
    "controller.integrations.proto",
    "controller.management",
    "controller.os_specific",
    "controller.utils",
}


def _package_version() -> str:
    source = (CRATES / "controller" / "__init__.py").read_text()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("controller/__init__.py defines no __version__")


def test_setup_packages_every_module():
    """The exact call setup.py makes must find the whole tree. A distribution that
    silently contains nothing is the failure this guards."""
    found = set(find_packages(where=str(CRATES)))
    missing = EXPECTED_PACKAGES - found
    assert not missing, (
        f"setup.py would ship a distribution missing {sorted(missing)} — "
        f"found only {sorted(found)}"
    )


def test_generated_protos_are_packaged():
    """proto/ is only included because it carries an __init__.py. Without it the
    stubs are data files that no installed package can import."""
    assert (PROTO_DIR / "__init__.py").exists()
    assert "controller.integrations.proto" in find_packages(where=str(CRATES))


def test_setup_and_package_agree_on_version():
    reported = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.strip().splitlines()[-1]
    assert reported == _package_version(), (
        f"setup.py reports {reported} but the package declares {_package_version()}"
    )


def test_cli_reports_the_packaged_version():
    """`--version` and the update check both read this; a stale literal here made
    the tool report an older release than the one installed."""
    from controller.management import cli

    assert cli.__version__ == _package_version()


def test_python_and_rust_versions_match():
    """The three binaries and the CLI ship together, so a mismatch means one of the
    four reports a version that was never released as a set."""
    match = re.search(
        r'^version = "(.*?)"', (REPO_ROOT / "Cargo.toml").read_text(), re.M
    )
    assert match, "no workspace version found in Cargo.toml"
    assert match.group(1) == _package_version(), (
        f"Cargo.toml is {match.group(1)} but the Python package "
        f"is {_package_version()}"
    )


def test_readme_states_the_packaged_version():
    """The README header is the first thing a release points at, and it went out at
    1.1.0 while everything else had moved to 1.2.0."""
    match = re.search(r"^\*\*Version:\*\* *(\S+)", (REPO_ROOT / "README.md").read_text(), re.M)
    assert match, "no '**Version:**' header found in README.md"
    assert match.group(1) == _package_version(), (
        f"README says {match.group(1)} but the package is {_package_version()}"
    )


def test_generated_stub_imports_its_sibling_relatively():
    """A bare `import control_center_pb2` resolves only via a sys.path hack.
    scripts/generate_proto.py rewrites it; this catches a regeneration that bypassed
    the script."""
    source = (PROTO_DIR / "control_center_pb2_grpc.py").read_text()
    bare = re.findall(r"^import (\w+_pb2) as", source, re.M)
    assert not bare, (
        f"{bare} imported as top-level modules — regenerate with "
        "scripts/generate_proto.py"
    )
    assert re.search(r"^from \. import \w+_pb2 as", source, re.M), (
        "no relative sibling import found in the generated stub"
    )


def test_importing_the_client_does_not_touch_global_state():
    """Run in a subprocess: the suite has already imported these, so sys.modules
    here cannot show what a first import does."""
    probe = """
import sys, json
before = list(sys.path)
import controller.integrations.gRPC  # noqa: F401
print(json.dumps({
    "added": [p for p in sys.path if p not in before],
    "squatted": [m for m in sys.modules if m.endswith("_pb2") or m.endswith("_pb2_grpc")],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT, capture_output=True, text=True,
        env={"PYTHONPATH": str(CRATES), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    report = __import__("json").loads(result.stdout.strip().splitlines()[-1])

    assert not report["added"], (
        f"importing the client injected {report['added']} into sys.path"
    )
    assert all(m.startswith("controller.") for m in report["squatted"]), (
        f"generated modules bound outside the package: {report['squatted']} — "
        "these collide with any other module of the same name"
    )
