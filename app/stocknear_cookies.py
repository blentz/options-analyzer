"""Firefox/LibreWolf cookies.sqlite reader.

Pulled out of stocknear.py so this can be tested / reused without
importing Playwright. The original module re-exports the helper for
backward compatibility.
"""

import logging
import shutil
import sqlite3
import tempfile
import time as _time
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_browser_cookies(profile_path: str, domain_filter: str = "stocknear.com") -> list[dict]:
    """Extract cookies from Firefox/LibreWolf cookies.sqlite for a specific domain.

    The cookies file is opened by a running browser, so we copy it into a
    private temporary directory before reading. Using TemporaryDirectory
    (instead of a manual NamedTemporaryFile + unlink in finally) ensures the
    copy is removed even if the process is killed between copy and cleanup —
    the OS-managed cleanup runs on directory __exit__ unconditionally.
    """
    cookies_db = Path(profile_path) / "cookies.sqlite"

    if not cookies_db.exists():
        logger.error("cookies.sqlite not found at %s", cookies_db)
        return []

    logger.debug("Extracting cookies from %s for domain %s", cookies_db, domain_filter)

    cookies: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="cookies-") as tmpdir:
        tmp_path = Path(tmpdir) / "cookies.sqlite"
        # Restrict perms so other users on the host can't read it while alive
        shutil.copy(cookies_db, tmp_path)
        # Firefox runs cookies.sqlite in WAL mode: a still-running browser buffers
        # recent writes (e.g. a fresh login) in the -wal sidecar before
        # checkpointing them into the main file. Copy -wal/-shm alongside so
        # SQLite replays those frames and we see the current session rather than
        # a stale token. (Missing sidecars are fine — DELETE-mode DBs lack them.)
        for suffix in ("-wal", "-shm"):
            sidecar = cookies_db.with_name(cookies_db.name + suffix)
            if sidecar.exists():
                shutil.copy(sidecar, Path(tmpdir) / (tmp_path.name + suffix))
        try:
            tmp_path.chmod(0o600)
        except Exception:
            pass

        conn = sqlite3.connect(str(tmp_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite
                FROM moz_cookies
                WHERE host LIKE ?
                """,
                (f"%{domain_filter}%",),
            )
            for row in cursor.fetchall():
                name, value, host, path, expiry, is_secure, is_http_only, same_site = row
                same_site_map = {0: "None", 1: "Lax", 2: "Strict"}
                cookie = {
                    "name": name,
                    "value": value,
                    "domain": host,
                    "path": path,
                    "secure": bool(is_secure),
                    "httpOnly": bool(is_http_only),
                    "sameSite": same_site_map.get(same_site, "Lax"),
                }
                if expiry and expiry > 0:
                    if expiry > 10000000000000:
                        cookie["expires"] = expiry // 1000000
                    elif expiry > 10000000000:
                        cookie["expires"] = expiry // 1000
                    else:
                        cookie["expires"] = expiry
                cookies.append(cookie)
        finally:
            conn.close()

    logger.info("Extracted %d cookies for domain %s", len(cookies), domain_filter)
    if cookies:
        # Don't log values; only metadata.
        import time as time_module
        now = time_module.time()
        for c in cookies:
            if c["name"] in ("pb_auth", "session", "auth", "token", "cf_clearance"):
                expires = c.get("expires", 0)
                is_expired = expires > 0 and expires < now
                logger.info(
                    "Auth cookie '%s': domain=%s, expires=%s, expired=%s",
                    c["name"], c["domain"], expires, is_expired
                )
    return cookies


