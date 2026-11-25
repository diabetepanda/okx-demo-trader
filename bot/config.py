"""Configuration loader for settings.toml, control.json, and .env."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 fallback
    tomllib = None  # type: ignore[assignment]


def _parse_toml_value(raw: str) -> Any:
    val = raw.strip()
    if val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    if val.startswith("'") and val.endswith("'"):
        return val[1:-1]
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_toml_value(item) for item in inner.split(",")]
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def _simple_toml_load(text: str) -> dict[str, Any]:
    """Minimal TOML parser for settings.toml when tomllib is unavailable."""
    root: dict[str, Any] = {}
    current: dict[str, Any] = root
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section_path = [p.strip() for p in line[1:-1].split(".") if p.strip()]
            target = root
            for part in section_path:
                if part not in target or not isinstance(target[part], dict):
                    target[part] = {}
                target = target[part]
            current = target
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            current[key.strip()] = _parse_toml_value(val)
    return root


def load_toml_file(path: Path) -> dict[str, Any]:
    """Load a TOML file using stdlib tomllib or built-in fallback."""
    if tomllib is not None:
        with path.open("rb") as f:
            return tomllib.load(f)
    return _simple_toml_load(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class ControlState:
    paused: bool = False
    close_all: bool = False


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    api_key: str
    api_secret: str
    api_passphrase: str
    trading_mode: str
    dry_run: bool
    settings: dict[str, Any]
    control: ControlState
    base_url: str = "https://www.okx.com"

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


def parse_bool_env(val: Optional[str], default: bool = True) -> bool:
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def load_control(path: Path) -> ControlState:
    if not path.exists():
        return ControlState(paused=False, close_all=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    return ControlState(
        paused=bool(data.get("paused", False)),
        close_all=bool(data.get("close_all", False)),
    )


def load_config(base_dir: Optional[Path] = None) -> AppConfig:
    root = base_dir or Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)

    settings_path = root / "settings.toml"
    settings = load_toml_file(settings_path) if settings_path.exists() else {}
    control = load_control(root / "control.json")

    dry_run = parse_bool_env(os.environ.get("DRY_RUN"), default=True)

    return AppConfig(
        base_dir=root,
        api_key=os.environ.get("OKX_API_KEY", "").strip(),
        api_secret=os.environ.get("OKX_API_SECRET", "").strip(),
        api_passphrase=os.environ.get("OKX_API_PASSPHRASE", "").strip(),
        trading_mode=os.environ.get("TRADING_MODE", "").strip(),
        dry_run=dry_run,
        settings=settings,
        control=control,
    )
