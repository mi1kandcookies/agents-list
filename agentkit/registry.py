"""
agentkit/registry.py - find and load specialists.

    list_specialists()        manifests of every specialists/<package>/agent.yaml
    load_specialist(slug)     import specialists.<package>.agent and instantiate
                              its Specialist subclass; a package without
                              agent.py gets the plain Specialist base, so
                              list/show/estimate work before agent.py exists

load_specialist(..., spec_hash=...) checks the package against the stamped
spec_hash before any of its code is imported. Every load refuses a package
that Python would import from anywhere but root/<package> (e.g. a
same-named package earlier on sys.path), checked before its code runs, so
the code that runs is the code that was hashed.

`root` and `package` let tests point at a fixture tree instead of
specialists/.
"""
from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from agentkit.errors import AgentKitError, ManifestError
from agentkit.manifest import Manifest, load_manifest, verify_spec_hash
from agentkit.specialist import Specialist, check_import_location, check_module_file

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "specialists"
DEFAULT_PACKAGE = "specialists"


def list_specialists(root: Path | None = None, *, strict: bool = False,
                     errors: list[tuple[Path, str]] | None = None) -> list[Manifest]:
    """Every loadable manifest under `root`, sorted by slug. Broken manifests
    raise with strict=True; otherwise they are skipped and, when `errors` is
    a list, reported in it as (path, message)."""
    root = Path(root or DEFAULT_ROOT)
    out = []
    for path in sorted(root.glob("*/agent.yaml")):
        try:
            out.append(load_manifest(path))
        except ManifestError as exc:
            if strict:
                raise
            if errors is not None:
                errors.append((path, str(exc)))
    return sorted(out, key=lambda m: m.slug)


def _module_name(package: str | None, name: str) -> str:
    return f"{package}.{name}" if package else name


def load_specialist(slug: str, *, root: Path | None = None,
                    package: str | None = DEFAULT_PACKAGE, spec_hash: str | None = None) -> Specialist:
    """The specialist `slug` from root/<package>. With `spec_hash` (the
    operator-stamped value) a package whose runtime files hash differently
    is refused before any of its code is imported."""
    root = Path(root or DEFAULT_ROOT)
    pkg = slug.replace("-", "_")
    manifest_path = root / pkg / "agent.yaml"
    if not manifest_path.is_file():
        known = ", ".join(m.slug for m in list_specialists(root)) or "none"
        raise AgentKitError(f"unknown specialist {slug!r} (known: {known})")
    manifest = load_manifest(manifest_path)
    if manifest.slug != slug:
        raise AgentKitError(f"{manifest_path} declares slug {manifest.slug!r}, expected {slug!r}")
    if spec_hash is not None:
        try:
            verify_spec_hash(manifest, spec_hash)
        except ManifestError as exc:
            raise AgentKitError(f"{slug}: {exc}; nothing was run") from None
    package_name = _module_name(package, pkg)
    check_import_location(package_name, root / pkg)
    module_name = f"{package_name}.agent"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name not in (module_name, package_name):
            raise
        return Specialist(manifest, package=package_name)
    check_module_file(module, root / pkg)
    classes = [obj for obj in vars(module).values()
               if inspect.isclass(obj) and issubclass(obj, Specialist) and obj is not Specialist
               and obj.__module__ == module.__name__]
    if len(classes) != 1:
        raise AgentKitError(f"{module_name} must define exactly one Specialist subclass, found {len(classes)}")
    return classes[0](manifest)


__all__ = ["DEFAULT_ROOT", "list_specialists", "load_specialist"]
