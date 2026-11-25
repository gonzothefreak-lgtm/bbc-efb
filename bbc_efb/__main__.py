"""Launch the Windows EFB from the workspace."""

from argparse import ArgumentParser
from pathlib import Path

from bbc_core.provider import ProviderError

from PySide6.QtWidgets import QApplication

from .session import native_session
from .shell import EfbWindow
from .theme import STYLE


def main(argv=None):
    parser = ArgumentParser(description="Launch BBC EFB.")
    parser.add_argument("--simconnect-dll", type=Path, metavar="PATH",
                        help="Enable the Connect control using this local SimConnect DLL.")
    options = parser.parse_args(argv)
    session = None
    if options.simconnect_dll is not None:
        try:
            session = native_session(options.simconnect_dll)
        except ProviderError as error:
            parser.error(str(error))

    app = QApplication([])
    app.setApplicationName("BBC EFB")
    app.setOrganizationName("Black Box Club")
    app.setStyleSheet(STYLE)
    if session is None:
        window = EfbWindow()
        window.show()
        return app.exec()

    reader = session.core.register_consumer()
    try:
        window = EfbWindow(connection_reader=reader, request_connection=session.request_connection,
                           register_recorder=session.core.register_consumer)
        session.start()
        try:
            window.show()
            return app.exec()
        finally:
            session.stop()
    finally:
        reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
