"""
StackNest — JAR security scanner.

Scans a plugin JAR's class bytecode for dangerous patterns *before* handing
it to the runtime test sandbox.  Does NOT fully parse the JVM class-file
format; instead it searches for dangerous constant-pool UTF-8 strings that
appear verbatim in the raw bytes of any class that references them.

Risk levels
-----------
  "safe"        — no concerning patterns found
  "suspicious"  — moderate-risk patterns present; allowed but logged
  "malicious"   — high-confidence malware indicators; blocked + user suspended
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_JAR_BYTES   = 20 * 1024 * 1024   # hard cap (also checked in server_test)
MAX_CLASS_BYTES =  4 * 1024 * 1024   # abnormally large class → flag immediately

# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

# HIGH — any match → malicious (blocked)
# Each entry: (bytecode_constant, human description)
_HIGH: list[tuple[bytes, str]] = [
    (b"java/lang/ProcessBuilder",  "ProcessBuilder (OS process spawning)"),
    (b"java/net/ServerSocket",      "ServerSocket (listening for remote connections)"),
    (b"java/net/URLClassLoader",    "URLClassLoader (loading remote bytecode)"),
    (b"sun/misc/Unsafe",            "sun.misc.Unsafe (unsafe memory access)"),
    (b"jdk/internal/misc/Unsafe",   "jdk.internal.misc.Unsafe (unsafe memory access)"),
]

# MEDIUM — suspicious when found; malicious when combined with Runtime.exec
_MEDIUM: list[tuple[bytes, str]] = [
    (b"java/net/Socket",            "raw TCP socket"),
    (b"java/net/URL",               "URL objects (possible remote fetch)"),
    (b"java/io/FileOutputStream",   "arbitrary file writes (FileOutputStream)"),
    (b"java/io/FileWriter",         "arbitrary file writes (FileWriter)"),
    (b"java/lang/reflect/Method",   "reflection (Method.invoke)"),
]

# Byte strings that indicate known-bad class naming conventions (case-insensitive)
_MALICIOUS_NAMES: list[bytes] = [
    b"backdoor", b"trojan", b"rootkit", b"keylogger", b"ransomware",
    b"shellcode", b"dropper", b"botnet", b"c2client", b"cncclient",
    b"malware", b"cryptominer", b"xmrig", b"reverseproxy",
    b"rce_payload", b"rcepayload",
]

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    risk_level:  str = "safe"          # "safe" | "suspicious" | "malicious"
    findings:    list[str] = field(default_factory=list)
    class_count: int = 0
    blocked:     bool = False

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_jar(jar_bytes: bytes) -> ScanResult:
    """
    Scan *jar_bytes* and return a ScanResult.

    Raises ValueError if the bytes do not represent a valid JAR/ZIP.
    """
    result = ScanResult()

    # -- Size check -----------------------------------------------------------
    if len(jar_bytes) > MAX_JAR_BYTES:
        result.risk_level = "malicious"
        result.findings.append(
            f"JAR exceeds the {MAX_JAR_BYTES // (1024 * 1024)} MB size limit."
        )
        result.blocked = True
        return result

    # -- ZIP magic bytes: PK\x03\x04 -----------------------------------------
    if len(jar_bytes) < 4 or jar_bytes[:2] != b"PK":
        raise ValueError("File is not a valid JAR/ZIP archive.")

    try:
        zf = zipfile.ZipFile(io.BytesIO(jar_bytes))
    except zipfile.BadZipFile:
        raise ValueError("File is not a valid JAR/ZIP archive.")

    high_hits:   list[str] = []
    medium_hits: list[str] = []
    name_hits:   list[str] = []
    runtime_exec = False

    try:
        for entry in zf.infolist():
            if not entry.filename.endswith(".class"):
                continue

            # Flag oversized class files immediately
            if entry.file_size > MAX_CLASS_BYTES:
                high_hits.append(
                    f"Abnormally large class file ({entry.file_size // 1024} KB): "
                    f"{entry.filename}"
                )
                continue

            try:
                data = zf.read(entry.filename)
            except Exception:
                continue

            result.class_count += 1

            # High-severity patterns
            for pattern, desc in _HIGH:
                if pattern in data:
                    high_hits.append(f"{desc} — {entry.filename}")

            # Runtime + exec = command execution (high)
            if b"java/lang/Runtime" in data and b"exec" in data:
                runtime_exec = True
                high_hits.append(
                    f"Runtime.exec() system command execution — {entry.filename}"
                )
            elif b"java/lang/Runtime" in data:
                medium_hits.append(f"java.lang.Runtime reference — {entry.filename}")

            # Medium-severity patterns
            for pattern, desc in _MEDIUM:
                if pattern in data:
                    medium_hits.append(f"{desc} — {entry.filename}")

            # Malicious class-name patterns
            data_lower = data.lower()
            for cn in _MALICIOUS_NAMES:
                if cn in data_lower:
                    name_hits.append(
                        f"Malicious identifier pattern '{cn.decode()}' — {entry.filename}"
                    )
    finally:
        zf.close()

    # -- Risk classification --------------------------------------------------
    if high_hits or name_hits:
        result.risk_level = "malicious"
        result.findings   = list(dict.fromkeys(high_hits + name_hits))  # dedup
        result.blocked    = True
    elif medium_hits:
        result.risk_level = "suspicious"
        result.findings   = list(dict.fromkeys(medium_hits))
    # else: safe, findings stays empty

    return result
