"""Logbook pages for the approved Q1-Q14 design: Dashboard, Flight History (Table, Cards,
Combined), the in-EFB Flight Details overlay, Black Box Recorder Management and Statistics.

Every page works from read-only ``FlightView`` objects (``model.py``), built from the
stored record dictionaries or from ``bbc_efb.storage.Flight`` objects. See
``LOGBOOK_VIEWS.md`` in this package for the record mapping, the integration steps and
the proposals waiting for owner decisions.

Names are imported lazily so ``bbc_efb.logbook_views.model`` stays importable without
Qt (for tools and tests that only need the view model).
"""

from importlib import import_module

_EXPORTS = {
    # model (no Qt)
    "FlightView": "model", "flight_view": "model", "flight_views": "model",
    "flight_view_from_record": "model", "load_journal": "model", "ViewError": "model",
    # filters
    "FilterState": "filters", "FilterBar": "filters", "compile_filter": "filters",
    # pages and widgets
    "FlightHistoryPage": "history", "FlightTableModel": "history", "FlightFilterProxy": "history",
    "FlightCardView": "cards", "FlightDetailsOverlay": "details", "journal_loader_for": "details",
    "DashboardPage": "dashboard", "LogbookWorkspace": "dashboard",
    "StatisticsPage": "statistics", "summarise": "statistics", "LogbookSummary": "statistics",
    "RecorderManagementPage": "recorder_management", "StoreActions": "recorder_management",
    "GapAcknowledger": "recorder_management", "attention_queue": "recorder_management",
    "AircraftArtLibrary": "aircraft_art", "AircraftArtWidget": "aircraft_art", "classify": "aircraft_art",
    "shared_library": "aircraft_art",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value
