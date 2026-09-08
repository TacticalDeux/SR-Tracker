"""ctypes wrapper around sr_tracker.dll.

The DLL exposes a small C API (sr_tracker_install / uninstall / active /
get_cube / get_account_id) and writes parsed events into a named
event buffer that this wrapper maps
into the tracker process. has_event() / next_event() read framed JSON
records directly from the section - no log file, no polling through
the DLL.

Section layout (65536 bytes):
  offset 0   uint64  head_bytes         producer-monotonic
  offset 8   uint32  session_generation
  offset 12  uint8   install_state      0=uninit, 1=installing, 2=ready
  offset 16  65520 bytes ring           [u32 len LE][len bytes][NUL]
"""
from __future__ import annotations

import ctypes
import struct
import time
from pathlib import Path

from . import paths


_SHM_NAME = "Global\\SRTrackerEvents"
_SHM_CAP = 65520
_SHM_SIZE = 65536
_PAGE_READWRITE = 0x04
_FILE_MAP_ALL_ACCESS = 0x000F001F

# Security descriptor for the shared section. The tracker creates the
# section, but the UNELEVATED game has to open it for READ|WRITE - and
# the default DACL on an elevated creator denies that (the game logged
# "OpenFileMappingA failed err=0x5"), so no events ever flowed. This
# SDDL grants full control to admins/system, read+write to Everyone,
# and stamps a medium mandatory-integrity label so the
# medium-integrity game also passes the NoWriteUp check:
#   D:  GA for Builtin Admins + System, GRGW for Everyone
#   S:  medium label, no-write-up
_SHM_SDDL = (
    "D:(A;;GA;;;BA)(A;;GA;;;SY)(A;;GRGW;;;WD)"
    "S:(ML;;NW;;;ME)"
)


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_bool),
    ]


def _shm_security_attributes() -> _SECURITY_ATTRIBUTES:
    """Build a SECURITY_ATTRIBUTES carrying _SHM_SDDL.

    The returned struct borrows the converted descriptor; pass it to
    CreateFileMappingA, then hand it to _free_shm_security_attributes.
    """
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorA.argtypes = [
        ctypes.c_char_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
    ]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorA.restype = (
        ctypes.c_bool
    )
    sd = ctypes.c_void_p()
    rev = ctypes.c_uint32()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorA(
        _SHM_SDDL.encode("ascii"), 1, ctypes.byref(sd), ctypes.byref(rev),
    ):
        err = ctypes.get_last_error()
        raise OSError(
            "ConvertStringSecurityDescriptorToSecurityDescriptorA failed: "
            f"0x{err:x} (SDDL={_SHM_SDDL!r})"
        )
    return _SECURITY_ATTRIBUTES(
        ctypes.sizeof(_SECURITY_ATTRIBUTES), sd.value, False
    )


def _free_shm_security_attributes(sa: _SECURITY_ATTRIBUTES) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    k32.LocalFree(sa.lpSecurityDescriptor)
    sa.lpSecurityDescriptor = None


class TrackerDLL:
    def __init__(self, path: Path | None = None):
        self._path = path or paths.dll_path()
        if not self._path.exists():
            raise FileNotFoundError(f"DLL not found: {self._path}")
        lib = ctypes.CDLL(str(self._path))

        lib.sr_tracker_get_cube.restype = ctypes.c_int
        lib.sr_tracker_get_cube.argtypes = []

        lib.sr_tracker_get_account_id.restype = ctypes.c_int
        lib.sr_tracker_get_account_id.argtypes = []

        lib.sr_tracker_install.restype = ctypes.c_bool
        lib.sr_tracker_install.argtypes = []

        lib.sr_tracker_uninstall.restype = None
        lib.sr_tracker_uninstall.argtypes = []

        lib.sr_tracker_active.restype = ctypes.c_bool
        lib.sr_tracker_active.argtypes = []

        self._lib = lib

        # Map the event buffer the injected DLL uses. The
        # tracker runs elevated (built --uac-admin), so creating a
        # Global\ section is allowed. We try OpenFileMappingA first so
        # the tracker can attach to a section that the previous run's
        # injected DLL already made (e.g. after a crash); falling back
        # to CreateFileMappingA handles the cold-start case where the
        # DLL hasn't run yet. Either way the result is the same handle
        # to the same section.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingA.argtypes = [
            ctypes.c_uint32, ctypes.c_bool, ctypes.c_char_p,
        ]
        k32.OpenFileMappingA.restype = ctypes.c_void_p
        k32.CreateFileMappingA.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p,
        ]
        k32.CreateFileMappingA.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t,
        ]
        k32.MapViewOfFile.restype = ctypes.c_void_p

        self._h_shm = k32.OpenFileMappingA(
            _FILE_MAP_ALL_ACCESS, False, _SHM_NAME.encode("ascii"),
        )
        if not self._h_shm:
            # Section does not exist yet (game hasn't been injected, or
            # the previous tracker never created one). Try to create it
            # ourselves. This requires SeCreateGlobalPrivilege, which
            # only elevated tokens hold - hence the helpful error
            # message below if it fails.
            ctypes.set_last_error(0)
            # Explicit SA: the default DACL denies the unelevated game
            # (err=0x5 on its OpenFileMappingA). See _SHM_SDDL.
            sa = _shm_security_attributes()
            try:
                self._h_shm = k32.CreateFileMappingA(
                    ctypes.c_void_p(-1), ctypes.byref(sa),
                    _PAGE_READWRITE, 0, _SHM_SIZE,
                    _SHM_NAME.encode("ascii"),
                )
            finally:
                _free_shm_security_attributes(sa)
            if not self._h_shm:
                create_err = ctypes.get_last_error()
                raise OSError(
                    f"Could not open or create shared section "
                    f"{_SHM_NAME!r}. CreateFileMappingA failed with "
                    f"Win32 error 0x{create_err:x}. This usually means "
                    f"the tracker process is not running elevated: "
                    f"creating a Global\\ named section requires "
                    f"SeCreateGlobalPrivilege, which only elevated "
                    f"tokens hold. Re-launch the tracker from an "
                    f"elevated cmd (right-click cmd -> 'Run as "
                    f"administrator')."
                )
        view = k32.MapViewOfFile(
            self._h_shm, _FILE_MAP_ALL_ACCESS, 0, 0, _SHM_SIZE,
        )
        if not view:
            err = ctypes.get_last_error()
            raise OSError(f"MapViewOfFile failed: 0x{err:x}")
        self._shm = (ctypes.c_uint8 * _SHM_SIZE).from_address(view)

        # Per-process consumer state. _tail is private (not in the
        # shared section). _last_generation lets us reset on game
        # restarts without replaying old-session events. _resync holds
        # the reason for the last lossy resync (fell behind / corrupt /
        # torn record) for the consumer to surface in the Debug console
        # via pop_resync() — previously these were silent, which made
        # a stall indistinguishable from an idle game.
        self._tail = 0
        self._last_generation = 0
        self._resync: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def install(self) -> bool:
        return bool(self._lib.sr_tracker_install())

    def uninstall(self) -> None:
        self._lib.sr_tracker_uninstall()

    def active(self) -> bool:
        return bool(self._lib.sr_tracker_active())

    def wait_for_install(self, timeout_s: float = 3.0) -> bool:
        """Block until the injected DLL signals install_state == 2 (ready).
        Returns True if ready, False on timeout. Non-blocking consumer
        loops can also just call has_event() repeatedly; install_state is
        a one-shot signal and has_event returns False until events arrive.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._shm[12] == 2:
                return True
            time.sleep(0.05)
        return False

    def has_event(self) -> bool:
        head = self._read_head()
        self._maybe_reset_on_generation_bump()
        return head > self._tail

    def next_event(self) -> str | None:
        head = self._read_head()
        self._maybe_reset_on_generation_bump()
        if head <= self._tail:
            return None
        if head - self._tail > _SHM_CAP:
            # Consumer fell behind; drop the gap and resync.
            dropped = head - self._tail
            self._tail = head
            self._resync = (
                f"fell behind by {dropped} bytes (>64KB ring) — "
                f"skipped to producer head"
            )
            return None
        length_bytes = self._read_at(self._tail, 4)
        if len(length_bytes) != 4:
            self._tail = head
            self._resync = "short length read — resynced to producer head"
            return None
        (length,) = struct.unpack("<I", length_bytes)
        if length == 0 or length > _SHM_CAP - 4:
            # Corrupt length - resync to head.
            self._tail = head
            self._resync = f"corrupt length prefix {length} — resynced to head"
            return None
        payload = self._read_at(self._tail + 4, length)
        nul = self._read_at(self._tail + 4 + length, 1)
        if nul != b"\x00":
            # Producer's NUL sentinel missing -> record was torn. Resync.
            self._tail = head
            self._resync = "torn record (NUL sentinel missing) — resynced to head"
            return None
        self._tail += 4 + length + 1
        return payload.decode("utf-8", errors="replace")

    def pop_resync(self) -> str | None:
        """Take-and-clear the last lossy-resync reason, if any."""
        reason, self._resync = self._resync, None
        return reason

    def debug_state(self) -> dict:
        """Producer head / consumer tail / generation for stall diagnosis
        (used by the status bar idle readout). Never raises."""
        try:
            head = self._read_head()
        except Exception:
            head = -1
        try:
            gen = struct.unpack_from("<I", bytes(self._shm[8:12]))[0]
        except Exception:
            gen = -1
        return {"head": head, "tail": self._tail,
                "pending": head - self._tail if head >= 0 else -1,
                "generation": gen}

    def cube(self) -> int:
        return self._lib.sr_tracker_get_cube()

    def account_id(self) -> int:
        return self._lib.sr_tracker_get_account_id()

    # --- buffer helpers ---
    def _read_head(self) -> int:
        # Atomic 8-byte load at offset 0. ctypes does a single mov on
        # x64; the producer's release-store makes this safe.
        return struct.unpack_from("<Q", bytes(self._shm[:8]))[0]

    def _read_at(self, abs_off: int, n: int) -> bytes:
        """Read n bytes from the ring at absolute byte offset abs_off.
        The producer wrote at ((abs_off % SHM_CAP) + 16); reads do the
        same and handle wrap-around across the 65520-byte ring."""
        start = (abs_off % _SHM_CAP) + 16
        end = start + n
        if end <= _SHM_SIZE:
            return bytes(self._shm[start:end])
        first = _SHM_SIZE - start
        return bytes(self._shm[start:]) + bytes(
            self._shm[16:16 + (n - first)]
        )

    def _maybe_reset_on_generation_bump(self) -> None:
        gen = struct.unpack_from("<I", bytes(self._shm[8:12]))[0]
        if gen != self._last_generation:
            self._tail = 0
            self._last_generation = gen
