from __future__ import annotations

import logging
from dataclasses import dataclass

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger(__name__)

_KNOWN_SECTIONS: set[str] = {"model", "probe", "intervals", "zones", "targets", "runtime"}
_KNOWN_FIELDS: dict[str, set[str]] = {
    "model":     {"slug_stem", "gift_id", "example_slug"},
    "probe":     {"hole_tolerance"},
    "intervals": {"coarse_sec", "mid_sec", "tight_sec"},
    "zones":     {"mid_at", "tight_lead"},
    "runtime":   {"armed"},
}


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tg_api_id: int
    tg_api_hash: str
    tg_session: str = "probe"

    web_token: str = ""
    web_host: str = "127.0.0.1"
    web_port: int = 8080


@dataclass
class ModelConfig:
    slug_stem: str = ""
    gift_id: int = 0
    example_slug: str = ""


@dataclass
class ProbeConfig:
    hole_tolerance: int = 5


@dataclass
class IntervalConfig:
    coarse_sec: float = 60.0
    mid_sec: float = 10.0
    tight_sec: float = 0.3


@dataclass
class ZoneConfig:
    mid_at: int = 400
    tight_lead: int = 44


@dataclass
class TargetConfig:
    num: int
    ammo_index: int


@dataclass
class RuntimeConfig:
    armed: bool = False


@dataclass
class AppConfig:
    env: EnvSettings
    model: ModelConfig
    probe: ProbeConfig
    intervals: IntervalConfig
    zones: ZoneConfig
    targets: list[TargetConfig]
    runtime: RuntimeConfig


def _check_unknown(raw: dict) -> None:
    for section in raw:
        if section == "targets":
            continue
        if section not in _KNOWN_SECTIONS:
            _log.warning("config_unknown_section section=%s", section)
            continue
        known = _KNOWN_FIELDS.get(section, set())
        section_data = raw[section]
        if isinstance(section_data, dict):
            for field in section_data:
                if field not in known:
                    _log.warning("config_unknown_field section=%s field=%s", section, field)


def load_config(path: str = "targets.yaml") -> AppConfig:
    env = EnvSettings()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    _check_unknown(raw)

    m = raw.get("model", {})
    model = ModelConfig(
        slug_stem=m.get("slug_stem", ""),
        gift_id=int(m.get("gift_id", 0)),
        example_slug=m.get("example_slug", ""),
    )

    p = raw.get("probe", {})
    probe = ProbeConfig(hole_tolerance=int(p.get("hole_tolerance", 5)))

    iv = raw.get("intervals", {})
    intervals = IntervalConfig(
        coarse_sec=float(iv.get("coarse_sec", 60.0)),
        mid_sec=float(iv.get("mid_sec", 10.0)),
        tight_sec=float(iv.get("tight_sec", 0.3)),
    )

    z = raw.get("zones", {})
    zones = ZoneConfig(
        mid_at=int(z.get("mid_at", 400)),
        tight_lead=int(z.get("tight_lead", 44)),
    )

    targets = [TargetConfig(**t) for t in raw.get("targets", [])]

    r = raw.get("runtime", {})
    runtime = RuntimeConfig(armed=bool(r.get("armed", False)))

    _log.info(
        "config_loaded  coarse=%.0f mid=%.0f tight=%.3f mid_at=%d tight_lead=%d gift_id=%d armed=%s",
        intervals.coarse_sec, intervals.mid_sec, intervals.tight_sec,
        zones.mid_at, zones.tight_lead, model.gift_id, runtime.armed,
    )

    return AppConfig(
        env=env,
        model=model,
        probe=probe,
        intervals=intervals,
        zones=zones,
        targets=targets,
        runtime=runtime,
    )
