"""Debugger boundary tests use a fake Win32 event source, never a user process."""
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chatlog_assistant.sources import windows_debug as debug


class HardwareCaptureTests(unittest.TestCase):
    def run_capture(self, callback):
        kernel = SimpleNamespace(**{name: Mock(return_value=True) for name in (
            "DebugActiveProcess", "DebugActiveProcessStop", "DebugSetProcessKillOnExit",
            "ContinueDebugEvent", "CloseHandle",
        )})
        events = iter((debug.DEBUG_EVENT_CREATE_THREAD, debug.DEBUG_EVENT_EXCEPTION))

        def wait(pointer, _timeout):
            event = pointer._obj
            event.dwProcessId, event.dwThreadId = 7, 9
            event.dwDebugEventCode = next(events)
            event.u.Exception.ExceptionRecord.ExceptionCode = debug.EXCEPTION_SINGLE_STEP
            event.u.Exception.ExceptionRecord.ExceptionAddress = 0x123400
            return True

        kernel.WaitForDebugEvent = Mock(side_effect=wait)
        self.kernel = kernel
        with (
            patch.object(debug.os, "name", "nt"),
            patch.object(ctypes, "WinDLL", return_value=kernel, create=True),
            patch.object(debug, "enable_debug_privilege", return_value=True),
            patch.object(debug, "list_threads", return_value=[SimpleNamespace(tid=9)]),
            patch.object(debug, "set_execute_breakpoints", return_value=True),
            patch.object(debug, "clear_execute_breakpoints", return_value=True),
            patch.object(debug, "read_x86_hit", return_value=debug.CaptureHit(7, 0x123400, (b"sample",), None, None)),
            patch.object(debug, "_set_wow64_eip_and_trap", return_value=True),
            patch.object(debug, "_resume_hardware_breakpoint", return_value=True),
            patch.object(debug, "install_software_breakpoints", side_effect=AssertionError("software code patch forbidden")),
            patch.object(debug, "write_process_memory", side_effect=AssertionError("code write forbidden")),
        ):
            return debug.attach_and_wait([7], lambda _pid: [0x123400], callback, timeout=2, wow64=True)

    def test_hardware_capture_never_patches_target_code(self):
        result = self.run_capture(lambda _hit: True)
        self.assertEqual(result["hits"], 1)
        self.assertEqual(result["software_breakpoints"], 0)
        self.assertTrue(self.kernel.DebugActiveProcessStop.called)

    def test_callback_failure_resumes_pending_event_and_detaches(self):
        def fail(_hit):
            raise ValueError("verification failed")

        with self.assertRaisesRegex(ValueError, "verification failed"):
            self.run_capture(fail)
        continued = self.kernel.ContinueDebugEvent.call_args_list
        self.assertEqual(len(continued), 2)
        self.assertTrue(self.kernel.DebugActiveProcessStop.called)


if __name__ == "__main__":
    unittest.main()
