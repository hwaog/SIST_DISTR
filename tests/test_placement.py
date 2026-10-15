import random
from collections import Counter

from dfsha.control.placement import Candidate, choose

GB = 1 << 30


def nodes(spec):
    return [Candidate(i, z, int(u * 100 * GB), 100 * GB, 0.0) for i, z, u in spec]


def test_distinct_nodes_and_zone_diversity():
    c = nodes([("a", "z1", 0), ("b", "z1", 0), ("c", "z2", 0), ("d", "z2", 0)])
    rng = random.Random(1)
    for _ in range(200):
        picks = choose(c, 3, rng=rng)
        assert len({p.id for p in picks}) == 3
        assert len({p.zone for p in picks}) == 2


def test_uniform_when_utilisation_is_similar():
    # diferencias marginales de ocupación NO deben condenar a un nodo a quedarse sin datos
    c = nodes([("a", "z1", 0.0010), ("b", "z2", 0.0012), ("c", "z1", 0.0011), ("d", "z2", 0.0030)])
    cnt = Counter()
    rng = random.Random(2)
    for _ in range(4000):
        for p in choose(c, 3, rng=rng):
            cnt[p.id] += 1
    assert set(cnt) == {"a", "b", "c", "d"}
    assert min(cnt.values()) > 0.6 * max(cnt.values()), cnt


def test_strongly_loaded_node_is_avoided():
    c = nodes([("a", "z1", 0.10), ("b", "z2", 0.10), ("c", "z1", 0.10), ("full", "z2", 0.90)])
    cnt = Counter()
    rng = random.Random(3)
    for _ in range(2000):
        for p in choose(c, 2, rng=rng):
            cnt[p.id] += 1
    assert cnt["full"] < 0.25 * cnt["a"], cnt


def test_exclusions_capacity_and_zone_hint():
    c = nodes([("a", "z1", 0), ("b", "z2", 0), ("c", "z2", 0.999)])
    assert {p.id for p in choose(c, 3, exclude={"a"}, size=GB)} == {"b"}  # c sin espacio, a excluido
    rng = random.Random(4)
    assert all(choose(c, 1, zone_hint="z2", size=GB, rng=rng)[0].id == "b" for _ in range(50))
    assert choose([], 3) == []


def test_small_load_differences_do_not_starve_a_node():
    # un heartbeat con 1-3 transferencias en curso no debe excluir a un nodo de todo un lote
    c = nodes([("a", "z1", 0.01), ("b", "z2", 0.01), ("c", "z1", 0.01), ("d", "z2", 0.01)])
    for node, load in zip(c, (0.0, 1.0, 2.0, 3.0), strict=True):
        node.load = load
    cnt = Counter()
    rng = random.Random(5)
    for _ in range(2000):
        for p in choose(c, 3, rng=rng):
            cnt[p.id] += 1
    assert min(cnt.values()) > 0.7 * max(cnt.values()), cnt
    c[0].load = 80.0  # un nodo realmente saturado sí se evita
    cnt = Counter()
    for _ in range(2000):
        for p in choose(c, 2, rng=rng):
            cnt[p.id] += 1
    assert cnt["a"] < 0.5 * cnt["c"], cnt
