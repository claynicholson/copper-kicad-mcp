"""Where a linked account's token lives: the operating system's credential store.

    get_store() -> SecretStore        chosen once per process
    store.get(name) -> str | None
    store.set(name, value)
    store.delete(name) -> bool
    store.names() -> list[str]

Backends
    WinCred       Windows Credential Manager (advapi32 through ctypes), target ``Copper:<name>``
    MacKeychain   macOS login keychain through /usr/bin/security          (written to the documented
                  CLI behaviour; NOT verified on a Mac in this build)
    SecretTool    libsecret through ``secret-tool``                         (same: NOT verified on Linux)
    KeyFile       ``$COPPER_HOME/credentials.bin`` — obfuscation + tamper detection only, the key lies
                  next to the data. Used in the test sandbox, or when the user explicitly opts in.
    MemoryStore   tests

Sandbox rule: with ``COPPER_CONFIG_HOME_OVERRIDE`` set, :func:`get_store` returns the key-file store
inside the sandbox and never loads an OS backend, so tests cannot touch a real keychain.

Copper never falls back silently to a weaker store: with no OS store available it raises
:class:`StoreError` explaining the alternatives.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
_NO_WINDOW = 0x08000000 if IS_WIN else 0
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,40}")
PREFIX = "Copper:"
MAX_BLOB = 2560            # CRED_MAX_CREDENTIAL_BLOB_SIZE


class StoreError(Exception):
    """The credential store cannot be used (locked, missing, refused)."""


def _check_name(name: str) -> str:
    if not _NAME.fullmatch(name or ""):
        raise ValueError(f"bad credential name {name!r}")
    return name


class SecretStore:
    id = "base"
    name = "credential store"
    strength = "os"            # os | file-permissions | memory

    def available(self) -> tuple[bool, str]:
        return True, ""

    def get(self, name: str) -> str | None:
        raise NotImplementedError

    def set(self, name: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> bool:
        raise NotImplementedError

    def names(self) -> list[str]:
        raise NotImplementedError


# ── memory ────────────────────────────────────────────────────────────────
class MemoryStore(SecretStore):
    id, name, strength = "memory", "memory (tests)", "memory"

    def __init__(self):
        self._d: dict[str, str] = {}

    def get(self, name):
        return self._d.get(_check_name(name))

    def set(self, name, value):
        self._d[_check_name(name)] = value

    def delete(self, name):
        return self._d.pop(_check_name(name), None) is not None

    def names(self):
        return sorted(self._d)


# ── Windows Credential Manager ────────────────────────────────────────────
class WinCred(SecretStore):
    id, name, strength = "wincred", "Windows Credential Manager", "os"
    _TYPE_GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2
    _NOT_FOUND = 1168

    def __init__(self, prefix: str = PREFIX):
        self.prefix = prefix
        self._api = None

    def _load(self):
        if self._api is not None:
            return self._api
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                        ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]

        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        PCRED = ctypes.POINTER(CREDENTIAL)
        adv.CredWriteW.argtypes = [PCRED, wintypes.DWORD]
        adv.CredWriteW.restype = wintypes.BOOL
        adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCRED)]
        adv.CredReadW.restype = wintypes.BOOL
        adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        adv.CredDeleteW.restype = wintypes.BOOL
        adv.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       ctypes.POINTER(ctypes.POINTER(PCRED))]
        adv.CredEnumerateW.restype = wintypes.BOOL
        adv.CredFree.argtypes = [ctypes.c_void_p]
        adv.CredFree.restype = None
        self._api = (ctypes, adv, CREDENTIAL, PCRED)
        return self._api

    def available(self):
        if not IS_WIN:
            return False, "not Windows"
        try:
            self._load()
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def _err(self, what: str):
        ctypes = self._load()[0]
        code = ctypes.get_last_error()
        return StoreError(f"Windows Credential Manager refused to {what} (error {code}: {ctypes.FormatError(code).strip()})")

    def get(self, name):
        ctypes, adv, _C, PCRED = self._load()
        p = PCRED()
        if not adv.CredReadW(self.prefix + _check_name(name), self._TYPE_GENERIC, 0, ctypes.byref(p)):
            if ctypes.get_last_error() == self._NOT_FOUND:
                return None
            raise self._err("read the entry")
        try:
            c = p.contents
            return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
        finally:
            adv.CredFree(p)

    def set(self, name, value):
        ctypes, adv, CREDENTIAL, _P = self._load()
        blob = value.encode("utf-8")
        if len(blob) > MAX_BLOB:
            raise StoreError(f"value is {len(blob)} bytes; Windows Credential Manager holds at most {MAX_BLOB}")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else (ctypes.c_ubyte * 1)()
        c = CREDENTIAL()
        c.Type = self._TYPE_GENERIC
        c.TargetName = self.prefix + _check_name(name)
        c.Comment = "Copper for KiCad linked account"
        c.CredentialBlobSize = len(blob)
        c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        c.Persist = self._PERSIST_LOCAL_MACHINE
        c.UserName = "copper"
        if not adv.CredWriteW(ctypes.byref(c), 0):
            raise self._err("store the entry")

    def delete(self, name):
        ctypes, adv, _C, _P = self._load()
        if adv.CredDeleteW(self.prefix + _check_name(name), self._TYPE_GENERIC, 0):
            return True
        if ctypes.get_last_error() == self._NOT_FOUND:
            return False
        raise self._err("delete the entry")

    def names(self):
        ctypes, adv, _C, PCRED = self._load()
        from ctypes import wintypes
        n = wintypes.DWORD()
        arr = ctypes.POINTER(PCRED)()
        if not adv.CredEnumerateW(self.prefix + "*", 0, ctypes.byref(n), ctypes.byref(arr)):
            if ctypes.get_last_error() == self._NOT_FOUND:
                return []
            raise self._err("list entries")
        try:
            return sorted(arr[i].contents.TargetName[len(self.prefix):] for i in range(n.value))
        finally:
            adv.CredFree(arr)


# ── macOS Keychain (unverified on a Mac) ──────────────────────────────────
class MacKeychain(SecretStore):
    id, name, strength = "keychain", "macOS Keychain", "os"
    SERVICE = "Copper for KiCad"
    BIN = "/usr/bin/security"

    def available(self):
        if not IS_MAC:
            return False, "not macOS"
        return (True, "") if os.path.exists(self.BIN) else (False, f"{self.BIN} is missing")

    def _run(self, args, stdin: str | None = None):
        try:
            return subprocess.run([self.BIN, *args], input=stdin, capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise StoreError(f"the macOS Keychain did not answer ({e}); unlock the login keychain and try again") from None

    def _fail(self, r, what):
        if r.returncode in (36, 51):
            return StoreError("the login keychain is locked or cannot prompt here (SSH session?); unlock it and try again")
        return StoreError(f"macOS Keychain could not {what} (security exit {r.returncode})")

    def get(self, name):
        r = self._run(["find-generic-password", "-s", self.SERVICE, "-a", _check_name(name), "-w"])
        if r.returncode == 44:
            return None
        if r.returncode != 0:
            raise self._fail(r, "read the entry")
        out = r.stdout.rstrip("\n")
        try:                      # stored as hex so the value never needs shell quoting
            return bytes.fromhex(out).decode("utf-8")
        except ValueError:
            return out

    def set(self, name, value):
        # `security -i` reads the command from stdin, so the value is never in argv (process list).
        cmd = (f'add-generic-password -U -s "{self.SERVICE}" -a {_check_name(name)} '
               f'-l "Copper: {name}" -X {value.encode("utf-8").hex()}\n')
        r = self._run(["-i"], stdin=cmd)
        if r.returncode != 0:
            raise self._fail(r, "store the entry")

    def delete(self, name):
        r = self._run(["delete-generic-password", "-s", self.SERVICE, "-a", _check_name(name)])
        if r.returncode == 44:
            return False
        if r.returncode != 0:
            raise self._fail(r, "delete the entry")
        return True

    def names(self):
        # the keychain has no cheap "list by service"; the non-secret index (links.json) is the list
        return []


# ── libsecret (unverified on Linux) ───────────────────────────────────────
class SecretTool(SecretStore):
    id, name, strength = "libsecret", "system keyring (libsecret)", "os"
    SERVICE = "copper-kicad"

    def available(self):
        if IS_WIN or IS_MAC:
            return False, "not Linux"
        if not shutil.which("secret-tool"):
            return False, "secret-tool is not installed (package libsecret-tools)"
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return False, "no D-Bus session (headless login); a keyring cannot be reached"
        return True, ""

    def _run(self, args, stdin: str | None = None):
        try:
            return subprocess.run(["secret-tool", *args], input=stdin, capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise StoreError(f"the system keyring did not answer ({e}); unlock your keyring and try again") from None

    def get(self, name):
        r = self._run(["lookup", "service", self.SERVICE, "account", _check_name(name)])
        if r.returncode != 0 or r.stdout == "":
            return None
        return r.stdout.rstrip("\n")

    def set(self, name, value):
        r = self._run(["store", "--label", f"Copper: {name}", "service", self.SERVICE, "account", _check_name(name)],
                      stdin=value)
        if r.returncode != 0:
            raise StoreError("the system keyring refused to store the entry; unlock your keyring and try again")

    def delete(self, name):
        had = self.get(name) is not None
        self._run(["clear", "service", self.SERVICE, "account", _check_name(name)])
        return had

    def names(self):
        r = self._run(["search", "--all", "service", self.SERVICE])
        return sorted(set(re.findall(r"attribute\.account = (\S+)", r.stdout + r.stderr)))


# ── key file (sandbox / explicit opt-in) ──────────────────────────────────
class KeyFile(SecretStore):
    """Obfuscation plus tamper detection, NOT protection: the key lies next to the data.

    Body = nonce(24) ‖ ciphertext ‖ HMAC-SHA256; keystream = SHAKE-256(key ‖ nonce)."""
    id, name, strength = "keyfile", "key file (protected by file permissions only)", "file-permissions"

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.data = self.dir / "credentials.bin"
        self.keyf = self.dir / "credentials.key"

    def _key(self, create=False) -> bytes | None:
        try:
            k = self.keyf.read_bytes()
            if len(k) == 32:
                return k
            raise StoreError(f"{self.keyf} is damaged")
        except FileNotFoundError:
            if not create:
                if self.data.exists():
                    raise StoreError(f"{self.keyf} is missing; the stored accounts cannot be read. Link again.") from None
                return None
        k = secrets.token_bytes(32)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._write(self.keyf, k)
        return k

    @staticmethod
    def _write(p: Path, b: bytes):
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(b)
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    def _load(self) -> dict:
        key = self._key()
        if key is None or not self.data.exists():
            return {}
        raw = self.data.read_bytes()
        if len(raw) < 24 + 32:
            raise StoreError(f"{self.data} is damaged")
        nonce, ct, mac = raw[:24], raw[24:-32], raw[-32:]
        if not hmac.compare_digest(mac, hmac.new(key, nonce + ct, hashlib.sha256).digest()):
            raise StoreError(f"{self.data} was modified or belongs to another key file; link again")
        ks = hashlib.shake_256(key + nonce).digest(len(ct))
        return json.loads(bytes(a ^ b for a, b in zip(ct, ks)).decode("utf-8"))

    def _save(self, d: dict):
        key = self._key(create=True)
        pt = json.dumps(d, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_bytes(24)
        ct = bytes(a ^ b for a, b in zip(pt, hashlib.shake_256(key + nonce).digest(len(pt))))
        self._write(self.data, nonce + ct + hmac.new(key, nonce + ct, hashlib.sha256).digest())

    def get(self, name):
        return self._load().get(_check_name(name))

    def set(self, name, value):
        d = self._load()
        d[_check_name(name)] = value
        self._save(d)

    def delete(self, name):
        d = self._load()
        if _check_name(name) not in d:
            return False
        del d[name]
        self._save(d)
        return True

    def names(self):
        return sorted(self._load())


# ── selection ─────────────────────────────────────────────────────────────
_cached: tuple[tuple, SecretStore] | None = None


def get_store(allow_weak: bool = False) -> SecretStore:
    """The store for this process. Raises :class:`StoreError` when no OS store can be used and the
    user has not opted into the key file."""
    global _cached
    sandbox = os.environ.get("COPPER_CONFIG_HOME_OVERRIDE") or ""
    choice = (os.environ.get("COPPER_SECRET_BACKEND") or "").lower()
    key = (sandbox, choice, allow_weak)
    if _cached and _cached[0] == key:
        return _cached[1]
    from . import copper_home
    if sandbox:
        st: SecretStore = MemoryStore() if choice == "memory" else KeyFile(copper_home())
    elif choice == "memory":
        st = MemoryStore()
    elif choice == "keyfile" or (allow_weak and choice == ""):
        st = _os_store() or KeyFile(copper_home())
    else:
        st = _os_store()
        if st is None:
            if (copper_home() / "credentials.key").exists():
                st = KeyFile(copper_home())       # the user opted in earlier
            else:
                raise StoreError(_no_store_reason())
    _cached = (key, st)
    return st


def _candidates() -> list[SecretStore]:
    return [WinCred()] if IS_WIN else [MacKeychain()] if IS_MAC else [SecretTool()]


def _os_store() -> SecretStore | None:
    for st in _candidates():
        if st.available()[0]:
            return st
    return None


def _no_store_reason() -> str:
    why = "; ".join(f"{st.name}: {st.available()[1]}" for st in _candidates())
    return (f"no operating-system credential store is available ({why}). Copper does not keep tokens in "
            "plain files. Use the GitHub CLI instead (`gh auth login`, then `copper link github`), or pass "
            "--allow-weak-store once to keep the token in a file protected by file permissions only.")


def describe() -> dict:
    """Which store is in use and why (never reads a secret)."""
    try:
        st = get_store()
        return {"id": st.id, "name": st.name, "strength": st.strength, "available": True}
    except StoreError as e:
        return {"id": None, "name": "none", "strength": "none", "available": False, "detail": str(e)}


def reset_cache() -> None:
    global _cached
    _cached = None
