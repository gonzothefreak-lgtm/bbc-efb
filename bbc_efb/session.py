"""Own one in-process Core session for the lifetime of the EFB."""

from threading import Event, Thread

from bbc_core import ConnectionState, Core
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_sdk import SimConnectSdk


class SessionHost:
    """Run all provider operations on one worker away from the UI."""

    def __init__(self, core: Core):
        self.core = core
        self._requested = Event()
        self._stopping = Event()
        self._worker = Thread(target=self._run, name="BBC Core session")

    def start(self):
        self._worker.start()

    def request_connection(self):
        self._requested.set()

    def stop(self):
        self._stopping.set()
        self._requested.set()
        self._worker.join()

    def _run(self):
        reader = self.core.register_consumer()
        try:
            while not self._stopping.is_set():
                state = reader.connection().state
                connected = state is ConnectionState.CONNECTED
                requested = self._requested.wait(0.05 if connected else None)
                if self._stopping.is_set():
                    break
                if requested:
                    self._requested.clear()
                    if not connected:
                        if state is ConnectionState.ERROR:
                            self.core.disconnect()
                        if not self._stopping.is_set():
                            self.core.connect()
                elif connected:
                    self.core.pump()
        finally:
            try:
                self.core.disconnect()
            finally:
                reader.close()


def native_session(dll_path):
    sdk = SimConnectSdk(dll_path, callback_dispatch=True, load_on_open=True)
    return SessionHost(Core(SimConnectProvider(sdk)))
