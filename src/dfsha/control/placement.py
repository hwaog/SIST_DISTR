"""Política de colocación de réplicas.

Objetivos (en este orden):

1. **Tolerancia a fallos**: réplicas en DataNodes distintos y, si existen ≥2
   *zonas* (rack / zona de disponibilidad), abarcar al menos dos zonas.
2. **Balance de carga/espacio**: «potencia de dos opciones» (power of two
   choices): se muestrean dos candidatos y se elige el menos utilizado.  Con
   O(1) por decisión evita tanto el sesgo de «siempre el más vacío» (hot spot al
   añadir un nodo) como el azar puro.
3. **Localidad de escritura** (opcional): si el cliente declara su zona, la
   primera réplica de la tubería se coloca en ella (menos tráfico inter-zona).
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Candidate:
    id: str
    zone: str
    used: int
    capacity: int
    load: float  # transferencias en curso (reportado en el heartbeat)

    @property
    def util(self) -> float:
        return self.used / self.capacity if self.capacity else 1.0

    @property
    def key(self) -> tuple[int, int]:
        """Clave de orden (menor es mejor): banda de utilización y luego escalón de carga.

        Tanto la utilización (bandas del 5 %) como la carga instantánea (escalones de 8 transferencias
        en curso) se **discretizan**: dos nodos «casi igual» *empatan* y se elige al azar entre ellos.
        Sin esto, «el menor de dos» condena al nodo marginalmente peor (0,01 % más lleno, o una
        transferencia más en curso en el último *heartbeat*) a no recibir nunca datos.
        """
        return (int(self.util / BAND), int(self.load // LOAD_STEP))


BAND = 0.05
LOAD_STEP = 8


def choose(
    candidates: list[Candidate],
    n: int,
    *,
    exclude: set[str] | frozenset[str] = frozenset(),
    zone_hint: str | None = None,
    size: int = 0,
    rng: random.Random | None = None,
) -> list[Candidate]:
    rng = rng or random
    pool = [c for c in candidates if c.id not in exclude and c.capacity - c.used >= size]
    chosen: list[Candidate] = []
    zones_total = len({c.zone for c in pool})
    while pool and len(chosen) < n:
        zones_used = {c.zone for c in chosen}
        sub = pool
        if not chosen and zone_hint and any(c.zone == zone_hint for c in pool):
            sub = [c for c in pool if c.zone == zone_hint]
        elif zones_total >= 2 and len(zones_used) < 2 and chosen:
            diff = [c for c in pool if c.zone not in zones_used]
            sub = diff or pool
        pick = _p2c(sub, rng)
        chosen.append(pick)
        pool = [c for c in pool if c.id != pick.id]
    return chosen


def _p2c(pool: list[Candidate], rng) -> Candidate:
    if len(pool) == 1:
        return pool[0]
    a, b = rng.sample(pool, 2)
    if a.key == b.key:
        return rng.choice((a, b))
    return a if a.key < b.key else b
