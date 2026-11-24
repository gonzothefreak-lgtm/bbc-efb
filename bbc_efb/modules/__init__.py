"""Drop-in EFB modules (EXPERIMENTAL, second release; off by default).

A module is a folder inside ``<data folder>/modules`` that holds a
``module.json`` manifest and a Python package. The host discovers it, checks
the manifest and compatibility, and, once the pilot has turned the feature and
the module on, imports it and hands it a Core ``ConsumerAPI`` (from
``CoreHost.register_consumer`` when the host is given a CoreHost) limited to the
control groups the pilot granted. Built-in modules are registered in code and
receive their related control groups automatically (owner decision).

Modules are trusted in-process Python code: there is no sandbox. See
``docs/MODULE_AUTHORING.md``. This package is Qt-free and SimConnect-free.
"""

from .loader import (
    DEFAULT_CONTROL_GROUPS, EXPERIMENTAL_LABEL, MODULE_DATA_FOLDER, MODULES_FOLDER, STATE_FILE, TRUST_NOTICE,
    ApiProvider, ControlGroup, ModuleContext, ModuleFactory, ModuleHost, ModulePolicy, ModulePreferences,
    ModuleRecord, ModuleSetting, ModuleSettingsReadOnly, ModuleStateStore, ModuleStatus, PartialGrant, ScopedAPI, as_api_provider,
    bridge_api_provider, core_api_provider, host_api_provider,
)
from .manifest import (
    MANIFEST_FORMAT, MANIFEST_NAME, EntryPoint, ManifestResult, ModuleManifest, Problem, ProblemCode,
    Requirements, Version, VersionRange, api_compatibility, manifest_from_dict, read_manifest,
)

__all__ = [
    "DEFAULT_CONTROL_GROUPS", "EXPERIMENTAL_LABEL", "MANIFEST_FORMAT", "MANIFEST_NAME", "MODULE_DATA_FOLDER",
    "MODULES_FOLDER", "STATE_FILE", "TRUST_NOTICE", "ApiProvider", "ControlGroup", "EntryPoint", "ManifestResult",
    "ModuleContext", "ModuleFactory", "ModuleHost", "ModuleManifest", "ModulePolicy", "ModulePreferences",
    "ModuleRecord", "ModuleSetting", "ModuleSettingsReadOnly", "ModuleStateStore", "ModuleStatus", "PartialGrant", "Problem", "ProblemCode",
    "Requirements", "ScopedAPI", "Version", "VersionRange", "api_compatibility", "as_api_provider",
    "bridge_api_provider", "core_api_provider", "host_api_provider", "manifest_from_dict", "read_manifest",
]
