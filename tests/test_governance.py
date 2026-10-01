"""方案治理服务的行为测试：谱系、许可、评审、仿真回执、机密性与重启恢复。"""

import tempfile
import unittest
from pathlib import Path

from src.governance import GovernanceService, Store
from src.governance import models

NOW = ["2026-10-01T09:00:00+00:00"]


def make_service(path: Path) -> GovernanceService:
    return GovernanceService(Store(path), clock=lambda: NOW[0])


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.svc = make_service(self.path)
        self.brief = self.svc.create_brief(
            "aero",
            "下一代轿跑气动造型",
            objectives=["风阻系数降低 8%", "保持品牌前脸特征"],
            constraints=["量产成本上限", "全球法规适配"],
            created_by="designer-li",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _asset(self, name="风洞参考图集", restrictions=None, valid_until=None):
        lic = self.svc.register_license("aero", "素材供应方", restrictions or [], valid_until)
        return self.svc.register_asset("aero", name, "供应方授权合同", lic["license_id"])

    def _model(self, restrictions=None):
        lic = self.svc.register_license("aero", "模型供应商", restrictions or [])
        return self.svc.register_model("aero", "气动智能体", "v3.2", "供应商甲", lic["license_id"])


class BriefTest(ServiceCase):
    def test_brief_requires_objectives_and_constraints(self):
        with self.assertRaisesRegex(ValueError, "任务目标不能为空"):
            self.svc.create_brief("aero", "t", [], ["c"], "x")
        with self.assertRaisesRegex(ValueError, "任务约束不能为空"):
            self.svc.create_brief("aero", "t", ["o"], [], "x")

    def test_confidential_brief_hidden_from_other_teams(self):
        secret = self.svc.create_brief(
            "aero", "机密预研", ["目标"], ["约束"], "designer-li", confidential=True
        )
        self.assertEqual([b["brief_id"] for b in self.svc.list_briefs("aero")].count(secret["brief_id"]), 1)
        self.assertNotIn(secret["brief_id"], [b["brief_id"] for b in self.svc.list_briefs("marketing")])
        # 无关团队按“不存在”处理，连存在性都不泄露
        with self.assertRaises(LookupError):
            self.svc.create_candidate(
                "marketing", secret["brief_id"], [], self._model()["model_id"], "prompt", [], "x"
            )


class CandidateTest(ServiceCase):
    def test_merge_inherits_restrictions_and_dedup_contribution(self):
        asset_a = self._asset("图集A", ["attribution-required"])
        asset_b = self._asset("图集B", ["region:eu-only"])
        model = self._model()
        c1 = self.svc.create_candidate(
            "aero", self.brief["brief_id"], [], model["model_id"], "提示记录1", [asset_a["asset_id"]], "designer-li"
        )
        # c2 从 c1 派生，又直接使用素材 A，并引入素材 B
        c2 = self.svc.create_candidate(
            "aero", self.brief["brief_id"], [c1["candidate_id"]], model["model_id"], "提示记录2",
            [asset_a["asset_id"], asset_b["asset_id"]], "designer-li",
        )
        merged = self.svc.merge_candidates(
            "aero", self.brief["brief_id"], [c1["candidate_id"], c2["candidate_id"]],
            model["model_id"], "合并提示", [], "designer-wang",
        )
        # 许可继承：两个来源限制的并集
        self.assertEqual(
            merged["inherited_restrictions"], ["attribution-required", "region:eu-only"]
        )
        # 同一来源不因多次派生重复计算：A 只计一份
        contribution = merged["contribution"]
        self.assertEqual(sorted(contribution), sorted([asset_a["asset_id"], asset_b["asset_id"]]))
        self.assertAlmostEqual(sum(v["share"] for v in contribution.values()), 1.0)
        self.assertEqual(contribution[asset_a["asset_id"]]["share"], 0.5)
        self.assertGreaterEqual(contribution[asset_a["asset_id"]]["paths"], 2)

    def test_parents_must_belong_to_same_brief(self):
        other = self.svc.create_brief("aero", "另一任务", ["o"], ["c"], "designer-li")
        model = self._model()
        c1 = self.svc.create_candidate(
            "aero", other["brief_id"], [], model["model_id"], "p", [], "designer-li"
        )
        with self.assertRaisesRegex(ValueError, "同一任务"):
            self.svc.create_candidate(
                "aero", self.brief["brief_id"], [c1["candidate_id"]], model["model_id"], "p", [], "designer-li"
            )


class LicenseTest(ServiceCase):
    def test_narrowing_keeps_old_review_verifiable_and_restricts_new_derivation(self):
        asset = self._asset("图集", [])
        model = self._model()
        license_id = asset["license_id"]
        c1 = self.svc.create_candidate(
            "aero", self.brief["brief_id"], [], model["model_id"], "p1", [asset["asset_id"]], "designer-li"
        )
        self.svc.submit_for_review("aero", c1["candidate_id"], "designer-li")
        review = self.svc.submit_review(
            "aero", c1["candidate_id"], "reviewer-zhao", "approve", engineering_notes="风阻达标"
        )
        self.assertTrue(self.svc.verify_review(review["review_id"]))

        # 许可收窄：旧评审仍按当时资料可验证
        self.svc.narrow_license("aero", license_id, ["no-export"])
        self.assertTrue(self.svc.verify_review(review["review_id"]))

        # 新派生立即受限
        c2 = self.svc.create_candidate(
            "aero", self.brief["brief_id"], [c1["candidate_id"]], model["model_id"], "p2", [], "designer-li"
        )
        self.assertIn("no-export", c2["inherited_restrictions"])

    def test_license_cannot_be_loosened(self):
        lic = self.svc.register_license("aero", "供应方", ["no-export"], "2027-01-01")
        with self.assertRaisesRegex(ValueError, "只能收窄"):
            self.svc.narrow_license("aero", lic["license_id"], [])
        with self.assertRaisesRegex(ValueError, "只能缩短"):
            self.svc.narrow_license("aero", lic["license_id"], ["no-export"], "2028-01-01")

    def test_expired_license_blocks_derivation_until_exception_approved_by_other(self):
        asset = self._asset("过期图集", [], valid_until="2026-09-01")
        model = self._model()
        with self.assertRaises(PermissionError):
            self.svc.create_candidate(
                "aero", self.brief["brief_id"], [], model["model_id"], "p", [asset["asset_id"]], "designer-li"
            )
        exception = self.svc.request_license_exception(
            "aero", asset["license_id"], self.brief["brief_id"], "designer-li", "续约流程中"
        )
        # 提交方案的人不能批准自己的许可例外
        with self.assertRaises(PermissionError):
            self.svc.decide_license_exception("aero", exception["exception_id"], "designer-li", True)
        self.svc.decide_license_exception("aero", exception["exception_id"], "legal-sun", True)
        candidate = self.svc.create_candidate(
            "aero", self.brief["brief_id"], [], model["model_id"], "p", [asset["asset_id"]], "designer-li"
        )
        self.assertEqual(candidate["state"], models.DRAFT)


class SimulationTest(ServiceCase):
    def test_retransmission_recorded_once_and_digest_change_quarantines(self):
        model = self._model()
        c1 = self.svc.create_candidate(
            "aero", self.brief["brief_id"], [], model["model_id"], "p", [], "designer-li"
        )
        params = {"风速": 120, "攻角": 0}
        run = self.svc.record_simulation("aero", "sim-session-1", [c1["candidate_id"]], params, "digest-v1")
        again = self.svc.record_simulation("aero", "sim-session-1", [c1["candidate_id"]], params, "digest-v1")
        self.assertIs(run, again)
        self.assertEqual(len(self.svc.store.data["simulations"]), 1)

        # 摘要变化：相关候选停入待查，不能评审
        self.svc.submit_for_review("aero", c1["candidate_id"], "designer-li")
        self.svc.record_simulation("aero", "sim-session-1", [c1["candidate_id"]], params, "digest-v2")
        candidate = self.svc.store.data["candidates"][c1["candidate_id"]]
        self.assertEqual(candidate["state"], models.QUARANTINED)
        with self.assertRaisesRegex(ValueError, "待查"):
            self.svc.submit_review("aero", c1["candidate_id"], "reviewer-zhao", "approve")
        # 排查后恢复
        self.svc.resolve_quarantine("aero", c1["candidate_id"], "reviewer-zhao")
        self.assertEqual(
            self.svc.store.data["candidates"][c1["candidate_id"]]["state"], models.IN_REVIEW
        )


class RestartTest(ServiceCase):
    def test_restart_preserves_todos_and_lineage(self):
        expiring = self.svc.register_license("aero", "供应方", [], "2026-10-20")
        self.svc.register_license("aero", "供应方", [], "2028-01-01")
        model = self._model()
        c1 = self.svc.create_candidate(
            "aero", self.brief["brief_id"], [], model["model_id"], "p", [], "designer-li"
        )
        self.svc.submit_for_review("aero", c1["candidate_id"], "designer-li")

        # 服务重启：从同一文件恢复
        reopened = make_service(self.path)
        todos = reopened.pending_todos("aero")
        self.assertEqual([c["candidate_id"] for c in todos["reviews"]], [c1["candidate_id"]])
        self.assertEqual(
            [l["license_id"] for l in todos["expiring_licenses"]], [expiring["license_id"]]
        )
        lineage = reopened.lineage("aero", c1["candidate_id"])
        self.assertEqual(lineage["chain"][0]["candidate_id"], c1["candidate_id"])


class ProductionTest(ServiceCase):
    def test_full_flow_freezes_and_lineage_tells_the_story(self):
        asset = self._asset("图集A", ["attribution-required"])
        model = self._model()
        c1 = self.svc.create_candidate(
            "aero", self.brief["brief_id"], [], model["model_id"], "提示记录1", [asset["asset_id"]], "designer-li"
        )
        self.svc.record_simulation("aero", "s1", [c1["candidate_id"]], {"风速": 120}, "d1")
        self.svc.submit_for_review("aero", c1["candidate_id"], "designer-li")
        self.svc.submit_review(
            "aero", c1["candidate_id"], "reviewer-zhao", "approve",
            engineering_notes="风阻系数达标", aesthetic_notes="保留品牌前脸",
        )
        decision = self.svc.record_production_decision(
            "aero", c1["candidate_id"], "reviewer-qian", True, "满足量产条件"
        )
        self.assertEqual(
            self.svc.store.data["candidates"][c1["candidate_id"]]["state"], models.FROZEN
        )

        lineage = self.svc.lineage("aero", c1["candidate_id"])
        self.assertEqual(lineage["brief"]["objectives"][0], "风阻系数降低 8%")
        self.assertEqual(lineage["chain"][0]["model"]["version"], "v3.2")
        self.assertEqual(lineage["reviews"][0]["engineering_notes"], "风阻系数达标")
        self.assertEqual(lineage["reviews"][0]["aesthetic_notes"], "保留品牌前脸")
        self.assertEqual(lineage["decisions"][0]["decision_id"], decision["decision_id"])
        self.assertEqual(lineage["simulations"][0]["session_id"], "s1")
        self.assertEqual(
            lineage["chain"][0]["contribution"][asset["asset_id"]]["share"], 1.0
        )

    def test_production_requires_approved_candidate(self):
        model = self._model()
        c1 = self.svc.create_candidate(
            "aero", self.brief["brief_id"], [], model["model_id"], "p", [], "designer-li"
        )
        with self.assertRaisesRegex(ValueError, "尚未通过评审"):
            self.svc.record_production_decision("aero", c1["candidate_id"], "reviewer-qian", True, "r")


if __name__ == "__main__":
    unittest.main()
