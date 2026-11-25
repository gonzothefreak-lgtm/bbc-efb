"""Exercise the EFB widgets in Qt's offscreen platform on the local runtime."""

from importlib.util import find_spec
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from unittest import TestCase, skipUnless
from unittest.mock import patch

from bbc_core import ConnectionState, Core, CoreRequestError
from bbc_core.provider import ProviderError
from bbc_core.simconnect_abi import Receive, ReceiveId
from bbc_core.simulated import SimulatedProvider
from test_flight_recorder_offline import FlightReplay, RecorderReplay
from test_simconnect_provider_offline import FakeSdk, packet


@skipUnless(find_spec("PySide6"), "PySide6 is required for the offline UI checks")
class EfbShellUiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QSettings, Qt
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

        from bbc_efb.shell import EfbWindow
        from bbc_efb.theme import STYLE

        platform = patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"})
        platform.start()
        cls.addClassCleanup(platform.stop)
        cls.app = QApplication.instance() or QApplication([])
        windows = os.environ.get("WINDIR")
        if windows:
            font = Path(windows) / "Fonts" / "segoeui.ttf"
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
        cls.app.setStyleSheet(STYLE)
        cls.window_type = EfbWindow
        cls.settings_type = QSettings
        cls.label_type = QLabel
        cls.button_type = QPushButton
        cls.message_box = QMessageBox
        cls.mouse_click = staticmethod(QTest.mouseClick)
        cls.wait = staticmethod(QTest.qWait)
        cls.left_button = Qt.MouseButton.LeftButton

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "Pilot Logbook"
        self.data.mkdir()
        self.window = self.open_window()

    def open_window(self, *, connection_reader=None, request_connection=None, register_recorder=None):
        settings = self.settings_type(str(self.root / "settings.ini"), self.settings_type.Format.IniFormat)
        window = self.window_type(settings, connection_reader=connection_reader,
                                  request_connection=request_connection, register_recorder=register_recorder)
        if connection_reader is not None:
            self.addCleanup(connection_reader.close)
        self.addCleanup(window.deleteLater)
        window.show()
        self.app.processEvents()
        return window

    def click(self, control):
        self.assertTrue(control.isVisible(), control.text())
        self.assertTrue(control.isEnabled(), control.text())
        self.mouse_click(control, self.left_button)
        self.app.processEvents()

    def assert_connection_status(self, window, expected):
        for _ in range(40):
            if window.connection_status.text() == expected:
                break
            self.wait(25)
        self.assertEqual(window.connection_status.text(), expected)

    def find_button(self, parent, text):
        return next(control for control in parent.findChildren(self.button_type)
                    if control.text() == text and control.isVisible())

    def pick_folder(self):
        with patch("bbc_efb.shell.QFileDialog.getExistingDirectory", return_value=str(self.data)):
            self.click(self.find_button(self.window, "Choose Logbook folder"))

    def open_manual_editor(self):
        self.pick_folder()
        self.click(self.window.section_buttons["Logbook"])
        logbook = self.window.logbook
        self.click(logbook.dashboard.new_flight)
        for name, value in (("aircraft", "A320"), ("departure", "YMML"), ("arrival", "YSSY")):
            logbook.editor.text_fields[name].setText(value)
        logbook.editor.minutes.setValue(80)
        return logbook

    def test_shell_launches_in_dispatch_and_keeps_controls_visible_when_narrow(self):
        window = self.window
        self.assertIs(window.sections.currentWidget(), window.pages["Dispatch"])
        self.assertTrue(window.folder_prompt.isVisible())
        self.assertFalse(window.windowIcon().isNull())
        self.assertEqual(window.title_layout.getItemPosition(window.title_layout.indexOf(window.navigation))[0], 0)
        window.resize(500, 540)
        self.app.processEvents()
        self.assertEqual(window.width(), 500)
        self.assertEqual(window.title_layout.getItemPosition(window.title_layout.indexOf(window.navigation))[0], 1)
        self.click(window.section_buttons["In-Flight"])
        for name in ("Aircraft Status", "Radios"):
            self.click(self.find_button(window, name))
            captions = [item.text() for item in window.module_preview.currentWidget().findChildren(self.label_type)]
            self.assertIn(name, captions)
            self.assertTrue(any("in development" in caption for caption in captions))
        self.assertEqual(window.connection_status.text(), "SimConnect · Disconnected")
        self.assertEqual(window.logging_status.text(), "Flight Logging · Inactive")
        self.click(window.connection_status)
        self.assertIs(window.sections.currentWidget(), window.pages["Settings"])
        self.assertEqual(window.settings_tabs.tabText(window.settings_tabs.currentIndex()), "SimConnect")
        self.assertEqual(window.connection_detail.text(), "No simulator provider is configured.")
        self.assertFalse(window.connect_button.isVisible())
        self.assertFalse(window._connection_timer.isActive())
        self.click(window.logging_status)
        self.assertEqual(window.settings_tabs.tabText(window.settings_tabs.currentIndex()), "Flight Logging & Privacy")
        self.assertFalse(window.start_logging.isVisible())
        self.assertFalse(window.stop_logging.isVisible())
        self.click(window.section_buttons["Logbook"])
        self.assertTrue(window.logbook._narrow_sidebar)
        window.resize(950, 650)
        self.app.processEvents()
        self.assertFalse(window.logbook._narrow_sidebar)

    def test_connection_status_follows_core_without_changing_flight_logging(self):
        provider = SimulatedProvider()
        core = Core(provider)
        self.addCleanup(core.disconnect)
        reader = core.register_consumer()
        window = self.open_window(connection_reader=reader)
        self.assertEqual(window.connection_status.text(), "SimConnect · Disconnected (simulated)")

        core.connect()
        self.assert_connection_status(window, "SimConnect · Connected (simulated)")
        self.click(window.connection_status)
        self.assertEqual(window.settings_tabs.tabText(window.settings_tabs.currentIndex()), "SimConnect")
        self.assertIn(provider.source.name, window.connection_source.text())
        self.assertTrue(window.connection_source.isVisible())
        self.assertFalse(window.connection_detail.isVisible())
        self.assertEqual(window.logging_status.text(), "Flight Logging · Inactive")

        provider.lose_connection()
        core.pump()
        self.assert_connection_status(window, "SimConnect · Disconnected (simulated)")
        self.assertEqual(window.connection_detail.text(), "Simulated transport loss.")
        self.assertEqual(window.logging_status.text(), "Flight Logging · Inactive")

        core.connect()
        self.assert_connection_status(window, "SimConnect · Connected (simulated)")
        self.assertFalse(window.connection_detail.isVisible())
        self.assertTrue(window.close())
        self.assertFalse(window._connection_timer.isActive())
        with self.assertRaises(CoreRequestError):
            reader.connection()
        other_reader = core.register_consumer()
        self.addCleanup(other_reader.close)
        self.assertEqual(other_reader.connection().state, ConnectionState.CONNECTED)

    def test_connection_error_exposes_core_detail_in_settings(self):
        core = Core(SimulatedProvider())
        self.addCleanup(core.disconnect)
        reader = core.register_consumer()
        window = self.open_window(connection_reader=reader)
        error = "<test> Provider could not open the connection."
        with patch.object(SimulatedProvider, "connect", side_effect=ProviderError(error)):
            core.connect()
        self.assert_connection_status(window, "SimConnect · Error · Connection failed (simulated)")
        self.click(window.connection_status)
        self.assertTrue(window.connection_detail.isVisible())
        self.assertEqual(window.connection_detail.text(), error)
        self.assertEqual(window.logging_status.text(), "Flight Logging · Inactive")
        self.assertTrue(window.close())

    def test_session_host_connects_retries_pumps_and_closes_on_one_worker(self):
        from bbc_efb.session import native_session

        sdk = FakeSdk()
        open_packet = sdk.open
        next_packet = sdk.next_packet
        close_packet = sdk.close
        allow_open = Event()
        fail_open = Event()
        fail_open.set()
        send_quit = Event()
        worker_ids = set()

        def open_session(application):
            worker_ids.add(get_ident())
            if not allow_open.wait(5) or fail_open.is_set():
                raise ProviderError("Fake SDK cannot find the simulator.")
            return open_packet(application)

        def receive(handle):
            worker_ids.add(get_ident())
            if send_quit.is_set():
                send_quit.clear()
                return packet(Receive, ReceiveId.QUIT)
            return next_packet(handle)

        def close_session(handle):
            worker_ids.add(get_ident())
            close_packet(handle)

        sdk.open = open_session
        sdk.next_packet = receive
        sdk.close = close_session
        dll = self.root / "SimConnect.dll"
        with patch("bbc_efb.session.SimConnectSdk", return_value=sdk) as sdk_type:
            host = native_session(dll)
        sdk_type.assert_called_once_with(dll, callback_dispatch=True, load_on_open=True)
        reader = host.core.register_consumer()
        window = self.open_window(connection_reader=reader, request_connection=host.request_connection)
        host.start()
        self.addCleanup(host.stop)
        self.addCleanup(allow_open.set)
        self.click(window.connection_status)
        self.assertEqual(window.connect_button.text(), "Connect")
        self.assertIsNone(sdk.handle)
        self.click(window.connect_button)
        self.assert_connection_status(window, "SimConnect · Connecting (simulated)")
        self.click(window.section_buttons["Logbook"])
        self.assertIs(window.sections.currentWidget(), window.logbook)
        allow_open.set()
        self.assert_connection_status(window, "SimConnect · Error · Connection failed (simulated)")
        self.click(window.connection_status)
        self.assertIn("Fake SDK cannot find the simulator", window.connection_detail.text())
        self.assertEqual(window.connect_button.text(), "Retry connection")

        fail_open.clear()
        self.click(window.connect_button)
        self.assert_connection_status(window, "SimConnect · Connected (simulated)")
        first_session = reader.connection().session_id
        self.assertFalse(window.connect_button.isVisible())

        send_quit.set()
        self.assert_connection_status(window, "SimConnect · Disconnected (simulated)")
        self.assertEqual(len(sdk.closed), 1)
        self.click(window.connect_button)
        self.assert_connection_status(window, "SimConnect · Connected (simulated)")
        self.assertNotEqual(reader.connection().session_id, first_session)
        self.assertEqual(window.logging_status.text(), "Flight Logging · Inactive")

        self.assertTrue(window.close())
        host.stop()
        self.assertIsNone(sdk.handle)
        self.assertEqual(len(sdk.closed), 2)
        self.assertEqual(len(worker_ids), 1)
        self.assertNotIn(get_ident(), worker_ids)

    def test_manual_flight_is_saved_edited_and_reopened_in_history(self):
        core = Core(SimulatedProvider())
        self.assertTrue(self.window.close())
        self.window = self.open_window(connection_reader=core.register_consumer())
        self.assertEqual(self.window.connection_status.text(), "SimConnect · Disconnected (simulated)")
        logbook = self.open_manual_editor()
        self.click(logbook.editor.save_button)
        first = logbook.selected
        self.assertEqual(first.fields.minutes, 80)
        self.click(self.find_button(self.window, "Edit flight"))
        logbook.editor.minutes.setValue(84)
        self.click(logbook.editor.save_button)
        self.assertEqual(logbook.selected.id, first.id)
        self.assertTrue(self.window.close())

        reopened = self.open_window(connection_reader=core.register_consumer())
        self.assertEqual(reopened.connection_status.text(), "SimConnect · Disconnected (simulated)")
        self.assertIs(reopened.sections.currentWidget(), reopened.pages["Dispatch"])
        self.assertFalse(reopened.folder_prompt.isVisible())
        self.assertEqual(reopened.logbook.store.root, self.data)
        self.click(reopened.section_buttons["Logbook"])
        self.click(reopened.logbook.nav["Flight History"])
        history = reopened.logbook.history[1]
        self.assertEqual(history.rowCount(), 1)
        self.assertEqual(history.item(0, 5).text(), "84 min")
        history.selectRow(0)
        self.click(self.find_button(reopened, "Open flight"))
        captions = [item.text() for item in reopened.logbook.detail.findChildren(self.label_type)]
        self.assertIn("Duration: 80 → 84", captions)
        self.assertEqual([revision.fields.minutes for revision in reopened.logbook.selected.revisions], [80, 84])
        self.assertEqual([metric.text() for metric in reopened.logbook.dashboard.metrics], ["1", "1h 24m", "—", "—", "—"])
        self.assertEqual(len(list((self.data / "flights").glob("flights-*.json"))), 1)
        self.assertTrue(reopened.close())

    def test_automatic_flight_reappears_in_history_with_read_only_landing_rate(self):
        self.pick_folder()
        replay = RecorderReplay(self.window.logbook.store)
        self.addCleanup(replay.close)
        flight = replay.approach_and_park()
        self.assertTrue(self.window.close())

        reopened = self.open_window()
        self.click(reopened.section_buttons["Logbook"])
        self.click(reopened.logbook.nav["Flight History"])
        history = reopened.logbook.history[1]
        self.assertEqual(history.rowCount(), 1)
        self.assertEqual(history.item(0, 1).text(), "OFFLINE 747")
        self.assertEqual(history.item(0, 6).text(), "-180 fpm")
        history.selectRow(0)
        self.click(self.find_button(reopened, "Open flight"))
        captions = [item.text() for item in reopened.logbook.detail.findChildren(self.label_type)]
        self.assertIn("Automatically recorded flight (simulated)", captions)
        self.assertIn("Landing rate   -180 fpm", captions)
        self.click(self.find_button(reopened, "Edit flight"))
        reopened.logbook.editor.text_fields["arrival"].setText("YSSY")
        self.click(reopened.logbook.editor.save_button)
        self.assertEqual(reopened.logbook.selected.id, flight.id)
        self.assertEqual(reopened.logbook.selected.recording, flight.recording)
        self.assertTrue(reopened.close())

    def test_recorder_window_recovers_saved_active_flight_and_can_keep_it_partial(self):
        provider = FlightReplay()
        core = Core(provider)
        self.addCleanup(core.disconnect)
        core.connect()
        self.assertTrue(self.window.close())
        self.window = self.open_window(connection_reader=core.register_consumer(),
                                       register_recorder=core.register_consumer)
        self.pick_folder()
        self.click(self.window.logging_status)
        self.assertFalse(self.window.auto_logging.isChecked())
        self.assertEqual(self.window.logging_status.text(), "Flight Logging · Inactive")
        self.click(self.window.auto_logging)
        self.assertTrue(self.window._recorder_timer.isActive())
        core.pump()
        for second, speed, brake, status in (
            (0, 0, True, "Ready"), (1, 3, False, "Ready"), (2, 4, False, "Active"),
        ):
            provider.frame(second, speed=speed, brake=brake)
            core.pump()
            self.wait(300)
            self.assertEqual(self.window.logging_status.text(), f"Flight Logging · {status}")
        store = self.window.logbook.store
        flight_id = self.window._recorder.flight_id

        with patch.object(self.window, "_confirm_recording_close", return_value=False):
            self.assertFalse(self.window.close())
        self.assertTrue(self.window._recorder_timer.isActive())
        with (patch.object(self.window, "_confirm_recording_close", return_value=True),
              patch.object(store, "save_recording", side_effect=OSError("Disk busy")),
              patch("bbc_efb.shell.QMessageBox.warning") as warning):
            self.assertFalse(self.window.close())
            warning.assert_called_once()
        with patch.object(self.window, "_confirm_recording_close", return_value=True):
            self.assertTrue(self.window.close())
        core.disconnect()
        core.connect()

        reopened = self.open_window(connection_reader=core.register_consumer(),
                                    register_recorder=core.register_consumer)
        self.assertEqual(reopened.logging_status.text(), "Flight Logging · Interrupted")
        self.click(reopened.logging_status)
        self.assertFalse(reopened.resume_flight.isVisible())
        core.pump()
        provider.frame(3, speed=5, brake=False)
        core.pump()
        self.wait(300)
        self.assertTrue(reopened.resume_flight.isVisible())
        self.click(reopened.keep_partial_flight)
        self.assertIsNone(store.get(flight_id).checkpoint)
        self.click(reopened.section_buttons["Logbook"])
        self.click(reopened.logbook.nav["Flight History"])
        history = reopened.logbook.history[1]
        self.assertEqual(history.item(0, 7).text(), "Interrupted")
        self.assertEqual(reopened.logbook.dashboard.metrics[0].text(), "0")
        history.selectRow(0)
        self.click(self.find_button(reopened, "Open flight"))
        captions = [item.text() for item in reopened.logbook.detail.findChildren(self.label_type)]
        self.assertIn("Touchdown   Unknown", captions)
        self.assertTrue(reopened.close())

    def test_manual_logging_buttons_follow_state_and_stop_requires_confirmation(self):
        provider = FlightReplay()
        core = Core(provider)
        self.addCleanup(core.disconnect)
        core.connect()
        self.assertTrue(self.window.close())
        self.window = self.open_window(connection_reader=core.register_consumer(),
                                       register_recorder=core.register_consumer)
        self.pick_folder()
        self.click(self.window.logging_status)
        self.click(self.window.auto_logging)
        self.assertFalse(self.window.start_logging.isVisible())
        core.pump()
        provider.frame(30, ground=False, speed=150, brake=False)
        core.pump()
        self.wait(300)
        self.click(self.window.start_logging)
        self.assertFalse(self.window.start_logging.isVisible())
        self.assertTrue(self.window.stop_logging.isVisible())
        with patch("bbc_efb.shell.QMessageBox.question", return_value=self.message_box.StandardButton.Cancel):
            self.click(self.window.stop_logging)
        self.assertTrue(self.window.stop_logging.isVisible())
        with patch("bbc_efb.shell.QMessageBox.question", return_value=self.message_box.StandardButton.Yes):
            self.click(self.window.stop_logging)
        self.assertFalse(self.window.stop_logging.isVisible())
        self.assertTrue(self.window.start_logging.isVisible())
        self.click(self.window.section_buttons["Logbook"])
        self.click(self.window.logbook.nav["Flight History"])
        history = self.window.logbook.history[1]
        self.assertEqual(history.item(0, 7).text(), "Stopped")
        history.selectRow(0)
        self.click(self.find_button(self.window, "Open flight"))
        captions = [item.text() for item in self.window.logbook.detail.findChildren(self.label_type)]
        self.assertIn("Takeoff   Unknown", captions)
        self.assertIn("Stopped by pilot", captions)
        self.assertTrue(self.window.close())

    def test_cancel_keeps_unsaved_edits_open_and_discard_allows_close(self):
        logbook = self.open_manual_editor()
        cancel = self.message_box.StandardButton.Cancel
        with patch("bbc_efb.logbook.QMessageBox.question", return_value=cancel):
            self.click(self.window.section_buttons["Dispatch"])
            self.assertIs(self.window.sections.currentWidget(), logbook)
            self.assertTrue(self.window.section_buttons["Logbook"].isChecked())
            self.assertFalse(self.window.close())
        self.assertEqual(logbook.store.listing(), ([], []))
        with patch("bbc_efb.logbook.QMessageBox.question", return_value=self.message_box.StandardButton.Discard):
            self.assertTrue(self.window.close())

    def test_time_display_preference_survives_reopen(self):
        self.click(self.window.section_buttons["Settings"])
        self.window.time_zone.setCurrentIndex(1)
        self.assertTrue(self.window.logbook.use_local_time)
        self.assertTrue(self.window.close())
        reopened = self.open_window()
        self.assertEqual(reopened.time_zone.currentData(), "local")
        self.assertTrue(reopened.logbook.use_local_time)

    def test_title_bar_maximises_restores_and_closes(self):
        self.click(self.window.maximise)
        self.assertTrue(self.window.isMaximized())
        self.assertEqual(self.window.maximise.accessibleName(), "Restore")
        self.assertFalse(self.window.resize_grip.isVisible())
        self.click(self.window.maximise)
        self.assertFalse(self.window.isMaximized())
        self.assertTrue(self.window.resize_grip.isVisible())
        self.click(self.find_button(self.window, "×"))
        self.assertFalse(self.window.isVisible())
