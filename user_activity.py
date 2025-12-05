# user_activity.py
# Windows helper: GetLastInputInfo -> seconds since last input (mouse/keyboard)
# plus is_workstation_locked() using OpenDesktop / SwitchDesktop
from ctypes import Structure, sizeof, byref
import ctypes
import sys
import time
from ctypes import wintypes
import traceback


class LASTINPUTINFO(Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def _is_windows() -> bool:
    return sys.platform == "win32"


def get_last_input_millis_win() -> int:
    """
    Windows implementation.
    Returns milliseconds since last user input (difference between GetTickCount64 and LASTINPUTINFO.dwTime).
    May raise OSError on API failure.
    """
    if not _is_windows():
        raise OSError("get_last_input_millis_win: not running on Windows")

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    li = LASTINPUTINFO()
    li.cbSize = sizeof(LASTINPUTINFO)

    ok = user32.GetLastInputInfo(byref(li))
    if not ok:
        raise OSError("GetLastInputInfo failed (returned 0)")

    # Try GetTickCount64 first (more robust)
    tick64 = None
    try:
        if hasattr(kernel32, "GetTickCount64"):
            tick64 = int(kernel32.GetTickCount64())
        else:
            tick64 = int(kernel32.GetTickCount())
    except Exception:
        tick64 = int(kernel32.GetTickCount())

    last = int(li.dwTime) & 0xFFFFFFFF

    # compute diff with wrap-around handling
    if tick64 >= last:
        diff = tick64 - last
    else:
        diff = (0x100000000 + tick64) - last

    return int(diff)


def get_last_input_seconds() -> float:
    """
    Returns seconds since last user input (mouse/keyboard).
    On Windows uses GetLastInputInfo. On other platforms returns 0.0 as fallback.
    """
    if _is_windows():
        try:
            ms = get_last_input_millis_win()
            return ms / 1000.0
        except Exception as e:
            tb = traceback.format_exc()
            raise OSError(f"Windows GetLastInputInfo error: {e}\n{tb}")
    else:
        return 0.0

# ------------------------
# Workstation locked detection
# ------------------------


def is_workstation_locked() -> bool:
    """
    Attempt to detect if the workstation is locked (Win32):
    Uses OpenDesktop("Default", ...) + SwitchDesktop.
    Returns True if locked (i.e. SwitchDesktop returns False).
    If detection is not supported or fails, returns False (safe default).
    """
    if not _is_windows():
        return False
    try:
        user32 = ctypes.windll.user32
        OpenDesktopW = user32.OpenDesktopW
        OpenDesktopW.argtypes = [wintypes.LPCWSTR,
                                 wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        OpenDesktopW.restype = wintypes.HANDLE

        SwitchDesktop = user32.SwitchDesktop
        SwitchDesktop.argtypes = [wintypes.HANDLE]
        SwitchDesktop.restype = wintypes.BOOL

        CloseDesktop = user32.CloseDesktop
        CloseDesktop.argtypes = [wintypes.HANDLE]
        CloseDesktop.restype = wintypes.BOOL

        DESKTOP_SWITCHDESKTOP = 0x0100

        hDesk = OpenDesktopW("Default", 0, False, DESKTOP_SWITCHDESKTOP)
        if not hDesk:
            # couldn't open desktop (possible permissions) -> fallback: False
            return False
        try:
            can = SwitchDesktop(hDesk)
            # If SwitchDesktop returns False, typically desktop is locked (or we lack rights)
            return not bool(can)
        finally:
            try:
                CloseDesktop(hDesk)
            except Exception:
                pass
    except Exception:
        # In case of any failure, assume not locked (safe fallback).
        return False


# CLI diagnostic
if __name__ == "__main__":
    print("user_activity.py diagnostic run")
    print("Platform:", sys.platform)
    print("Python:", sys.version.splitlines()[0])
    print("Time (monotonic):", time.monotonic())
    if _is_windows():
        try:
            try:
                ms_last = get_last_input_millis_win()
                print("Milliseconds since last input (computed):", ms_last)
                print("Seconds since last input:", ms_last / 1000.0)
            except Exception as e:
                print("get_last_input_millis_win() raised:", e)
                traceback.print_exc()
            # raw LASTINPUTINFO.dwTime and tick values
            try:
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                li = LASTINPUTINFO()
                li.cbSize = sizeof(LASTINPUTINFO)
                ok = user32.GetLastInputInfo(byref(li))
                if not ok:
                    print("GetLastInputInfo returned 0 (failure).")
                else:
                    raw_dw = int(li.dwTime) & 0xFFFFFFFF
                    print("LASTINPUTINFO.dwTime (raw DWORD):", raw_dw)
                try:
                    tick64 = int(kernel32.GetTickCount64())
                    print("GetTickCount64:", tick64)
                except AttributeError:
                    tick32 = int(kernel32.GetTickCount())
                    print("GetTickCount (32-bit):", tick32)
            except Exception as e:
                print("Could not fetch raw debug values:", e)
                traceback.print_exc()
            try:
                locked = is_workstation_locked()
                print("is_workstation_locked ->", locked)
            except Exception as e:
                print("is_workstation_locked raised:", e)
                traceback.print_exc()
        except Exception as e:
            print("Error during Windows diagnostics:", e)
            traceback.print_exc()
    else:
        print("Not running on Windows. get_last_input_seconds() returns 0.0 as fallback.")
    print("Done.")
