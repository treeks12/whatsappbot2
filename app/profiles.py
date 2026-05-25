from dataclasses import dataclass
from random import uniform
from typing import Dict, Tuple


@dataclass(frozen=True)
class DelayProfile:
    id: str
    label: str
    media_delay: Tuple[float, float]
    text_delay: Tuple[float, float]
    client_delay: Tuple[float, float]
    returning_client_delay: Tuple[float, float]
    pause_every: int
    pause_duration: Tuple[float, float]

    def between_media(self) -> float:
        return uniform(*self.media_delay)

    def before_text(self) -> float:
        return uniform(*self.text_delay)

    def between_clients(self) -> float:
        return uniform(*self.client_delay)

    def between_returning_clients(self) -> float:
        return uniform(*self.returning_client_delay)

    def pause(self) -> float:
        return uniform(*self.pause_duration)


PROFILES: Dict[str, DelayProfile] = {
    "confianca_100": DelayProfile(
        id="confianca_100",
        label="Confianca 100",
        media_delay=(1, 3),
        text_delay=(0, 0),
        client_delay=(8, 20),
        returning_client_delay=(5, 12),
        pause_every=75,
        pause_duration=(60, 120),
    ),
    "precaucao_100": DelayProfile(
        id="precaucao_100",
        label="Precaucao 100",
        media_delay=(2, 6),
        text_delay=(0, 0),
        client_delay=(20, 50),
        returning_client_delay=(10, 25),
        pause_every=30,
        pause_duration=(120, 300),
    ),
    "loja_100": DelayProfile(
        id="loja_100",
        label="Loja 100",
        media_delay=(2, 6),
        text_delay=(0, 0),
        client_delay=(18, 45),
        returning_client_delay=(8, 22),
        pause_every=50,
        pause_duration=(90, 180),
    ),
    "humano_100": DelayProfile(
        id="humano_100",
        label="Humano 100",
        media_delay=(8, 20),
        text_delay=(20, 45),
        client_delay=(45, 120),
        returning_client_delay=(20, 60),
        pause_every=20,
        pause_duration=(300, 900),
    ),
    "normal": DelayProfile(
        id="normal",
        label="Normal",
        media_delay=(3, 7),
        text_delay=(10, 15),
        client_delay=(20, 45),
        returning_client_delay=(10, 25),
        pause_every=25,
        pause_duration=(120, 300),
    ),
}


def get_profile(profile_id: str) -> DelayProfile:
    return PROFILES.get(profile_id, PROFILES["precaucao_100"])
