import io
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bbc_efb import logging_setup


class LoggingSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fallback = self.root / "per-user" / "logs"
        patcher = mock.patch.object(logging_setup, "user_log_directory", return_value=self.fallback)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stderr = io.StringIO()
        stderr_patch = mock.patch.object(sys, "stderr", self.stderr)
        stderr_patch.start()
        self.addCleanup(stderr_patch.stop)
        # Runs before the patches are undone and the folder is removed, so
        # Windows can delete the closed log files.
        self.addCleanup(logging_setup.reset_logging)

    def read_log(self, config):
        for handler in logging.getLogger().handlers:
            handler.flush()
        return config.log_file.read_text(encoding="utf-8")

    def test_writes_a_rotating_utf8_log_in_the_data_folder(self):
        config = logging_setup.configure_logging(self.root / "data", "INFO")
        self.assertEqual(config.log_file, self.root / "data" / "logs" / "bbc-efb.log")
        self.assertIsNone(config.fallback_reason)
        logging.getLogger("bbc_core.core").info("Session ended: QUIT received from MSFS (Zürich test)")
        logging.getLogger("bbc_core.core").debug("hot path detail")
        text = self.read_log(config)
        self.assertIn("INFO [MainThread] bbc_core.core: Session ended: QUIT received from MSFS (Zürich test)", text)
        self.assertNotIn("hot path detail", text)
        self.assertRegex(text, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z ")
        handler = next(h for h in logging.getLogger().handlers
                       if isinstance(h, logging_setup.RotatingFileHandler))
        self.assertEqual((handler.maxBytes, handler.backupCount),
                         (logging_setup.MAX_LOG_BYTES, logging_setup.LOG_BACKUP_COUNT))
        self.assertTrue((self.root / "data" / "logs" / "fault.log").exists())

    def test_without_a_data_folder_uses_the_per_user_folder(self):
        config = logging_setup.configure_logging(None, logging.WARNING)
        self.assertEqual(config.log_file, self.fallback / "bbc-efb.log")
        self.assertIsNone(config.fallback_reason)

    def test_unusable_data_folder_falls_back_and_says_why(self):
        blocker = self.root / "not-a-folder"
        blocker.write_text("file in the way", encoding="utf-8")
        config = logging_setup.configure_logging(blocker, "INFO")
        self.assertEqual(config.log_file, self.fallback / "bbc-efb.log")
        self.assertIn("could not be used for logs", config.fallback_reason)
        self.assertIn("instead of the data folder", self.read_log(config))

    def test_console_handler_only_when_stderr_exists(self):
        config = logging_setup.configure_logging(self.root, "INFO")
        self.assertTrue(config.console)
        self.assertIn("Logging started", self.stderr.getvalue())
        logging_setup.reset_logging()
        with mock.patch.object(sys, "stderr", None):
            config = logging_setup.configure_logging(self.root, "INFO")
            self.assertFalse(config.console)
            stream_handlers = [h for h in logging.getLogger().handlers
                               if type(h) is logging.StreamHandler]
            self.assertEqual(stream_handlers, [])
            logging_setup.reset_logging()

    def test_reconfiguring_does_not_duplicate_handlers(self):
        before = list(logging.getLogger().handlers)
        logging_setup.configure_logging(self.root / "one", "INFO")
        config = logging_setup.configure_logging(self.root / "two", "INFO")
        added = [h for h in logging.getLogger().handlers if h not in before]
        self.assertEqual(len(added), 2)
        logging.getLogger("bbc_efb.test").warning("only once")
        self.assertEqual(self.read_log(config).count("only once"), 1)
        self.assertNotIn("only once", (self.root / "one" / "logs" / "bbc-efb.log").read_text(encoding="utf-8"))

    def test_uncaught_exceptions_are_logged_from_main_thread_threads_and_callbacks(self):
        original_hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)
        config = logging_setup.configure_logging(self.root, "INFO")
        try:
            raise KeyError("schema")
        except KeyError:
            sys.excepthook(*sys.exc_info())

        def fail():
            raise RuntimeError("worker failed")

        worker = threading.Thread(target=fail, name="core-pump")
        worker.start()
        worker.join()

        class Unraisable:
            exc_type = ValueError
            exc_value = ValueError("callback failed")
            exc_traceback = None
            err_msg = "Exception ignored on calling ctypes callback function"
            object = "dispatch"

        sys.unraisablehook(Unraisable())
        text = self.read_log(config)
        self.assertIn("CRITICAL [MainThread] bbc_efb.uncaught: Uncaught exception", text)
        self.assertIn("KeyError: 'schema'", text)
        self.assertIn("Uncaught exception in thread core-pump.", text)
        self.assertIn("RuntimeError: worker failed", text)
        self.assertIn("Exception ignored on calling ctypes callback function: 'dispatch'", text)
        self.assertIn("ValueError: callback failed", text)
        logging_setup.reset_logging()
        self.assertEqual((sys.excepthook, threading.excepthook, sys.unraisablehook), original_hooks)

    def test_invalid_level_is_rejected_in_plain_words_and_installs_nothing(self):
        handlers = list(logging.getLogger().handlers)
        hook = sys.excepthook
        with self.assertRaisesRegex(ValueError, "Unknown log level"):
            logging_setup.configure_logging(self.root, "LOUD")
        self.assertEqual(logging.getLogger().handlers, handlers)
        self.assertIs(sys.excepthook, hook)

    def test_bootstrap_then_relocate_keeps_level_lines_and_hooks(self):
        original_hook = sys.excepthook
        first = logging_setup.configure_logging(None, "WARNING")
        self.assertEqual(first.log_file, self.fallback / "bbc-efb.log")
        logging.getLogger("bbc_efb.app").warning("before the data folder is known")
        moved = logging_setup.relocate_logging(self.root / "data")
        self.assertEqual(moved.log_file, self.root / "data" / "logs" / "bbc-efb.log")
        self.assertEqual(moved.level, logging.WARNING)
        self.assertEqual(moved.data_dir, self.root / "data")
        self.assertEqual(logging_setup.current_configuration(), moved)
        logging.getLogger("bbc_efb.app").warning("after the move")
        self.assertIn("before the data folder is known", (self.fallback / "bbc-efb.log").read_text(encoding="utf-8"))
        text = self.read_log(moved)
        self.assertIn("after the move", text)
        self.assertNotIn("before the data folder is known", text)
        files = [h for h in logging.getLogger().handlers if isinstance(h, logging_setup.RotatingFileHandler)]
        self.assertEqual(len(files), 1)
        self.assertIsNot(sys.excepthook, original_hook)
        logging_setup.reset_logging()
        self.assertIs(sys.excepthook, original_hook)
        self.assertIsNone(logging_setup.current_configuration())

    def test_relocate_without_configuration_does_nothing(self):
        self.assertIsNone(logging_setup.relocate_logging(self.root / "data"))
        self.assertFalse((self.root / "data").exists())

    def test_release_folder_closes_every_file_inside_it(self):
        data = self.root / "data"
        logging_setup.configure_logging(data, "INFO")
        self.assertEqual(len(logging_setup.open_files_inside(data)), 2)  # log and fault.log
        self.assertIsNone(logging_setup.release_folder(self.root / "elsewhere"))
        moved = logging_setup.release_folder(data)
        self.assertEqual(moved.log_file, self.fallback / "bbc-efb.log")
        self.assertEqual(logging_setup.open_files_inside(data), ())
        for handler in logging.getLogger().handlers:
            self.assertFalse(str(getattr(handler, "baseFilename", "")).startswith(str(data)))
        # Nothing in the old folder is held open, so a restore can rename it.
        data.rename(self.root / "data.pre-restore")
        self.assertTrue((self.root / "data.pre-restore" / "logs" / "bbc-efb.log").exists())

    def test_per_user_folder_follows_platform_conventions(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "Local")}):
            self.assertEqual(_real_user_log_directory(), self.root / "Local" / "BBC EFB" / "logs")
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict("os.environ", {"XDG_STATE_HOME": str(self.root / "state")}):
            self.assertEqual(_real_user_log_directory(), self.root / "state" / "bbc-efb" / "logs")


_real_user_log_directory = logging_setup.user_log_directory


if __name__ == "__main__":
    unittest.main()
