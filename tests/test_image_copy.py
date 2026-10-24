"""The image must contain every local module server.py imports.

1.5.0 copied an explicit file list and left granmago_strategy.py out, so the
add-on died on import. This fails if that list and the imports diverge.
"""

import ast
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "granmago"

SCHEMA_KEYS = {
    "log_level",
    "initial_balance",
    "risk_pct",
    "leverage",
    "max_positions",
    "fee_pct_per_side",
    "slippage_pct_per_side",
    "pairs",
}

# Keys present in the 1.5.0 schema and removed in 1.5.1.
OBSOLETE_KEYS = {
    "scan_seconds",
    "trailing_enabled",
    "trailing_activation_pct",
    "trailing_distance",
    "entry_threshold",
    "partial_tp_r",
    "partial_tp_fraction",
    "partial_move_breakeven",
    "stop_mode",
    "stop_atr_period",
    "stop_atr_mult",
    "trailing_activation_r",
    "trailing_keep_tp",
    "pair_cooldown_minutes",
    "max_same_direction_risk_r",
    "regime_filter",
    "regime_adx_min",
    "regime_er_min",
    "strategy",
    "granmago_variant",
    "granmago_max_adds",
    "granmago_momentum_exit",
    "granmago_max_hold_bars",
    "granmago_breakeven_r",
    "granmago_min_rr",
    "granmago_stop_pad_pct",
    "granmago_style",
    "granmago_setups",
}


def _local_py_modules():
    return {path.stem for path in ADDON.glob("*.py")}


def _imported_locals(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return {name for name in found if name in _local_py_modules()}


def _copied_into_app(dockerfile):
    """Sources a COPY places in /app. Wildcards expand against the add-on dir."""
    copied = set()
    for raw in dockerfile.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line.startswith("COPY "):
            continue
        parts = line.split()
        args = [part for part in parts[1:] if not part.startswith("--")]
        if len(args) < 2:
            continue
        dest, sources = args[-1], args[:-1]
        into_app = dest in (".", "./") or dest.startswith("/app")
        if not into_app and not dest.startswith("/"):
            into_app = True
        if not into_app:
            continue
        for src in sources:
            if any(ch in src for ch in "*?["):
                for match in ADDON.glob(src):
                    if match.is_file():
                        copied.add(match.name)
            else:
                copied.add(Path(src).name)
    return copied


def _needed_modules():
    needed = set()
    queue = ["server.py"]
    seen = set()
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = ADDON / name
        if not path.is_file():
            continue
        for mod in _imported_locals(path):
            filename = mod + ".py"
            needed.add(filename)
            queue.append(filename)
    return needed


def _block_keys(text, header):
    """Top-level keys of a two-space mapping that starts at `header:`."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(header + ":"):
            start = index + 1
            break
    if start is None:
        return []
    keys = []
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break
        if line.startswith("  ") and not line.startswith("   ") and not line.startswith("  -"):
            key = line.strip().split(":", 1)[0]
            if key:
                keys.append(key)
    return keys


class ImageCopyTest(unittest.TestCase):
    def test_dockerfile_copies_every_imported_local_module(self):
        dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
        needed = _needed_modules()
        self.assertIn("granmago_strategy.py", needed)
        copied = _copied_into_app(dockerfile)
        missing = needed - copied
        self.assertEqual(missing, set(), "Dockerfile does not copy %s" % sorted(missing))

    def test_reproduced_copy_imports_server(self):
        dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
        copied = _copied_into_app(dockerfile)
        with tempfile.TemporaryDirectory(prefix="granmago-image-") as tmp:
            app = Path(tmp)
            for name in copied:
                source = ADDON / name
                if source.is_file():
                    (app / name).write_bytes(source.read_bytes())
            static = ADDON / "static"
            if static.is_dir() and "static" in dockerfile:
                dest = app / "static"
                dest.mkdir()
                for item in static.iterdir():
                    if item.is_file():
                        (dest / item.name).write_bytes(item.read_bytes())
            data = app / "data"
            data.mkdir()
            env = os.environ.copy()
            env["DATA_DIR"] = str(data)
            env["LOG_LEVEL"] = "error"
            env["PYTHONPATH"] = str(app)
            env.pop("PYTHONHOME", None)
            script = textwrap.dedent(
                """
                import server
                import granmago_strategy
                assert server.VERSION == "1.5.1"
                assert server.strategy_mode() == "granmago"
                assert server.granmago_style() == "sweep"
                """
            )
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(app),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("ModuleNotFoundError", proc.stderr)


class OptionsSurfaceTest(unittest.TestCase):
    def test_schema_translations_and_run_script_list_the_same_options(self):
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        self.assertIn('version: "1.5.1"', config)
        self.assertEqual(set(_block_keys(config, "options")), SCHEMA_KEYS)
        self.assertEqual(set(_block_keys(config, "schema")), SCHEMA_KEYS)
        options_text = config.split("schema:", 1)[0]
        for key in OBSOLETE_KEYS:
            self.assertNotRegex(options_text, r"(?m)^  %s:" % re.escape(key))
        self.assertRegex(config, r"(?m)^  risk_pct: 1\s*$")
        self.assertRegex(config, r"(?m)^  leverage: 3\s*$")
        self.assertRegex(config, r"(?m)^  max_positions: 3\s*$")
        self.assertRegex(config, r"(?m)^  fee_pct_per_side: 0\.06\s*$")
        self.assertRegex(config, r"(?m)^  slippage_pct_per_side: 0\.02\s*$")
        self.assertEqual(options_text.count("-USDT"), 28)

        run = (ADDON / "run.sh").read_text(encoding="utf-8")
        exported = set(re.findall(r"bashio::config '([^']+)'", run))
        self.assertEqual(exported, SCHEMA_KEYS)
        block = re.search(r"obsolete=\((.*?)\)", run, re.S)
        self.assertIsNotNone(block)
        removed = set(re.findall(r"\b([a-z][a-z0-9_]*)\b", block.group(1)))
        self.assertEqual(removed, OBSOLETE_KEYS)
        self.assertTrue(removed.isdisjoint(SCHEMA_KEYS))

        for language in ("en", "es"):
            text = (ADDON / "translations" / ("%s.yaml" % language)).read_text(encoding="utf-8")
            self.assertEqual(set(_block_keys(text, "configuration")), SCHEMA_KEYS)


if __name__ == "__main__":
    unittest.main()
