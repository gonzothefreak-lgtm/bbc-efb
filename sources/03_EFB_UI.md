# EFB shell and UI

- App frame: compact dark borderless Windows shell, custom window controls, and usable minimise, maximise, restore, close and resizing. Hybrid adaptive layout reflows where useful and scrolls where minimum usable dimensions require it.
- Dispatch: default Section on launch. Present approved pre-flight workflow structure, with unavailable services labelled.
- In-Flight: keep the right-hand Modules launcher accessible during resizing; retain a shared visual system. Auto-collapse behaviour remains open.
- Logbook: present the approved pages and views described in [Logbook and Flight Recorder](04_LOGBOOK_AND_RECORDER.md).
- Settings: make it available throughout the EFB. Include SimConnect diagnostics, Flight Logging & Privacy, and Data Management. Export All Data and restorable backup are distinct.
- Persistent footer: show independent side-by-side SimConnect connection and Flight Logging state. SimConnect opens Settings > SimConnect > Connection Diagnostics. Flight Logging shows appropriate status, current-flight details and controls as implemented; manual Stop requires confirmation.
- While logging is active, closing the app presents Cancel or Save & Exit. Keep incomplete flights truthful and report save failures.

Use the preserved illustrated UI Standards v1.2 and Logbook Design Record v1.2 for design reference. Sample metrics and historical illustrations are not implementation evidence. Test narrow layouts, Windows scaling, keyboard navigation and accessible state indicators in a real concept.
