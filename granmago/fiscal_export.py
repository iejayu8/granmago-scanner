"""Export of the append-only fiscal journals to a shared folder.

1.8.1: the export is always built in a LOCAL staging folder (sqlite backup,
CSV and manifest). Only finished files are then copied to the configured
destination (e.g. an SMB/CIFS NAS under /share/nas_ugreen), each one to a .tmp
there and renamed, on a worker thread with a timeout. 1.8.0 ran the SQLite
backup directly on the destination: on CIFS SQLite's file locking hung and left
a 0-byte .fiscal_journal_demo.sqlite.*.tmp with nothing in the log.

Staging: /share/granmago-fiscal (local disk of HA, readable from Samba/the
host); if that cannot be written, <DATA_DIR>/granmago-fiscal-staging.

For every ``fiscal_journal_<mode>.sqlite`` in the add-on data dir it writes,
into the export dir (default ``/share/granmago-fiscal``):

* ``fiscal_journal_<mode>.sqlite``: consistent copy made with SQLite's online
  backup API (never ``cp`` of a live WAL database), switched to
  journal_mode=DELETE so it is one self-contained file.
* ``fiscal_journal_<mode>.csv``: the ``trades_eur`` view (trades + EUR rate).
* ``manifest.json``: export time, engine version, rows and SHA-256 per file.

Every file is written to a temporary name in the same folder, fsynced and then
renamed (atomic on POSIX). The source journal is opened read-only and is never
modified. Before a file is published it is checked for the configured secrets
(API key, secret, passphrase, control token); a hit aborts that export.
"""
import csv
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import fiscal_journal

DEFAULT_DIR = "/share/granmago-fiscal"
DEFAULT_MINUTES = 15
STAGING_DIR = DEFAULT_DIR                    # preferred local staging
STAGING_FALLBACK = "granmago-fiscal-staging"  # under DATA_DIR when /share is not writable
DEFAULT_COPY_TIMEOUT = 60                     # seconds for the whole copy to the destination
SHARE_ROOT = "/share"                         # /share/<mount>/... must be a mount point (NAS)
COPY_CHUNK = 1 << 20
JOURNAL_GLOB = "fiscal_journal_*.sqlite"
# funding_usdt is NOT NULL in schema 1: when Blofin gave no funding the CSV
# shows it empty instead of 0, so nothing looks known that is not.
_FUNDING_UNKNOWN = ("unavailable", "pending")


class ExportError(RuntimeError):
    pass


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _check_secrets(path, secrets):
    data = Path(path).read_bytes()
    for s in secrets:
        if s and len(s) >= 4 and s.encode() in data:
            raise ExportError("secreto detectado en %s: exportación cancelada" % Path(path).name)


def _publish(tmp, final, secrets):
    try:
        _check_secrets(tmp, secrets)
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; readers outside the add-on need read
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tmp_in(dest, name):
    fd, tmp = tempfile.mkstemp(prefix="." + name + ".", suffix=".tmp", dir=str(dest))
    os.close(fd)
    return tmp


def _backup_sqlite(src, dest, secrets):
    final = dest / src.name
    tmp = _tmp_in(dest, src.name)
    try:
        s = sqlite3.connect("file:%s?mode=ro" % src, uri=True, timeout=10)
        try:
            d = sqlite3.connect(tmp)
            try:
                s.backup(d)
                d.execute("PRAGMA journal_mode=DELETE")
                rows = d.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                ok = d.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                d.close()
        finally:
            s.close()
        if ok != "ok":
            raise ExportError("copia sqlite no íntegra: %s" % ok)
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _publish(tmp, final, secrets)
    return final, rows


def _write_csv(src, dest, secrets):
    final = dest / (src.stem + ".csv")
    con = sqlite3.connect("file:%s?mode=ro" % src, uri=True, timeout=10)
    try:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM trades_eur ORDER BY seq")]
    finally:
        con.close()
    cols = list(fiscal_journal.EXPORT_COLUMNS)
    tmp = _tmp_in(dest, final.name)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                if r.get("funding_status") in _FUNDING_UNKNOWN:
                    r["funding_usdt"] = None
                w.writerow(r)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _publish(tmp, final, secrets)
    return final, len(rows)


class RemoteCopyError(ExportError):
    """The local export is fine; copying it to the destination failed."""


def _is_orphan_tmp(name):
    # Our temp names: "." + final name + "." + random + ".tmp" (mkstemp / copy).
    return name.startswith(".") and name.endswith(".tmp") and (
        name.startswith(".fiscal_journal_") or name.startswith(".manifest.json."))


def clean_orphans(folder):
    """Remove temp files left by an interrupted export. Returns their names."""
    gone = []
    for p in Path(folder).iterdir():
        if _is_orphan_tmp(p.name) and p.is_file():
            try:
                p.unlink()
                gone.append(p.name)
            except OSError:
                pass
    return sorted(gone)


def _writable_dir(path):
    try:
        fd, probe = tempfile.mkstemp(prefix=".probe.", dir=str(path))
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        return False


def choose_staging(data_dir, preferred=None):
    """Local staging folder: `preferred` (default /share/granmago-fiscal),
    created if its parent exists; else <data_dir>/granmago-fiscal-staging."""
    pref = Path(preferred or STAGING_DIR)
    try:
        if not pref.exists() and pref.parent.is_dir():
            pref.mkdir(exist_ok=True)
        if pref.is_dir() and _writable_dir(pref):
            return pref
    except OSError:
        pass
    fb = Path(data_dir) / STAGING_FALLBACK
    fb.mkdir(parents=True, exist_ok=True)
    return fb


def validate_dest(dest, data_dir):
    """String-only checks (no filesystem access: a hung mount must not block
    the caller). The real checks run on the copy thread (_check_remote)."""
    if not dest or not str(dest).startswith("/"):
        raise ExportError("ruta de exportación no absoluta: %r" % (dest,))
    out = os.path.normpath(str(dest))
    data = os.path.normpath(os.path.abspath(str(data_dir)))
    if out == data or out.startswith(data + os.sep):
        raise ExportError("la exportación no puede ir dentro de la carpeta de datos del complemento")
    return Path(out)


def _mount_root(dest):
    """/share/<name> for a destination /share/<name>/..., else None."""
    root = Path(SHARE_ROOT)
    try:
        rel = Path(dest).relative_to(root)
    except ValueError:
        return None
    return root / rel.parts[0] if len(rel.parts) >= 2 else None


def _check_remote(dest):
    if not dest.parent.is_dir():
        raise ExportError("la carpeta padre %s no existe (¿NAS no montado?)" % dest.parent)
    mount = _mount_root(dest)
    if mount is not None and not os.path.ismount(str(mount)):
        raise ExportError("%s no es un punto de montaje (¿NAS no montado?)" % mount)
    if not dest.is_dir():
        raise ExportError("la carpeta de exportación %s no existe (¿NAS no montado?); no se crea" % dest)
    if not os.access(str(dest), os.W_OK):
        raise ExportError("sin permiso de escritura en %s" % dest)


class _Pinned:
    """A staging file opened before the copy thread starts. The open fd keeps
    the inode of THIS build: if a later export replaces the staging files
    (os.replace) while a hung copy resumes, it still copies one consistent
    set (sqlite, CSV and the manifest that describes them)."""

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.fh = open(self.path, "rb")

    def close(self):
        try:
            self.fh.close()
        except OSError:
            pass


def _copy_one(src, dest):
    """Whole-file copy to a .tmp in dest, fsync, size + SHA-256 check, rename.
    `src` is a _Pinned staging file (a path is pinned here)."""
    own = not isinstance(src, _Pinned)
    if own:
        src = _Pinned(src)
    try:
        return _copy_pinned(src, dest)
    finally:
        if own:
            src.close()


def _copy_pinned(src, dest):
    final = dest / src.name
    tmp = dest / (".%s.%s.tmp" % (src.name, os.urandom(4).hex()))
    fi = src.fh
    fi.seek(0)
    want = hashlib.sha256()
    for chunk in iter(lambda: fi.read(COPY_CHUNK), b""):
        want.update(chunk)
    size = os.fstat(fi.fileno()).st_size
    try:
        h = hashlib.sha256()
        fi.seek(0)
        with open(tmp, "wb") as fo:
            for chunk in iter(lambda: fi.read(COPY_CHUNK), b""):
                fo.write(chunk)
                h.update(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        if h.hexdigest() != want.hexdigest() or tmp.stat().st_size != size:
            raise ExportError("copia de %s no coincide con el staging" % src.name)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final.name


def _copy_files(staging, dest, names):
    """`names` are staging file names or _Pinned files (manifest.json last)."""
    _check_remote(dest)
    cleaned = clean_orphans(dest)
    copied = [_copy_one(n if isinstance(n, _Pinned) else Path(staging) / n, dest) for n in names]
    _fsync_dir(dest)
    return {"copied": copied, "cleaned": cleaned}


_COPY_STATE = {"thread": None, "dest": None}


def copy_to_dest(staging, dest, names, timeout=DEFAULT_COPY_TIMEOUT):
    """Run _copy_files on a daemon thread and wait at most `timeout` s. A copy
    stuck on a hung SMB mount is abandoned (the thread cannot be killed) and
    no second copy is started while it is still stuck."""
    prev = _COPY_STATE["thread"]
    if prev is not None and prev.is_alive():
        raise RemoteCopyError("la copia anterior a %s sigue colgada; no se lanza otra" % _COPY_STATE["dest"])
    box = {}
    # 1.8.2: pin this build's files (local disk, fast) before the thread starts.
    pinned = []
    try:
        for n in names:
            pinned.append(_Pinned(Path(staging) / n))
    except OSError as exc:
        for f in pinned:
            f.close()
        raise RemoteCopyError("staging %s ilegible: %s" % (staging, exc))

    def run():
        try:
            box["ok"] = _copy_files(Path(staging), Path(dest), pinned)
        except BaseException as exc:  # reported to the caller
            box["err"] = exc
        finally:
            for f in pinned:
                f.close()

    t = threading.Thread(target=run, name="granmago-fiscal-copy", daemon=True)
    _COPY_STATE.update(thread=t, dest=str(dest))
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise RemoteCopyError("timeout de %ss copiando a %s (¿montaje SMB colgado?)" % (timeout, dest))
    if "err" in box:
        err = box["err"]
        raise RemoteCopyError(str(err) if isinstance(err, ExportError) else "%s: %s" % (type(err).__name__, err))
    return box["ok"]


def export_all(data_dir, dest, *, secrets=(), engine_version="", staging=None, timeout=DEFAULT_COPY_TIMEOUT):
    """Build the export in local staging, then copy it to `dest` if different.

    Returns None when there is no journal yet, else a dict with manifest,
    staging, dest, copied/cleaned and remoteError (None when dest has the full
    export). Raises ExportError/OSError only when the LOCAL export fails."""
    data_dir = Path(data_dir)
    sources = sorted(p for p in data_dir.glob(JOURNAL_GLOB) if p.is_file())
    if not sources:
        return None
    dest = validate_dest(dest, data_dir)
    out = Path(staging) if staging is not None else choose_staging(data_dir)
    clean_orphans(out)
    manifest = build_local(sources, out, secrets=secrets, engine_version=engine_version)
    result = {"manifest": manifest, "staging": str(out), "dest": str(dest), "copied": [], "cleaned": [],
              "remoteError": None}
    if os.path.normpath(str(out)) == str(dest):
        return result
    names = [f["file"] for f in manifest["files"]] + ["manifest.json"]
    try:
        result.update(copy_to_dest(out, dest, names, timeout))
    except RemoteCopyError as exc:
        result["remoteError"] = str(exc)
    return result


def build_local(sources, out, *, secrets=(), engine_version=""):
    """1.8.0 export (backup + CSV + manifest, atomic, secrets check) into the
    local folder `out`. Never used on a remote path."""
    out = Path(out)
    files = []
    for src in sources:
        mode = src.stem[len("fiscal_journal_"):]
        for kind, fn in (("sqlite", _backup_sqlite), ("csv", _write_csv)):
            final, rows = fn(src, out, secrets)
            files.append({"file": final.name, "mode": mode, "format": kind, "rows": rows,
                          "sha256": _sha256(final), "bytes": final.stat().st_size})
    manifest = {"exportedAtUtc": _utc_now(), "engineVersion": engine_version,
                "schemaVersion": fiscal_journal.SCHEMA_VERSION, "files": files,
                "note": "Copia de solo lectura del diario fiscal append-only de GranMago. "
                        "El original vive en /data del complemento y no se modifica."}
    tmp = _tmp_in(out, "manifest.json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    _publish(tmp, out / "manifest.json", secrets)
    _fsync_dir(out)
    return manifest
