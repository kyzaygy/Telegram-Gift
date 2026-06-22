from __future__ import annotations

from dataclasses import dataclass

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@dataclass
class ProbeConfig:
    hole_tolerance: int = 5


@dataclass
class IntervalConfig:
    coarse_sec: float = 60.0
    mid_sec: float = 10.0
    tight_sec: float = 1.5


@dataclass
class ZoneConfig:
    mid_at: int = 400
    tight_lead: int = 4


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


def load_config(path: str = "targets.yaml") -> AppConfig:
    env = EnvSettings()

    with open(path) as f:
        raw = yaml.safe_load(f)

    m = raw.get("model", {})
    model = ModelConfig(
        slug_stem=m.get("slug_stem", ""),
        gift_id=int(m.get("gift_id", 0)),
    )

    p = raw.get("probe", {})
    probe = ProbeConfig(hole_tolerance=int(p.get("hole_tolerance", 5)))

    iv = raw.get("intervals", {})
    intervals = IntervalConfig(
        coarse_sec=float(iv.get("coarse_sec", 60.0)),
        mid_sec=float(iv.get("mid_sec", 10.0)),
        tight_sec=float(iv.get("tight_sec", 1.5)),
    )

    z = raw.get("zones", {})
    zones = ZoneConfig(
        mid_at=int(z.get("mid_at", 400)),
        tight_lead=int(z.get("tight_lead", 4)),
    )

    targets = [TargetConfig(**t) for t in raw.get("targets", [])]

    r = raw.get("runtime", {})
    runtime = RuntimeConfig(armed=bool(r.get("armed", False)))

    return AppConfig(
        env=env,
        model=model,
        probe=probe,
        intervals=intervals,
        zones=zones,
        targets=targets,
        runtime=runtime,
    )
