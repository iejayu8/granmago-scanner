"""1.8.0: export of the append-only fiscal journals to a shared folder.

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
from datetime import datetime, timezone
from pathlib import Path

import fiscal_journal

DEFAULT_DIR = "/share/granmago-fiscal"
DEFAULT_MINUTES = 15
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


def resolve_dest(dest, data_dir):
    """Validate the export dir. The default dir is created if /share exists;
    any other dir must already exist (an unmounted NAS must not be silently
    replaced by a local folder)."""
    if not dest or not str(dest).startswith("/"):
        raise ExportError("ruta de exportación no absoluta: %r" % (dest,))
    out = Path(dest)
    data = Path(data_dir).resolve()
    if not out.exists():
        if str(out) == DEFAULT_DIR and out.parent.is_dir():
            out.mkdir(exist_ok=True)
        else:
            raise ExportError("la carpeta de exportación no existe (¿NAS desmontado?): %s" % out)
    if not out.is_dir():
        raise ExportError("la ruta de exportación no es una carpeta: %s" % out)
    real = out.resolve()
    if real == data or data in real.parents:
        raise ExportError("la exportación no puede ir dentro de la carpeta de datos del complemento")
    return out


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


def export_all(data_dir, dest, *, secrets=(), engine_version=""):
    """Export every journal in data_dir. Returns the manifest dict, or None
    when there is no journal yet. Raises ExportError/OSError on failure."""
    data_dir = Path(data_dir)
    sources = sorted(p for p in data_dir.glob(JOURNAL_GLOB) if p.is_file())
    if not sources:
        return None
    out = resolve_dest(dest, data_dir)
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
