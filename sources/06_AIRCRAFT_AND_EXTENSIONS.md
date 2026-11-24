# Aircraft and extension boundaries

User-installable modules are a deployment requirement. The owner wants pilots to use compatible modules without editing code. The module package, install and enable workflow, compatibility and access model remain open.

On 24 November 2026, the owner confirmed that PMDG-specific integration is deferred beyond C0.1 and belongs in an EFB plugin. FSUIPC integration is also deferred beyond C0.1. Fenix and iniBuilds are later research. Vendor broadcasts, aircraft-specific writes and a public SDK distribution need separate scope decisions.

The owner clarified the expected workflow. A developer reads the published Core API and module authoring guide, builds a compatible module independently, then drops it into the EFB's module location. The EFB discovers it and supplies its Core API connection when the module loads. Once compatible and enabled, it can use supported reads and writes without manual registration, Core edits or a separate SimConnect setup. The host should report a missing prerequisite or version mismatch with a clear reason. The exact package format, discovery mechanism and trust model remain design choices.

The first functional module planned after the initial EFB and Logbook build is Aircraft Status. It will display read-only aircraft telemetry such as speed, total aircraft weight and heading through the Core consumer API. Confirm the precise readings and display units when the module is scoped. The first EFB build uses the Aircraft Status placeholder to demonstrate the launcher.

Prioritise generic MSFS 2020 telemetry and measure support across representative aircraft. Distinguish SDK documentation, candidate fields and observed live behaviour. The preserved Aircraft Plugin Development Framework v0.5 holds the parked research and security constraints.

vPilot, SimBrief, VATSIM, Hoppie, weather and chart integrations remain proposals subject to provider, permission, feasibility and licensing validation. Any future commanded simulator actions require separate capability design and approval.
