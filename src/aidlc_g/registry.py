"""Content-addressed artifact registry and deterministic packaging (milestone M4).

Only the trusted CI runner may publish. Artifacts are addressed by the SHA-256
of their bytes and re-verified on every read, so a swapped blob is detected
before anything is deployed.
"""

import io
import tarfile
import threading
from typing import Dict, Mapping

from .canonical import file_digest
from .errors import AuthorizationError, Conflict, NotFound
from .service import GovernanceService


def deterministic_tar(files: Mapping[str, bytes]) -> bytes:
    """Same inputs -> same bytes: sorted entries, zeroed times and owners, fixed modes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(files):
            info = tarfile.TarInfo(path)
            info.size, info.mtime, info.mode = len(files[path]), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(files[path]))
    return buffer.getvalue()


class ArtifactRegistry:
    def __init__(self, gate: GovernanceService) -> None:
        self._gate = gate
        self._blobs: Dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, token: str, data: bytes) -> str:
        if "ci_runner" not in self._gate.whoami(token)["roles"]:
            raise AuthorizationError("ROLE_NOT_PERMITTED", "only CI runners publish artifacts")
        address = file_digest(data)
        with self._lock:
            self._blobs.setdefault(address, bytes(data))
        return address

    def get(self, address: str) -> bytes:
        data = self._blobs.get(address)
        if data is None:
            raise NotFound("UNKNOWN_ARTIFACT", address)
        if file_digest(data) != address:
            raise Conflict("ARTIFACT_CORRUPT", f"stored bytes no longer match {address}")
        return data

    def _corrupt_for_test(self, address: str, data: bytes) -> None:
        """Simulates a compromised registry swapping a blob (tests only)."""
        self._blobs[address] = data
