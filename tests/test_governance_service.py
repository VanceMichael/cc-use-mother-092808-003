"""方案治理服务的场景测试。

每个测试对应题面的一项治理要求；测试只使用标准库与临时目录，
不连接任何外部业务系统。
"""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from src.governance.errors import (
    ConflictError,
    ConfidentialityError,
    LicenseConstraintError,
    SegregationError,
    ValidationError,
)
from src.governance.service import (
    ST_ACTIVE,
    ST_FROZEN,
    ST_SUSPENDED,
    ST_UNDER_REVIEW,
    GovernanceService,
)
from src.governance.store import AppendOnlyStore, Clock

ALL_PERMS = ["use", "derive", "merge", "produce", "publish"]


def grant(grant_id: str, allows: list[str], *, terms_hash: str | None = None,
          effective_from: int = 1, expires_at: int | None = None,
          obligations: tuple[str, ...] = ()) -> dict:
    return {
        "grant_id": grant_id,
        "terms_hash": terms_hash or f"terms-{grant_id}",
        "effective_from": effective_from,
        "expires_at": expires_at,
        "allows": allows,
        "obligations": list(obligations),
    }


class GovernanceFixture:
    """构造一个机密项目 + 三个叶子来源的标准环境。"""

    def __init__(self, path: str | Path | None = None, *, confidential: bool = True) -> None:
        self.clock = Clock(1)
        self.store = AppendOnlyStore(path, clock=self.clock)
        self.svc = GovernanceService(self.store)
        self.designer = {"user_id": "u-designer", "team_id": "design-center"}
        self.other_designer = {"user_id": "u-designer-2", "team_id": "design-center"}
        self.counsel = {"user_id": "u-counsel", "team_id": "legal"}
        self.production_reviewer = {"user_id": "u-prod", "team_id": "production-review"}
        self.outsider = {"user_id": "u-outsider", "team_id": "unrelated-team"}

        self.svc.create_project(
            project_id="P-X", name="机密气动造型项目", owner_team="design-center",
            confidential=confidential, viewer=self.designer,
        )
        # 法务与量产评审团队取得密级；无关团队没有。
        self.svc.grant_clearance("P-X", "legal", self.designer)
        self.svc.grant_clearance("P-X", "production-review", self.designer)

        self.material = self.svc.register_source(
            project_id="P-X", kind="material", identifier="scan-01",
            title="风洞扫描油泥模型", content_hash_value="hash-material",
            grant=grant("g-mat-1", ALL_PERMS, obligations=("署名",)),
            viewer=self.designer,
        )
        self.model = self.svc.register_source(
            project_id="P-X", kind="model", identifier="aero-model-7",
            title="供应商气动大模型 v7", content_hash_value="hash-model",
            grant=grant("g-model-1", ["use", "derive", "merge", "produce"]),
            viewer=self.designer,
        )
        self.prompt = self.svc.register_source(
            project_id="P-X", kind="prompt", identifier="prompt-reduce-drag",
            title="减阻造型提示", content_hash_value="hash-prompt",
            grant=grant("g-prompt-1", ALL_PERMS),
            viewer=self.designer,
        )


class UnifiedRecordTest(unittest.TestCase):
    """任务目标：素材权利来源、模型版本、提示、候选、仿真参数、评审、
    派生关系与量产决定必须落在同一套版本记录中。"""

    def setUp(self) -> None:
        self.fx = GovernanceFixture()
        self.svc = self.fx.svc

    def test_all_facts_share_one_hash_chained_log(self) -> None:
        fx = self.fx
        svc = fx.svc
        c1 = svc.generate_candidate(
            project_id="P-X", candidate_id="cand-1", model=fx.model, prompt=fx.prompt,
            materials={fx.material: 2}, content_hash_value="hash-cand-1", viewer=fx.designer,
        )
        svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id="cand-1",
            params={"speed_kmh": 300, "mesh": "fine"}, summary={"cd": 0.231},
            viewer=fx.designer,
        )
        svc.record_review(
            review_id="rev-1", candidate_id="cand-1", reviewer=fx.counsel,
            machine_suggestions={"drag_coefficient": "0.228（建议涡流发生器）"},
            human_decision="采纳建议，但尾部线条改由人工定稿",
            tradeoffs=[{"kind": "engineering", "choice": "保留涡流发生器",
                        "gave_up": "更短尾长"},
                       {"kind": "aesthetic", "choice": "压低尾部上挑",
                        "gave_up": "部分后轴下压力"}],
        )

        types = [event["type"] for event in svc.event_log()]
        self.assertIn("ArtifactRegistered", types)
        self.assertIn("CandidateGenerated", types)
        self.assertIn("SimulationRecorded", types)
        self.assertIn("ReviewRecorded", types)

        # 事件链可独立校验，任一记录都与前后记录绑定。
        fx.store.replay()
        # 登记时内容指纹与许可条款在同一事件内绑定。
        registered = [e for e in svc.event_log() if e["type"] == "ArtifactRegistered"]
        for event in registered:
            self.assertTrue(event["payload"]["content_hash"])
            self.assertEqual(event["payload"]["grant"]["version"], 1)

        # 候选的生成回执带回机器来源与贡献，供统一登记。
        self.assertEqual(c1["event"]["payload"]["model"], fx.model)


class LineageAndContributionTest(unittest.TestCase):
    """合并继承许可限制、按路径分摊贡献，同一来源多次派生不重复计算。"""

    def setUp(self) -> None:
        self.fx = GovernanceFixture()
        self.svc = self.fx.svc

    def test_repeated_derivation_does_not_duplicate_source(self) -> None:
        fx = self.fx
        svc = fx.svc
        svc.generate_candidate(
            project_id="P-X", candidate_id="a1", model=fx.model, prompt=fx.prompt,
            content_hash_value="h-a1", viewer=fx.designer,
        )
        svc.refine_candidate(candidate_id="a2", parent="a1",
                             content_hash_value="h-a2", viewer=fx.designer)
        svc.refine_candidate(candidate_id="a3", parent="a2",
                             content_hash_value="h-a3", viewer=fx.designer)
        shares = svc.candidates["a3"]["shares"]
        # 只有三个叶子来源；模型与提示没有因为三次派生被记成三份。
        self.assertEqual(set(shares), {fx.model, fx.prompt})
        self.assertEqual(sum(shares.values()), Fraction(1))

    def test_merge_allocates_by_weight_and_union_licenses(self) -> None:
        fx = self.fx
        svc = fx.svc
        svc.generate_candidate(
            project_id="P-X", candidate_id="a1", model=fx.model, prompt=fx.prompt,
            content_hash_value="h-a1", viewer=fx.designer,
        )
        svc.generate_candidate(
            project_id="P-X", candidate_id="b1", model=fx.model, prompt=fx.prompt,
            materials={fx.material: 1}, content_hash_value="h-b1", viewer=fx.designer,
        )
        merged = svc.merge_candidates(
            project_id="P-X", candidate_id="m1",
            parent_weights={"a1": 3, "b1": 1},
            content_hash_value="h-m1", viewer=fx.designer,
        )
        shares = svc.candidates["m1"]["shares"]
        # a1: model/prompt 各 1/2；b1: 三者各 1/3。3:1 加权后：
        self.assertEqual(shares[fx.model], Fraction(11, 24))
        self.assertEqual(shares[fx.prompt], Fraction(11, 24))
        self.assertEqual(shares[fx.material], Fraction(1, 12))
        self.assertEqual(sum(shares.values()), Fraction(1))
        # 模型许可不含 publish：合并结果继承交集，publish 被模型来源限制。
        self.assertNotIn("publish", merged["allowance"]["allows"])
        self.assertEqual(merged["allowance"]["restricted_by"]["publish"], [fx.model])
        # 素材义务（署名）随并入而继承。
        self.assertIn("署名", merged["allowance"]["obligations"])

    def test_diamond_merge_counts_shared_source_once_per_vector(self) -> None:
        fx = self.fx
        svc = fx.svc
        svc.generate_candidate(
            project_id="P-X", candidate_id="root", model=fx.model, prompt=fx.prompt,
            content_hash_value="h-root", viewer=fx.designer,
        )
        svc.refine_candidate(candidate_id="left", parent="root",
                             content_hash_value="h-left", viewer=fx.designer)
        svc.refine_candidate(candidate_id="right", parent="root",
                             content_hash_value="h-right", viewer=fx.other_designer)
        svc.merge_candidates(
            project_id="P-X", candidate_id="diamond",
            parent_weights={"left": 1, "right": 1},
            content_hash_value="h-diamond", viewer=fx.designer,
        )
        shares = svc.candidates["diamond"]["shares"]
        self.assertEqual(set(shares), {fx.model, fx.prompt})
        self.assertEqual(shares[fx.model], Fraction(1, 2))
        self.assertEqual(shares[fx.prompt], Fraction(1, 2))


class ConfidentialityTest(unittest.TestCase):
    """机密项目的存在本身不能向无关团队泄露。"""

    def setUp(self) -> None:
        self.fx = GovernanceFixture(confidential=True)
        self.svc = self.fx.svc

    def test_outsider_gets_uniform_not_found_for_any_query(self) -> None:
        fx = self.fx
        with self.assertRaises(ConfidentialityError):
            fx.svc.generate_candidate(
                project_id="P-X", candidate_id="x", model=fx.model, prompt=fx.prompt,
                content_hash_value="h", viewer=fx.outsider,
            )
        with self.assertRaises(ConfidentialityError):
            fx.svc.lineage("any-candidate", fx.outsider)
        with self.assertRaises(ConfidentialityError):
            fx.svc.register_source(
                project_id="P-X", kind="material", identifier="z", title="z",
                content_hash_value="h", grant=grant("g", ALL_PERMS), viewer=fx.outsider,
            )

    def test_cleared_team_works_normally(self) -> None:
        fx = self.fx
        result = fx.svc.generate_candidate(
            project_id="P-X", candidate_id="a1", model=fx.model, prompt=fx.prompt,
            content_hash_value="h-a1", viewer=fx.designer,
        )
        self.assertTrue(result["event"]["hash"])

    def test_todo_queue_hides_confidential_project_from_outsiders(self) -> None:
        fx = self.fx
        fx.svc.generate_candidate(
            project_id="P-X", candidate_id="a1", model=fx.model, prompt=fx.prompt,
            content_hash_value="h-a1", viewer=fx.designer,
        )
        fx.svc.request_license_exception(
            exception_id="ex-secret", candidate_id="a1", permissions=["publish"],
            reason="机密项目的待办", expires_at=100, viewer=fx.designer,
        )
        # 无密级团队的待办队列看不到该项目的任何痕迹。
        self.assertEqual(fx.svc.open_todos(fx.outsider), [])
        # 有密级的法务团队能看到待审批例外。
        self.assertTrue(any(
            t["id"] == "ex-secret" for t in fx.svc.open_todos(fx.counsel)
        ))


class SegregationTest(unittest.TestCase):
    """提交方案的人不能批准自己的许可例外。"""

    def setUp(self) -> None:
        self.fx = GovernanceFixture()
        self.svc = self.fx.svc
        fx = self.fx
        svc = fx.svc
        svc.generate_candidate(
            project_id="P-X", candidate_id="a1", model=fx.model, prompt=fx.prompt,
            content_hash_value="h-a1", viewer=fx.designer,
        )
        svc.request_license_exception(
            exception_id="ex-1", candidate_id="a1", permissions=["publish"],
            reason="进入全球产品网络送审", expires_at=100, viewer=fx.designer,
        )

    def test_submitter_cannot_approve_own_exception(self) -> None:
        with self.assertRaises(SegregationError):
            self.fx.svc.decide_license_exception("ex-1", decision="approved",
                                                 viewer=self.fx.designer)

    def test_independent_counsel_can_approve_and_it_appears_as_todo(self) -> None:
        fx = self.fx
        todo = next(t for t in fx.svc.open_todos() if t["id"] == "ex-1")
        self.assertEqual(todo["kind"], "license_exception")
        fx.svc.decide_license_exception("ex-1", decision="approved", viewer=fx.counsel)
        self.assertFalse(any(t["id"] == "ex-1" for t in fx.svc.open_todos()))
        # 批准后 publish 经例外放开，但例外到期自动失效。
        self.fx.clock.advance_to(100)
        self.assertEqual(fx.svc._approved_exceptions("a1"), [])


class LicenseNarrowingTest(unittest.TestCase):
    """许可收窄：旧评审按当时资料可验证，新派生立即受限。"""

    def setUp(self) -> None:
        self.fx = GovernanceFixture()
        fx = self.fx
        svc = fx.svc
        svc.generate_candidate(
            project_id="P-X", candidate_id="a1", model=fx.model, prompt=fx.prompt,
            content_hash_value="h-a1", viewer=fx.designer,
        )
        svc.refine_candidate(candidate_id="a2", parent="a1",
                             content_hash_value="h-a2", viewer=fx.designer)
        self.review = svc.record_review(
            review_id="rev-old", candidate_id="a2", reviewer=fx.counsel,
            machine_suggestions={"cd": "建议 0.228"},
            human_decision="保留人工尾部造型",
            tradeoffs=[],
        )

    def test_old_review_still_verifies_after_narrowing(self) -> None:
        fx = self.fx
        # 供应商收窄模型许可：只允许使用，不再允许派生/量产。
        fx.svc.narrow_license(
            fx.model, grant=grant("g-model-2", ["use"], terms_hash="terms-model-v2"),
            viewer=fx.counsel,
        )
        result = fx.svc.verify_review("rev-old", fx.counsel)
        self.assertTrue(result["verified"])
        # 复核锁定的是评审当时的 v1，而不是收窄后的 v2。
        self.assertEqual(result["grant_versions"][fx.model], 1)

    def test_new_derivation_is_immediately_blocked(self) -> None:
        fx = self.fx
        fx.svc.narrow_license(
            fx.model, grant=grant("g-model-2", ["use"], terms_hash="terms-model-v2"),
            viewer=fx.counsel,
        )
        with self.assertRaises(LicenseConstraintError):
            fx.svc.refine_candidate(
                candidate_id="a3", parent="a2",
                content_hash_value="h-a3", viewer=fx.designer,
            )

    def test_narrowing_cannot_add_permissions_or_be_scheduled_future(self) -> None:
        fx = self.fx
        with self.assertRaises(ValidationError):
            fx.svc.narrow_license(
                fx.model, grant=grant("g-model-x", ALL_PERMS + ["new_perm"]),
                viewer=fx.counsel,
            )
        with self.assertRaises(ValidationError):
            fx.svc.narrow_license(
                fx.model,
                grant=grant("g-model-future", ["use"], effective_from=999),
                viewer=fx.counsel,
            )

    def test_expired_license_blocks_new_derivation_and_shows_todo(self) -> None:
        fx = self.fx
        # 用一个会到期的素材建立候选，到期后新派生立即受限。
        expiring = fx.svc.register_source(
            project_id="P-X", kind="external", identifier="stock-2026",
            title="外部图库", content_hash_value="h-stock",
            grant=grant("g-stock", ALL_PERMS, expires_at=50), viewer=fx.designer,
        )
        fx.svc.generate_candidate(
            project_id="P-X", candidate_id="b1", model=expiring, prompt=fx.prompt,
            content_hash_value="h-b1", viewer=fx.designer,
        )
        fx.clock.advance_to(50)
        self.assertTrue(any(
            t["kind"] == "license_expiry" and t["state"] == "expired"
            and t["source"] == expiring
            for t in fx.svc.open_todos()
        ))
        with self.assertRaises(LicenseConstraintError):
            fx.svc.refine_candidate(
                candidate_id="b2", parent="b1",
                content_hash_value="h-b2", viewer=fx.designer,
            )


class SimulationReceiptTest(unittest.TestCase):
    """仿真设备重传相同会话只记录一次；摘要变化时相关候选停在待查。"""

    def setUp(self) -> None:
        self.fx = GovernanceFixture()
        fx = self.fx
        svc = fx.svc
        svc.generate_candidate(
            project_id="P-X", candidate_id="a1", model=fx.model, prompt=fx.prompt,
            content_hash_value="h-a1", viewer=fx.designer,
        )
        svc.refine_candidate(candidate_id="a2", parent="a1",
                             content_hash_value="h-a2", viewer=fx.designer)

    def _params(self) -> dict:
        return {"speed_kmh": 300, "yaw_deg": 0, "mesh": "fine"}

    def test_identical_retransmission_is_recorded_once(self) -> None:
        fx = self.fx
        svc = fx.svc
        first = svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id="a1",
            params=self._params(), summary={"cd": 0.231}, viewer=fx.designer,
        )
        events_before = len(svc.event_log())
        second = svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id="a1",
            params=self._params(), summary={"cd": 0.231}, viewer=fx.designer,
        )
        self.assertFalse(first["deduped"])
        self.assertTrue(second["deduped"])
        self.assertEqual(len(svc.event_log()), events_before)
        self.assertEqual(second["receipt"]["receipt_event"], first["receipt_id"])

    def test_summary_change_suspends_candidate_and_descendants(self) -> None:
        fx = self.fx
        svc = fx.svc
        svc.refine_candidate(candidate_id="a3", parent="a2",
                             content_hash_value="h-a3", viewer=fx.designer)
        svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id="a1",
            params=self._params(), summary={"cd": 0.231}, viewer=fx.designer,
        )
        amended = svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id="a1",
            params=self._params(), summary={"cd": 0.245}, viewer=fx.designer,
        )
        # a1 及其全部后代停在待查。
        self.assertEqual(set(amended["affected_candidates"]), {"a1", "a2", "a3"})
        for candidate_id in ("a1", "a2", "a3"):
            self.assertEqual(svc.candidates[candidate_id]["status"], ST_SUSPENDED)
        # 待查期间不能派生也不能量产。
        with self.assertRaises(LicenseConstraintError):
            svc.refine_candidate(candidate_id="a4", parent="a3",
                                 content_hash_value="h-a4", viewer=fx.designer)
        self.assertTrue(any(
            t["kind"] == "simulation_investigation"
            and t["affected_candidates"] == ["a1", "a2", "a3"]
            for t in svc.open_todos()
        ))
        # 关闭核查后恢复到挂起前状态（a1 原为 active）。
        svc.resolve_investigation(
            amended["investigation_id"],
            resolution="设备校准漂移，已重测确认 0.231", viewer=fx.counsel,
        )
        self.assertEqual(svc.candidates["a1"]["status"], ST_ACTIVE)
        self.assertFalse(any(
            t["kind"] == "simulation_investigation" for t in svc.open_todos()
        ))

    def test_second_summary_change_while_open_keeps_suspension(self) -> None:
        fx = self.fx
        svc = fx.svc
        svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id="a1",
            params=self._params(), summary={"cd": 0.231}, viewer=fx.designer,
        )
        first = svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id="a1",
            params=self._params(), summary={"cd": 0.245}, viewer=fx.designer,
        )
        # 挂起中再次变化：仍然待查，且关闭第一次核查不会提前恢复。
        second = svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id="a1",
            params=self._params(), summary={"cd": 0.240}, viewer=fx.designer,
        )
        svc.resolve_investigation(first["investigation_id"],
                                  resolution="第一次核查", viewer=fx.counsel)
        self.assertEqual(svc.candidates["a1"]["status"], ST_SUSPENDED)
        svc.resolve_investigation(second["investigation_id"],
                                  resolution="第二次核查", viewer=fx.counsel)
        self.assertEqual(svc.candidates["a1"]["status"], ST_ACTIVE)


class RestartRecoveryTest(unittest.TestCase):
    """评审期间服务重启：待办与到期许可必须延续。"""

    def test_state_todos_and_expiries_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "governance.jsonl"
            fx = GovernanceFixture(path)
            svc = fx.svc
            svc.generate_candidate(
                project_id="P-X", candidate_id="a1", model=fx.model, prompt=fx.prompt,
                content_hash_value="h-a1", viewer=fx.designer,
            )
            svc.request_license_exception(
                exception_id="ex-9", candidate_id="a1", permissions=["publish"],
                reason="重启前提交的例外", expires_at=100, viewer=fx.designer,
            )
            svc.record_simulation(
                device_id="dev-9", session_id="sess-1", candidate_id="a1",
                params={"v": 1}, summary={"cd": 0.2}, viewer=fx.designer,
            )
            # 推进逻辑时钟后再发生摘要变化，事件时间戳落在 t=40。
            fx.clock.advance_to(40)
            amended = svc.record_simulation(
                device_id="dev-9", session_id="sess-1", candidate_id="a1",
                params={"v": 1}, summary={"cd": 0.9}, viewer=fx.designer,
            )
            self.assertEqual(amended["event"]["ts"], 40)
            # 模拟重启：用同一个日志文件构造全新的存储与服务实例。
            restarted_clock = Clock(1)
            restarted_store = AppendOnlyStore(path, clock=restarted_clock)
            restarted = GovernanceService(restarted_store)
            # 时钟自动对齐到最后一条历史事件之后，不会倒退。
            self.assertGreaterEqual(restarted_clock(), 40)
            candidate = restarted.candidates["a1"]
            self.assertEqual(candidate["status"], ST_SUSPENDED)
            kinds = {todo["id"]: todo["kind"] for todo in restarted.open_todos()}
            self.assertEqual(kinds["ex-9"], "license_exception")
            self.assertIn(amended["investigation_id"], kinds)
            # 待办在重启后仍可被处理：法务批准例外、关闭核查。
            restarted.decide_license_exception("ex-9", decision="approved",
                                               viewer=fx.counsel)
            inv = next(t for t in restarted.open_todos()
                       if t["kind"] == "simulation_investigation")
            restarted.resolve_investigation(inv["id"], resolution="重测确认",
                                            viewer=fx.counsel)
            self.assertEqual(restarted.candidates["a1"]["status"], ST_ACTIVE)
            # 再次重启结果一致（重放确定性）。
            third = GovernanceService(AppendOnlyStore(path, clock=Clock(1)))
            self.assertEqual(third.candidates["a1"]["status"], ST_ACTIVE)
            self.assertFalse(any(
                t["id"] == "ex-9" and t["kind"] == "license_exception"
                for t in third.open_todos()
            ))

    def test_tampered_log_is_rejected_on_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "governance.jsonl"
            fx = GovernanceFixture(path)
            fx.svc.register_source(
                project_id="P-X", kind="external", identifier="x", title="x",
                content_hash_value="h", grant=grant("g", ALL_PERMS), viewer=fx.designer,
            )
            raw = path.read_text(encoding="utf-8").splitlines()
            import json
            event = json.loads(raw[0])
            event["payload"]["name"] = "被篡改的名称"
            raw[0] = json.dumps(event, ensure_ascii=False)
            path.write_text("\n".join(raw) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                AppendOnlyStore(path, clock=Clock(1))


class ProductionFreezeTest(unittest.TestCase):
    """量产决定与谱系终视图：机器建议、人工选择、工程/审美取舍、权属确认。"""

    def _build_approved_chain(self, fx: GovernanceFixture) -> str:
        svc = fx.svc
        svc.generate_candidate(
            project_id="P-X", candidate_id="cand-machine",
            model=fx.model, prompt=fx.prompt, materials={fx.material: 1},
            content_hash_value="h-cand-machine", viewer=fx.designer,
        )
        svc.refine_candidate(
            candidate_id="cand-human", parent="cand-machine",
            content_hash_value="h-cand-human", viewer=fx.designer,
            change_note="设计师收紧腰线并压低尾部",
        )
        svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id="cand-human",
            params={"speed_kmh": 300}, summary={"cd": 0.229, "downforce_n": 820},
            viewer=fx.designer,
        )
        # 模型许可缺 publish，由独立法务批准的例外支撑全球发布。
        svc.request_license_exception(
            exception_id="ex-pub", candidate_id="cand-human",
            permissions=["publish"], reason="全球产品网络上架",
            expires_at=500, viewer=fx.designer,
        )
        svc.decide_license_exception("ex-pub", decision="approved", viewer=fx.counsel)
        svc.record_review(
            review_id="rev-final", candidate_id="cand-human",
            reviewer=fx.production_reviewer,
            machine_suggestions={
                "drag": "采用高尾部涡流发生器（cd 0.226）",
                "styling": "延续机器生成的侧面张力线",
            },
            human_decision="有条件采纳：保留涡流发生器，尾部线条人工定稿",
            tradeoffs=[
                {"kind": "engineering", "choice": "保留涡流发生器",
                 "gave_up": "尾门制造公差更严，单件成本上升"},
                {"kind": "aesthetic", "choice": "压低尾部上挑 8mm",
                 "gave_up": "放弃机器建议的高尾部姿态"},
            ],
            rights_confirmation="confirmed",
            note="素材署名义务已纳入发布包",
        )
        return "cand-human"

    def test_freeze_requires_review_rights_and_blocks_without_permission(self) -> None:
        fx = GovernanceFixture()
        candidate_id = self._build_approved_chain(fx)
        # 删掉“有例外”的前提不可能，所以另造一个无评审、无 publish 的对照。
        with self.assertRaises(ValidationError):
            fx.svc.freeze_for_production(
                candidate_id="cand-machine", decision="approved",
                viewer=fx.production_reviewer,
            )
        result = fx.svc.freeze_for_production(
            candidate_id=candidate_id, decision="approved",
            rationale="气动与造型取舍经多目标评审确认，权属链完整",
            viewer=fx.production_reviewer,
        )
        self.assertEqual(fx.svc.candidates[candidate_id]["status"], ST_FROZEN)
        self.assertEqual(result["event"]["payload"]["rights_confirmed"], True)
        self.assertTrue(result["bundle_hash"])

    def test_lineage_bundle_tells_the_full_story(self) -> None:
        fx = GovernanceFixture()
        candidate_id = self._build_approved_chain(fx)
        fx.svc.freeze_for_production(
            candidate_id=candidate_id, decision="approved",
            rationale="通过", viewer=fx.production_reviewer,
        )
        bundle = fx.svc.lineage(candidate_id, fx.production_reviewer)

        # 谱系节点覆盖素材/模型/提示、机器候选、人工修改、仿真、评审。
        node_kinds = {node["kind"] for node in bundle["nodes"].values()}
        self.assertIn("material", node_kinds)
        self.assertIn("model", node_kinds)
        self.assertIn("prompt", node_kinds)
        self.assertIn("designer_revision", node_kinds)
        self.assertIn("simulation", node_kinds)
        self.assertIn("review", node_kinds)

        review = bundle["reviews"][0]
        self.assertIn("涡流发生器", review["machine_suggestions"]["drag"])
        self.assertTrue(review["snapshot_verifiable"])
        kinds = {t["kind"] for t in review["tradeoffs"]}
        self.assertEqual(kinds, {"engineering", "aesthetic"})
        self.assertEqual(review["rights_confirmation"], "confirmed")

        # 权属确认：当前许可、限制来源、经职责分离批准的例外同时可见。
        effective = bundle["current_license"]["effective"]
        self.assertEqual(effective["restricted_by"]["publish"], [fx.model])
        exceptions = bundle["current_license"]["exceptions"]
        self.assertEqual(len(exceptions), 1)
        self.assertEqual(exceptions[0]["exception_id"], "ex-pub")
        self.assertNotEqual(exceptions[0]["requested_by"], exceptions[0]["decided_by"])

        decision = bundle["production_decision"]
        self.assertEqual(decision["decision"], "approved")
        self.assertEqual(decision["decided_by"], fx.production_reviewer["user_id"])

    def test_open_investigation_blocks_freeze(self) -> None:
        fx = GovernanceFixture()
        candidate_id = self._build_approved_chain(fx)
        fx.svc.record_simulation(
            device_id="dev-9", session_id="sess-1", candidate_id=candidate_id,
            params={"speed_kmh": 300}, summary={"cd": 0.40}, viewer=fx.designer,
        )
        with self.assertRaises(LicenseConstraintError):
            fx.svc.freeze_for_production(
                candidate_id=candidate_id, decision="approved",
                viewer=fx.production_reviewer,
            )

    def test_frozen_candidate_is_immutable_to_further_decisions(self) -> None:
        fx = GovernanceFixture()
        candidate_id = self._build_approved_chain(fx)
        fx.svc.freeze_for_production(
            candidate_id=candidate_id, decision="approved",
            viewer=fx.production_reviewer,
        )
        with self.assertRaises(ConflictError):
            fx.svc.freeze_for_production(
                candidate_id=candidate_id, decision="rejected",
                viewer=fx.production_reviewer,
            )
        with self.assertRaises(LicenseConstraintError):
            fx.svc.refine_candidate(
                candidate_id="post-freeze", parent=candidate_id,
                content_hash_value="h-post", viewer=fx.designer,
            )


if __name__ == "__main__":
    unittest.main()
