from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
    tg_session: str = "surfsniper"


@dataclass
class PollConfig:
    coarse_sec: float = 45.0
    approach_sec: float = 3.0
    armed_sec: float = 0.3


@dataclass
class FirecontrolConfig:
    safety: int = 1
    bracket: bool = False


@dataclass
class ModelConfig:
    slug_stem: str = ""
    example_slug: str = ""

    def initial_frontier(self) -> int:
        """Parse the numeric suffix from example_slug (e.g. 'SurfStar-12' → 12)."""
        if not self.example_slug or not self.slug_stem:
            return 0
        prefix = f"{self.slug_stem}-"
        if self.example_slug.startswith(prefix):
            try:
                return int(self.example_slug[len(prefix):])
            except ValueError:
                return 0
        return 0


@dataclass
class TargetConfig:
    num: int
    ammo_index: int


@dataclass
class RuntimeConfig:
    dry_run: bool = True


@dataclass
class Config:
    env: EnvSettings
    poll: PollConfig
    firecontrol: FirecontrolConfig
    model: ModelConfig
    targets: list[TargetConfig]
    runtime: RuntimeConfig


def load_config(targets_path: str = "targets.yaml") -> Config:
    env = EnvSettings()

    with open(targets_path) as f:
        data = yaml.safe_load(f)

    p = data.get("poll", {})
    poll = PollConfig(
        coarse_sec=float(p.get("coarse_sec", 45.0)),
        approach_sec=float(p.get("approach_sec", 3.0)),
        armed_sec=float(p.get("armed_sec", 0.3)),
    )

    fc = data.get("firecontrol", {})
    firecontrol = FirecontrolConfig(
        safety=int(fc.get("safety", 1)),
        bracket=bool(fc.get("bracket", False)),
    )

    m = data.get("model", {})
    model = ModelConfig(
        slug_stem=m.get("slug_stem", ""),
        example_slug=m.get("example_slug", ""),
    )

    targets = [TargetConfig(**t) for t in data.get("targets", [])]

    r = data.get("runtime", {})
    runtime = RuntimeConfig(dry_run=bool(r.get("dry_run", True)))

    return Config(
        env=env,
        poll=poll,
        firecontrol=firecontrol,
        model=model,
        targets=targets,
        runtime=runtime,
    )
