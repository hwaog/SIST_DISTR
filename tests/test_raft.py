import asyncio
import random

import pytest
from raft_harness import FAST, Cluster, eventually

from dfsha.common.errors import NotLeader, Unavailable
from dfsha.raft.core import RaftConfig, Role


@pytest.fixture
async def cluster3(tmp_path):
    c = await Cluster(tmp_path, 3).start()
    yield c
    await c.stop()


async def put(leader, k, v, timeout=3.0):
    return await leader.propose({"k": k, "v": v}, timeout=timeout)


async def test_single_node_elects_itself_and_persists(tmp_path):
    c = await Cluster(tmp_path, 1).start()
    leader = await c.wait_leader()
    for i in range(5):
        await put(leader, f"k{i}", i)
    assert c.sms["n1"].seq == [0, 1, 2, 3, 4]
    term = leader.current_term
    await c.crash("n1")
    node = await c.restart("n1")
    await c.wait_leader()
    assert node.current_term > term  # reelección tras reinicio
    await eventually(lambda: c.sms["n1"].seq == [0, 1, 2, 3, 4], msg="no se recuperó el estado")
    await c.stop()


async def test_three_nodes_elect_exactly_one_leader_and_replicate(cluster3):
    leader = await cluster3.wait_leader()
    assert len(cluster3.leaders()) == 1
    for i in range(20):
        assert await put(leader, "x", i) > 0
    await eventually(lambda: all(sm.seq == list(range(20)) for sm in cluster3.sms.values()))


async def test_followers_reject_propose_and_point_to_leader(cluster3):
    leader = await cluster3.wait_leader()
    follower = next(n for n in cluster3.live() if n is not leader)
    await eventually(lambda: follower.leader_id == leader.id)
    with pytest.raises(NotLeader) as ei:
        await follower.propose({"k": "a", "v": 1})
    assert ei.value.leader == leader.id
    with pytest.raises(NotLeader):
        await follower.read_barrier()


async def test_leader_crash_new_leader_keeps_committed_data(cluster3):
    leader = await cluster3.wait_leader()
    for i in range(10):
        await put(leader, "a", i)
    await cluster3.crash(leader.id)
    new = await cluster3.wait_leader(exclude={leader.id})
    assert new.id != leader.id and new.current_term > leader.current_term
    await put(new, "a", 100)
    await eventually(lambda: all(sm.seq == [*range(10), 100] for n, sm in cluster3.sms.items() if n != leader.id))
    # el antiguo líder vuelve y se pone al día como seguidor
    await cluster3.restart(leader.id)
    await eventually(lambda: cluster3.sms[leader.id].seq == [*range(10), 100], msg="no alcanzó al líder")
    assert cluster3.net.nodes[leader.id].role == Role.FOLLOWER


async def test_minority_leader_cannot_commit_and_steps_down(cluster3):
    leader = await cluster3.wait_leader()
    await put(leader, "k", 1)
    cluster3.net.isolate(leader.id)
    with pytest.raises((Unavailable, NotLeader)):
        await put(leader, "k", 2, timeout=0.6)  # no hay mayoría: no se compromete
    await eventually(lambda: leader.role != Role.LEADER, msg="check-quorum no destituyó al líder aislado")
    new = await cluster3.wait_leader(exclude={leader.id})
    await put(new, "k", 3)
    cluster3.net.heal()
    # la entrada "2" del líder aislado (nunca comprometida) debe desaparecer en todos
    await eventually(lambda: all(sm.seq == [1, 3] for sm in cluster3.sms.values()), msg=str({k: v.seq for k, v in cluster3.sms.items()}))


async def test_prevote_isolated_follower_does_not_inflate_term(cluster3):
    leader = await cluster3.wait_leader()
    follower = next(n for n in cluster3.live() if n is not leader)
    term0 = leader.current_term
    cluster3.net.isolate(follower.id)
    await asyncio.sleep(1.5)  # muchos timeouts de elección fallidos
    assert follower.current_term == term0, "el pre-voto debe impedir inflar el término"
    cluster3.net.heal()
    await asyncio.sleep(0.6)
    assert leader.role == Role.LEADER and leader.current_term == term0, "el rejoin no debe derribar al líder"


async def test_lease_read_barrier_on_leader(cluster3):
    leader = await cluster3.wait_leader()
    await put(leader, "k", 1)
    await eventually(leader.lease_valid)
    await leader.read_barrier()  # vía rápida
    cluster3.net.isolate(leader.id)
    await asyncio.sleep(0.6)
    with pytest.raises((NotLeader, Unavailable)):
        await leader.read_barrier(timeout=0.5)


async def test_snapshot_compaction_and_install_on_lagging_node(tmp_path):
    cfg = RaftConfig(**{**FAST.__dict__, "snapshot_threshold": 25})
    c = await Cluster(tmp_path, 3, cfg).start()
    leader = await c.wait_leader()
    lagging = next(n.id for n in c.live() if n is not leader)
    await c.crash(lagging)
    for i in range(80):
        await put(leader, "k", i)
    assert leader.snapshot_index > 0 and len(leader.log) < 80, "el log debe compactarse"
    await c.restart(lagging)  # le faltan entradas ya compactadas => InstallSnapshot
    await eventually(lambda: c.sms[lagging].seq == list(range(80)), timeout=8, msg="no se instaló el snapshot")
    assert c.net.nodes[lagging].snapshot_index > 0
    await c.stop()


async def test_restart_recovers_from_snapshot_and_log(tmp_path):
    cfg = RaftConfig(**{**FAST.__dict__, "snapshot_threshold": 15})
    c = await Cluster(tmp_path, 3, cfg).start()
    leader = await c.wait_leader()
    for i in range(40):
        await put(leader, "k", i)
    await eventually(lambda: all(len(sm.seq) == 40 for sm in c.sms.values()))
    victim = next(n.id for n in c.live() if n is not leader)
    await c.crash(victim)
    await c.restart(victim)
    await eventually(lambda: c.sms[victim].seq == list(range(40)), msg="restauración desde disco falló")
    await c.stop()


async def test_concurrent_proposals_are_totally_ordered(cluster3):
    leader = await cluster3.wait_leader()
    await asyncio.gather(*(put(leader, f"k{i}", i) for i in range(100)))
    await eventually(lambda: all(len(sm.seq) == 100 for sm in cluster3.sms.values()))
    orders = [tuple(sm.seq) for sm in cluster3.sms.values()]
    assert len(set(orders)) == 1, "todas las réplicas deben aplicar el MISMO orden"
    assert sorted(orders[0]) == list(range(100))


async def test_five_nodes_tolerate_two_failures(tmp_path):
    c = await Cluster(tmp_path, 5).start()
    leader = await c.wait_leader()
    await put(leader, "k", 1)
    victims = [n.id for n in c.live() if n is not leader][:2]
    for v in victims:
        await c.crash(v)
    await put(leader, "k", 2)  # 3/5 sigue siendo mayoría
    await c.crash(next(n.id for n in c.live() if n is not leader))
    with pytest.raises((Unavailable, NotLeader)):
        await put(leader, "k", 3, timeout=0.6)  # 2/5: sin quórum
    await c.stop()


async def test_randomized_chaos_preserves_agreement(tmp_path):
    """Caídas, particiones y reinicios aleatorios con escrituras concurrentes.

    Invariante de seguridad: toda entrada que cualquier réplica aplicó es
    prefijo común de las demás (mismo orden, sin divergencias).
    """
    rnd = random.Random(7)
    c = await Cluster(tmp_path, 5).start()
    acked: list[int] = []
    counter = 0

    async def writer():
        nonlocal counter
        for _ in range(60):
            ls = c.leaders()
            if ls:
                counter += 1
                v = counter
                try:
                    await ls[0].propose({"k": "k", "v": v}, timeout=0.8)
                    acked.append(v)
                except Exception:
                    pass
            await asyncio.sleep(0.02)

    async def chaos():
        for _ in range(8):
            await asyncio.sleep(0.25)
            action = rnd.choice(["crash", "isolate", "none"])
            live_ids = [n.id for n in c.live()]
            if action == "crash" and len(live_ids) > 3:
                await c.crash(rnd.choice(live_ids))
            elif action == "isolate":
                c.net.isolate(rnd.choice(live_ids))
            await asyncio.sleep(0.4)
            c.net.heal()
            for nid in list(c.net.down):
                await c.restart(nid)

    await asyncio.gather(writer(), chaos())
    c.net.heal()
    for nid in list(c.net.down):
        await c.restart(nid)
    leader = await c.wait_leader(timeout=8)
    await put(leader, "k", 999_999)
    await eventually(
        lambda: len({tuple(sm.seq) for sm in c.sms.values()}) == 1,
        timeout=10,
        msg="réplicas divergentes: " + str({k: v.seq[-5:] for k, v in c.sms.items()}),
    )
    final = c.sms[leader.id].seq
    assert len(final) == len(set(final)), "una entrada se aplicó dos veces"
    missing = [v for v in acked if v not in final]
    assert not missing, f"escrituras CONFIRMADAS que se perdieron: {missing}"
    await c.stop()
