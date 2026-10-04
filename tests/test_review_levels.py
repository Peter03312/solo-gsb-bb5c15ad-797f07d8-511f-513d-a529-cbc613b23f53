"""本轮功能测试: 论文评审保障等级 (高/中/普通)。

覆盖需求:
- 会务方按论文设置等级: 提交论文编号、等级与当前资料修订号, 沿用现有会务方密钥;
- 相同等级重试不推进修订号 (幂等); 有效变更推进修订号; 未设置视为普通;
- 版本不符 409、未知或已撤回论文 404、非法等级/编号不一致 422, 拒绝不留部分变更;
- 可查询各论文等级与修订号;
- 普通分配/补位预演无法完整覆盖时: 先使完整分配总数最大, 再依次使高、中等级
  完整分配数最大, 最后沿用全局字典序; 完整可行时仍按容量比例 + 字典序优化;
- 普通分配锁定与补位已确认槽位仍按既有约束处理, 失效锁定不被优先级释放;
- 预演返回各等级已分配/未分配数量及原有诊断; 无法完整分配仍拒绝发布,
  当前发布版不变;
- 删除或撤回论文后其等级不再参与求解。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-levels-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "assignment_locks",
            "paper_review_levels", "paper_withdrawals",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution=None, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution or f"Inst-{rid}",
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid, topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics is not None else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def rev(client):
    return client.get("/meta", headers=ORG).json()["revision"]


def set_level(client, pid, level, base_revision=None):
    if base_revision is None:
        base_revision = rev(client)
    return client.post(
        f"/papers/{pid}/review-level",
        headers=ORG,
        json={"paper_id": pid, "base_revision": base_revision, "level": level},
    )


def setup(client, reviewers=("R1", "R2", "R3", "R4"), papers=("P1",)):
    for rid in reviewers:
        assert add_reviewer(client, rid).status_code == 201
    for pid in papers:
        assert add_paper(client, pid).status_code == 201


# ------------------------------------------------------------ 鉴权与校验

def test_organizer_key_required_for_levels(client):
    setup(client)
    r = client.post(
        "/papers/P1/review-level",
        json={"paper_id": "P1", "base_revision": 0, "level": "high"},
    )
    assert r.status_code == 401
    assert client.get("/papers/review-levels/all").status_code == 401
    assert client.get("/papers/P1/review-level").status_code == 401


def test_unset_level_defaults_to_normal(client):
    setup(client)
    body = client.get("/papers/P1/review-level", headers=ORG).json()
    assert body["level"] == "normal"
    assert body["explicit"] is False
    all_levels = client.get("/papers/review-levels/all", headers=ORG).json()
    assert all_levels["levels"] == {"P1": "normal"}
    assert all_levels["counts"] == {"high": 0, "medium": 0, "normal": 1}


def test_set_level_bumps_revision_and_is_queryable(client):
    setup(client, papers=("P1", "P2"))
    r = set_level(client, "P1", "high")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True
    assert body["level"] == "high"
    assert body["revision"] == rev(client)
    g = client.get("/papers/P1/review-level", headers=ORG).json()
    assert g["level"] == "high" and g["explicit"] is True
    all_levels = client.get("/papers/review-levels/all", headers=ORG).json()
    assert all_levels["levels"] == {"P1": "high", "P2": "normal"}
    assert all_levels["counts"] == {"high": 1, "medium": 0, "normal": 1}
    assert all_levels["revision"] == body["revision"]

    r2 = set_level(client, "P2", "medium")
    assert r2.json()["changed"] is True
    counts = client.get("/papers/review-levels/all", headers=ORG).json()["counts"]
    assert counts == {"high": 1, "medium": 1, "normal": 0}


def test_same_level_retry_is_idempotent(client):
    setup(client, papers=("P1", "P2"))
    r1 = set_level(client, "P1", "high").json()
    assert r1["changed"] is True
    r2 = set_level(client, "P1", "high", base_revision=rev(client))
    assert r2.status_code == 200
    assert r2.json()["changed"] is False
    assert r2.json()["revision"] == rev(client)
    # 对未设置论文提交普通也幂等 (不推进修订号、不写表)
    base = rev(client)
    r3 = set_level(client, "P2", "normal")
    assert r3.json() == {
        "ok": True, "paper_id": "P2", "level": "normal",
        "changed": False, "revision": base,
    }
    assert rev(client) == base


def test_setting_normal_after_high_is_effective_change(client):
    setup(client)
    set_level(client, "P1", "high")
    assert set_level(client, "P1", "medium", base_revision=rev(client)).json()["changed"] is True
    r = set_level(client, "P1", "normal", base_revision=rev(client))
    assert r.json()["changed"] is True
    assert r.json()["level"] == "normal"
    g = client.get("/papers/P1/review-level", headers=ORG).json()
    assert g["level"] == "normal" and g["explicit"] is False
    all_levels = client.get("/papers/review-levels/all", headers=ORG).json()
    assert all_levels["counts"] == {"high": 0, "medium": 0, "normal": 1}


def test_stale_revision_rejected_without_change(client):
    setup(client, papers=("P1", "P2"))
    stale = rev(client)
    add_paper(client, "P3")  # 推进修订号
    r = set_level(client, "P1", "high", base_revision=stale)
    assert r.status_code == 409
    levels = client.get("/papers/review-levels/all", headers=ORG).json()["levels"]
    assert levels == {"P1": "normal", "P2": "normal", "P3": "normal"}


def test_unknown_or_withdrawn_paper_rejected_404(client):
    setup(client)
    r = set_level(client, "GHOST", "high")
    assert r.status_code == 404
    assert client.get("/papers/GHOST/review-level", headers=ORG).status_code == 404
    # 撤回稿不可再设置/查询等级
    client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev(client), "reason": "一稿多投"},
    )
    assert set_level(client, "P1", "high", base_revision=rev(client)).status_code == 404
    assert client.get("/papers/P1/review-level", headers=ORG).status_code == 404


def test_invalid_level_and_path_mismatch_rejected_422(client):
    setup(client)
    r = client.post(
        "/papers/P1/review-level",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev(client), "level": "critical"},
    )
    assert r.status_code == 422
    r = client.post(
        "/papers/P1/review-level",
        headers=ORG,
        json={"paper_id": "P2", "base_revision": rev(client), "level": "high"},
    )
    assert r.status_code == 422
    # 被拒不写表、不推进修订号
    assert client.get("/papers/P1/review-level", headers=ORG).json()["level"] == "normal"


# ------------------------------------------------------------ 部分择优优先级

def test_partial_prefers_high_level_paper_over_normal(client):
    # 两名容量 1 的评审人, 两篇论文: 只能完整分配其中一篇
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_level(client, "P1", "high")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["plan"] == {"P1": ["R1", "R2"]}
    assert body["unassigned"] == ["P2"]
    lc = body["level_counts"]
    assert lc["by_level"] == {
        "high": {"assigned": 1, "unassigned": 0},
        "medium": {"assigned": 0, "unassigned": 0},
        "normal": {"assigned": 0, "unassigned": 1},
    }
    assert lc["assigned_total"] == 1 and lc["unassigned_total"] == 1


def test_partial_without_levels_keeps_lexicographic_order(client):
    # 同样只能满足一篇, 无等级设置时沿用字典序 -> 保留 P1
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert set(body["plan"]) == {"P1"}
    assert body["unassigned"] == ["P2"]


def test_partial_high_beats_medium_medium_beats_normal(client):
    # 3 篇论文只能完整分配 2 篇 (容量总计 4):
    # 若按字典序保留 P1,P2; 高=P3、中=P2 时应保留 P2,P3 (保高再保中)
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_reviewer(client, "R3", capacity=2)
    add_paper(client, "P1")
    add_paper(client, "P2")
    add_paper(client, "P3")
    set_level(client, "P3", "high")
    set_level(client, "P2", "medium", base_revision=rev(client))
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert set(body["plan"]) == {"P2", "P3"}
    assert body["unassigned"] == ["P1"]
    lc = body["level_counts"]["by_level"]
    assert lc["high"]["assigned"] == 1 and lc["high"]["unassigned"] == 0
    assert lc["medium"]["assigned"] == 1 and lc["medium"]["unassigned"] == 0
    assert lc["normal"]["assigned"] == 0 and lc["normal"]["unassigned"] == 1


def test_partial_tie_on_levels_falls_back_to_lexicographic(client):
    # 总数与高、中数量均相同 (同为普通) 时沿用全局字典序
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_level(client, "P1", "medium")
    set_level(client, "P2", "medium", base_revision=rev(client))
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert set(body["plan"]) == {"P1"}


def test_feasible_case_ignores_levels_for_optimization(client):
    # 完整可行时仍按容量比例 + 字典序, 高等级不改变方案
    add_reviewer(client, "R1", capacity=2)
    add_reviewer(client, "R2", capacity=4)
    add_reviewer(client, "R3", capacity=4)
    for pid in ("P1", "P2", "P3"):
        add_paper(client, pid)
    set_level(client, "P3", "high")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert body["max_used_capacity_ratio"] == "3/4"
    assert body["plan"] == {"P1": ["R1", "R2"], "P2": ["R2", "R3"], "P3": ["R2", "R3"]}
    lc = body["level_counts"]["by_level"]
    assert lc["high"] == {"assigned": 1, "unassigned": 0}
    assert lc["normal"] == {"assigned": 2, "unassigned": 0}


def test_level_does_not_release_invalid_lock(client):
    # P1 高等级但锁定的评审人随后被删除: 锁定失效只报错不释放, 高等级也不得替换
    setup(client, papers=("P1",))
    set_level(client, "P1", "high")
    client.post(
        "/assignment/locks",
        headers=ORG,
        json={"base_revision": rev(client), "locks": {"P1": ["R4"]}},
    )
    client.delete("/reviewers/R4", headers=ORG)
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["unassigned"] == ["P1"]
    assert "lock_reviewer_not_found" in body["diagnostics"]["P1"]["reasons"]
    assert body["plan"] == {}
    lc = body["level_counts"]["by_level"]
    assert lc["high"] == {"assigned": 0, "unassigned": 1}


def test_infeasible_publish_rejected_and_published_kept(client):
    setup(client, papers=("P1",))
    rev0 = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev0})
    set_level(client, "P1", "high")
    # 两名评审人停用 -> 不可完整分配
    for rid in ("R1", "R2", "R3", "R4"):
        client.post(
            f"/reviewers/{rid}/status",
            headers=ORG,
            json={"reviewer_id": rid, "base_revision": rev(client),
                  "active": False, "reason": "停用"},
        )
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    pub = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev(client)}
    )
    assert pub.status_code == 422
    current = client.get("/assignment", headers=ORG).json()
    assert current["serial"] == 1  # 当前发布版不变


# ------------------------------------------------------------ 补位中的等级择优

def test_backfill_partial_prefers_high_level_paper(client):
    # 发布两篇论文; P1 两名评审人确认 (补位时两槽位固定)。随后停用评审人使总余量
    # 不足以补齐 P2: 高等级的 P1 凭固定槽位保持完整, P2 无法补齐。
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_reviewer(client, "R3", capacity=2)
    add_paper(client, "P1")
    add_paper(client, "P2")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    client.post(
        "/assignment/publish", headers=ORG,
        json={"base_revision": dry["revision"]},
    )
    plan = client.get("/assignment", headers=ORG).json()["plan"]
    # 字典序+容量约束下方案为 P1=[R1,R3], P2=[R2,R3]
    assert plan["P1"] == ["R1", "R3"]
    for rid in plan["P1"]:
        d = client.post(
            "/reviewer/assignments/P1/decision",
            headers=reviewer_headers(rid),
            json={"paper_id": "P1", "decision": "confirm"},
        )
        assert d.status_code == 200
    set_level(client, "P1", "high")
    # 停用 R2: 固定负载 R1=1(满), R3=1(余 1); P2 需要 2 个槽位但只剩 1 -> 无法补齐
    client.post(
        "/reviewers/R2/status",
        headers=ORG,
        json={"reviewer_id": "R2", "base_revision": rev(client),
              "active": False, "reason": "休假"},
    )
    body = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["plan"] == {"P1": ["R1", "R3"]}
    assert body["unassigned"] == ["P2"]
    lc = body["level_counts"]["by_level"]
    assert lc["high"] == {"assigned": 1, "unassigned": 0}
    assert lc["normal"] == {"assigned": 0, "unassigned": 1}
    assert body["level_counts"]["assigned_total"] == 1


def test_backfill_priority_among_free_papers(client):
    # 两篇均无确认 (补位全部重算), 容量只够一篇: 高等级论文优先补齐
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_reviewer(client, "R3", capacity=3)
    add_paper(client, "P1")
    add_paper(client, "P2")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    client.post(
        "/assignment/publish", headers=ORG,
        json={"base_revision": dry["revision"]},
    )
    # 停用 R3 后仅剩 R1/R2 各容量 1 -> 只能补齐一篇
    client.post(
        "/reviewers/R3/status",
        headers=ORG,
        json={"reviewer_id": "R3", "base_revision": rev(client),
              "active": False, "reason": "休假"},
    )
    set_level(client, "P2", "high")
    body = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert set(body["plan"]) == {"P2"}
    assert body["unassigned"] == ["P1"]
    lc = body["level_counts"]["by_level"]
    assert lc["high"] == {"assigned": 1, "unassigned": 0}
    assert lc["normal"] == {"assigned": 0, "unassigned": 1}
    # 无法完整补位 -> 补位发布 422, 当前发布版不变
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": rev(client), "base_serial": body["serial"]},
    )
    assert bp.status_code == 422
    assert client.get("/assignment", headers=ORG).json()["serial"] == 1


def test_backfill_level_counts_on_full_feasible(client):
    add_reviewer(client, "R1")
    add_reviewer(client, "R2")
    add_reviewer(client, "R3")
    add_paper(client, "P1")
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    client.post(
        "/assignment/publish", headers=ORG,
        json={"base_revision": dry["revision"]},
    )
    set_level(client, "P1", "medium")
    body = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    lc = body["level_counts"]["by_level"]
    assert lc["medium"] == {"assigned": 1, "unassigned": 0}
    assert lc["high"] == {"assigned": 0, "unassigned": 0}
    assert lc["normal"] == {"assigned": 0, "unassigned": 0}


# ------------------------------------------------------------ 删除 / 撤回清除等级

def test_deleting_paper_clears_its_level(client):
    setup(client, papers=("P1", "P2"))
    set_level(client, "P1", "high")
    set_level(client, "P2", "medium", base_revision=rev(client))
    client.delete("/papers/P1", headers=ORG)
    levels = client.get("/papers/review-levels/all", headers=ORG).json()
    assert levels["levels"] == {"P2": "medium"}
    assert levels["counts"] == {"high": 0, "medium": 1, "normal": 0}


def test_withdrawn_paper_level_excluded_from_solving(client):
    # P1 高等级但随后撤回: 等级清除, 普通预演目标论文中不再有高等级论文
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_reviewer(client, "R3", capacity=2)
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_level(client, "P1", "high")
    set_level(client, "P2", "medium", base_revision=rev(client))
    client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev(client), "reason": "撤稿"},
    )
    body = client.post("/assignment/dry-run", headers=ORG).json()
    # 仅剩 P2, 容量充足 -> 完整可行
    assert body["feasible"] is True
    assert set(body["plan"]) == {"P2"}
    lc = body["level_counts"]["by_level"]
    assert lc["high"] == {"assigned": 0, "unassigned": 0}
    assert lc["medium"] == {"assigned": 1, "unassigned": 0}
    assert lc["normal"] == {"assigned": 0, "unassigned": 0}
    levels = client.get("/papers/review-levels/all", headers=ORG).json()
    assert "P1" not in levels["levels"]
    assert levels["levels"] == {"P2": "medium"}


def test_withdrawn_high_level_does_not_block_other_papers(client):
    # 撤回一篇高等级论文后, 剩余普通论文的部分择优回到字典序, 不保留高等级偏好
    add_reviewer(client, "R1", capacity=1)
    add_reviewer(client, "R2", capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    set_level(client, "P1", "high")
    client.post(
        "/papers/P1/withdrawal",
        headers=ORG,
        json={"paper_id": "P1", "base_revision": rev(client), "reason": "撤稿"},
    )
    add_paper(client, "P3")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    # 目标只有 P2、P3 (均普通), 字典序保留 P2
    assert set(body["plan"]) == {"P2"}
    assert body["unassigned"] == ["P3"]
