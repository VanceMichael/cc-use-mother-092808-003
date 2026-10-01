"""方案治理服务：任务、素材权利、模型版本、候选谱系、评审、仿真回执与量产决定。

设计要点：
- 机密任务对无关团队连存在性都不泄露（一律按“不存在”处理）。
- 许可只追加版本、只能收窄；评审快照当时许可版本，事后可验证。
- 候选合并时继承全部来源限制，贡献按来源去重分摊，同一来源不因多次派生重复计算。
- 仿真回执按会话幂等；摘要变化把相关候选停入待查。
- 每次写操作立即落盘，重启后待办、评审与到期许可继续有效。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import models
from .store import Store


class GovernanceService:
    def __init__(self, store: Store, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    # ---------- 基础工具 ----------

    def _next_id(self, prefix: str) -> str:
        self.store.data["seq"] += 1
        return f"{prefix}-{self.store.data['seq']:06d}"

    def _today(self) -> str:
        return self.clock()[:10]

    def _event(self, kind: str, actor: str, **payload: Any) -> None:
        events = self.store.data["events"]
        events.append(
            {
                "seq": len(events) + 1,
                "kind": kind,
                "actor": actor,
                "at": self.clock(),
                "payload": payload,
            }
        )

    def _get_brief(self, brief_id: str, team: str) -> dict[str, Any]:
        brief = self.store.data["briefs"].get(brief_id)
        if brief is None or (brief["confidential"] and brief["team"] != team):
            # 机密任务对无关团队按不存在处理，不泄露存在性
            raise LookupError("任务不存在")
        return brief

    def _get_candidate(self, candidate_id: str, team: str) -> dict[str, Any]:
        candidate = self.store.data["candidates"].get(candidate_id)
        if candidate is None:
            raise LookupError("候选不存在")
        self._get_brief(candidate["brief_id"], team)
        return candidate

    def _license(self, license_id: str) -> dict[str, Any]:
        license_ = self.store.data["licenses"].get(license_id)
        if license_ is None:
            raise LookupError("许可不存在")
        return license_

    @staticmethod
    def _current_version(license_: dict[str, Any]) -> dict[str, Any]:
        return license_["history"][-1]

    def _has_approved_exception(self, license_id: str, brief_id: str) -> bool:
        return any(
            e["license_id"] == license_id and e["brief_id"] == brief_id and e["status"] == "approved"
            for e in self.store.data["exceptions"].values()
        )

    def _check_license_usable(self, license_id: str, brief_id: str) -> None:
        current = self._current_version(self._license(license_id))
        valid_until = current["valid_until"]
        if valid_until and valid_until < self._today():
            if not self._has_approved_exception(license_id, brief_id):
                raise PermissionError(f"许可 {license_id} 已到期，需要已批准的许可例外")

    # ---------- 任务目标与约束 ----------

    def create_brief(
        self,
        team: str,
        title: str,
        objectives: list[str],
        constraints: list[str],
        created_by: str,
        confidential: bool = False,
    ) -> dict[str, Any]:
        brief = models.make_brief(
            self._next_id("brief"), title, objectives, constraints, team, confidential, created_by, self.clock()
        )
        self.store.data["briefs"][brief["brief_id"]] = brief
        self._event("brief_created", created_by, brief_id=brief["brief_id"])
        self.store.save()
        return brief

    def list_briefs(self, team: str) -> list[dict[str, Any]]:
        """无关团队看不到机密任务，连存在性都不暴露。"""
        return [
            b
            for b in self.store.data["briefs"].values()
            if not b["confidential"] or b["team"] == team
        ]

    # ---------- 素材、许可与模型 ----------

    def register_license(
        self,
        team: str,
        owner: str,
        restrictions: list[str] | None = None,
        valid_until: str | None = None,
    ) -> dict[str, Any]:
        license_ = models.make_license(
            self._next_id("lic"), owner, team, restrictions or [], valid_until, self.clock()
        )
        self.store.data["licenses"][license_["license_id"]] = license_
        self._event("license_registered", team, license_id=license_["license_id"])
        self.store.save()
        return license_

    def narrow_license(
        self,
        team: str,
        license_id: str,
        restrictions: list[str],
        valid_until: str | None = None,
    ) -> dict[str, Any]:
        """许可收窄：只允许增加限制、缩短有效期；旧版本保留供历史评审验证。"""
        license_ = self._license(license_id)
        current = self._current_version(license_)
        new_restrictions = set(restrictions)
        if not new_restrictions >= set(current["restrictions"]):
            raise ValueError("许可只能收窄，不能移除既有限制")
        if valid_until and current["valid_until"] and valid_until > current["valid_until"]:
            raise ValueError("许可有效期只能缩短，不能延长")
        version = {
            "version": current["version"] + 1,
            "restrictions": sorted(new_restrictions),
            "valid_until": valid_until if valid_until is not None else current["valid_until"],
            "recorded_at": self.clock(),
        }
        license_["history"].append(version)
        self._event("license_narrowed", team, license_id=license_id, version=version["version"])
        self.store.save()
        return license_

    def register_asset(
        self, team: str, name: str, rights_source: str, license_id: str
    ) -> dict[str, Any]:
        self._license(license_id)
        asset = models.make_asset(
            self._next_id("asset"), name, rights_source, license_id, team, self.clock()
        )
        self.store.data["assets"][asset["asset_id"]] = asset
        self._event("asset_registered", team, asset_id=asset["asset_id"])
        self.store.save()
        return asset

    def register_model(
        self, team: str, name: str, version: str, vendor: str, license_id: str
    ) -> dict[str, Any]:
        self._license(license_id)
        model = models.make_model(
            self._next_id("model"), name, version, vendor, license_id, self.clock()
        )
        self.store.data["models"][model["model_id"]] = model
        self._event("model_registered", team, model_id=model["model_id"])
        self.store.save()
        return model

    # ---------- 候选与派生 ----------

    def _source_asset_paths(self, candidate: dict[str, Any]) -> dict[str, int]:
        """沿派生有向图收集来源素材及到达路径数；同一来源只计一次份额。"""
        counts: dict[str, int] = {}
        visited: set[str] = set()

        def walk(node: dict[str, Any]) -> None:
            if node["candidate_id"] in visited:
                return
            visited.add(node["candidate_id"])
            for asset_id in node["asset_ids"]:
                counts[asset_id] = counts.get(asset_id, 0) + 1
            for parent_id in node["parent_ids"]:
                walk(self.store.data["candidates"][parent_id])

        walk(candidate)
        return counts

    def _license_scope(self, candidate: dict[str, Any]) -> list[str]:
        """候选涉及的许可：全部唯一来源素材的许可加模型许可。"""
        license_ids = {
            self.store.data["assets"][asset_id]["license_id"]
            for asset_id in self._source_asset_paths(candidate)
        }
        license_ids.add(self.store.data["models"][candidate["model_id"]]["license_id"])
        return sorted(license_ids)

    def create_candidate(
        self,
        team: str,
        brief_id: str,
        parent_ids: list[str],
        model_id: str,
        prompt_ref: str,
        asset_ids: list[str],
        created_by: str,
    ) -> dict[str, Any]:
        brief = self._get_brief(brief_id, team)
        parents = [self._get_candidate(pid, team) for pid in parent_ids]
        if any(p["brief_id"] != brief_id for p in parents):
            raise ValueError("父候选必须属于同一任务")
        model = self.store.data["models"].get(model_id)
        if model is None:
            raise LookupError("模型版本不存在")
        for asset_id in asset_ids:
            if asset_id not in self.store.data["assets"]:
                raise LookupError("素材不存在")

        candidate = models.make_candidate(
            self._next_id("cand"),
            brief_id,
            parent_ids,
            model_id,
            prompt_ref,
            asset_ids,
            created_by,
            self.clock(),
            [],
            {},
            {},
        )
        # 许可到期即阻断新派生，除非已有批准的例外
        for license_id in self._license_scope(candidate):
            self._check_license_usable(license_id, brief["brief_id"])

        # 继承限制：当前许可版本的并集（收窄立即生效）
        restrictions: set[str] = set()
        snapshot: dict[str, int] = {}
        for license_id in self._license_scope(candidate):
            current = self._current_version(self._license(license_id))
            restrictions |= set(current["restrictions"])
            snapshot[license_id] = current["version"]
        candidate["inherited_restrictions"] = sorted(restrictions)
        candidate["license_snapshot"] = snapshot

        # 贡献分摊：唯一来源平分，路径数仅作透明展示，不重复计份额
        paths = self._source_asset_paths(candidate)
        share = 1.0 / len(paths) if paths else 0.0
        candidate["contribution"] = {
            asset_id: {"share": share, "paths": count} for asset_id, count in sorted(paths.items())
        }

        self.store.data["candidates"][candidate["candidate_id"]] = candidate
        kind = "candidate_merged" if len(parent_ids) > 1 else "candidate_created"
        self._event(kind, created_by, candidate_id=candidate["candidate_id"], brief_id=brief_id)
        self.store.save()
        return candidate

    def merge_candidates(
        self,
        team: str,
        brief_id: str,
        parent_ids: list[str],
        model_id: str,
        prompt_ref: str,
        asset_ids: list[str],
        created_by: str,
    ) -> dict[str, Any]:
        """合并候选：继承各分支许可限制的并集，贡献按来源去重分摊。"""
        if len(parent_ids) < 2:
            raise ValueError("合并至少需要两个父候选")
        return self.create_candidate(
            team, brief_id, parent_ids, model_id, prompt_ref, asset_ids, created_by
        )

    def submit_for_review(self, team: str, candidate_id: str, actor: str) -> dict[str, Any]:
        candidate = self._get_candidate(candidate_id, team)
        if candidate["state"] != models.DRAFT:
            raise ValueError("只有草稿可以提交评审")
        candidate["state"] = models.IN_REVIEW
        self._event("candidate_submitted", actor, candidate_id=candidate_id)
        self.store.save()
        return candidate

    # ---------- 仿真回执 ----------

    def record_simulation(
        self,
        team: str,
        session_id: str,
        candidate_ids: list[str],
        params: dict[str, Any],
        summary_digest: str,
    ) -> dict[str, Any]:
        for candidate_id in candidate_ids:
            self._get_candidate(candidate_id, team)
        existing = self.store.data["simulations"].get(session_id)
        if existing is not None:
            if existing["summary_digest"] == summary_digest and existing["candidate_ids"] == list(
                candidate_ids
            ):
                return existing  # 设备重传相同会话：只记录一次
            # 摘要变化：记录冲突并把相关候选停入待查
            existing["conflicts"].append(
                {
                    "candidate_ids": list(candidate_ids),
                    "summary_digest": summary_digest,
                    "at": self.clock(),
                }
            )
            related = set(existing["candidate_ids"]) | set(candidate_ids)
            for candidate_id in related:
                candidate = self.store.data["candidates"][candidate_id]
                if candidate["state"] != models.QUARANTINED:
                    candidate["state_before_quarantine"] = candidate["state"]
                    candidate["state"] = models.QUARANTINED
            self._event(
                "simulation_conflict", team, session_id=session_id, candidates=sorted(related)
            )
            self.store.save()
            return existing

        run = models.make_simulation(
            session_id, list(candidate_ids), params, summary_digest, self.clock()
        )
        self.store.data["simulations"][session_id] = run
        self._event("simulation_recorded", team, session_id=session_id)
        self.store.save()
        return run

    def resolve_quarantine(self, team: str, candidate_id: str, reviewer: str) -> dict[str, Any]:
        candidate = self._get_candidate(candidate_id, team)
        if candidate["state"] != models.QUARANTINED:
            raise ValueError("候选不在待查状态")
        candidate["state"] = candidate["state_before_quarantine"] or models.IN_REVIEW
        candidate["state_before_quarantine"] = None
        self._event("quarantine_resolved", reviewer, candidate_id=candidate_id)
        self.store.save()
        return candidate

    # ---------- 人工评审 ----------

    def submit_review(
        self,
        team: str,
        candidate_id: str,
        reviewer: str,
        decision: str,
        engineering_notes: str = "",
        aesthetic_notes: str = "",
    ) -> dict[str, Any]:
        candidate = self._get_candidate(candidate_id, team)
        if candidate["state"] == models.QUARANTINED:
            raise ValueError("候选处于待查状态，不能评审")
        if candidate["state"] == models.FROZEN:
            raise ValueError("候选已量产冻结，不能评审")
        # 快照评审当时的许可版本，之后许可收窄不影响本次评审的可验证性
        snapshot: dict[str, int] = {}
        restrictions: set[str] = set()
        for license_id in self._license_scope(candidate):
            current = self._current_version(self._license(license_id))
            snapshot[license_id] = current["version"]
            restrictions |= set(current["restrictions"])
        review = models.make_review(
            self._next_id("rev"),
            candidate_id,
            reviewer,
            decision,
            engineering_notes,
            aesthetic_notes,
            snapshot,
            sorted(restrictions),
            self.clock(),
        )
        self.store.data["reviews"][review["review_id"]] = review
        if decision == "approve":
            candidate["state"] = models.APPROVED
        elif decision == "reject":
            candidate["state"] = models.REJECTED
        else:
            candidate["state"] = models.IN_REVIEW
        self._event(
            "review_submitted", reviewer, candidate_id=candidate_id, decision=decision
        )
        self.store.save()
        return review

    def verify_review(self, review_id: str) -> bool:
        """按评审当时的许可版本快照重新计算限制，验证评审记录未被后续收窄影响。"""
        review = self.store.data["reviews"].get(review_id)
        if review is None:
            raise LookupError("评审不存在")
        recomputed: set[str] = set()
        for license_id, version in review["license_snapshot"].items():
            license_ = self.store.data["licenses"].get(license_id)
            if license_ is None:
                return False
            match = [v for v in license_["history"] if v["version"] == version]
            if not match:
                return False
            recomputed |= set(match[0]["restrictions"])
        return sorted(recomputed) == review["restrictions_at_review"]

    # ---------- 许可例外 ----------

    def request_license_exception(
        self, team: str, license_id: str, brief_id: str, requester: str, reason: str
    ) -> dict[str, Any]:
        self._license(license_id)
        self._get_brief(brief_id, team)
        exception = models.make_exception(
            self._next_id("exc"), license_id, brief_id, requester, reason, self.clock()
        )
        self.store.data["exceptions"][exception["exception_id"]] = exception
        self._event("exception_requested", requester, exception_id=exception["exception_id"])
        self.store.save()
        return exception

    def decide_license_exception(
        self, team: str, exception_id: str, approver: str, approve: bool
    ) -> dict[str, Any]:
        exception = self.store.data["exceptions"].get(exception_id)
        if exception is None:
            raise LookupError("许可例外不存在")
        self._get_brief(exception["brief_id"], team)
        if exception["status"] != "pending":
            raise ValueError("许可例外已有结论")
        if exception["requester"] == approver:
            raise PermissionError("提交方案的人不能批准自己的许可例外")
        exception["status"] = "approved" if approve else "rejected"
        exception["approver"] = approver
        exception["decided_at"] = self.clock()
        self._event(
            "exception_decided", approver, exception_id=exception_id, approved=approve
        )
        self.store.save()
        return exception

    # ---------- 量产决定 ----------

    def record_production_decision(
        self, team: str, candidate_id: str, reviewer: str, approved: bool, rationale: str
    ) -> dict[str, Any]:
        candidate = self._get_candidate(candidate_id, team)
        if candidate["state"] != models.APPROVED:
            raise ValueError("候选尚未通过评审，不能进入量产决定")
        # 量产前复核：所有来源许可当前仍有效，或有已批准的例外
        for license_id in self._license_scope(candidate):
            self._check_license_usable(license_id, candidate["brief_id"])
        decision = models.make_decision(
            self._next_id("dec"), candidate_id, reviewer, approved, rationale, self.clock()
        )
        self.store.data["decisions"][decision["decision_id"]] = decision
        if approved:
            candidate["state"] = models.FROZEN
        self._event(
            "production_decided", reviewer, candidate_id=candidate_id, approved=approved
        )
        self.store.save()
        return decision

    # ---------- 谱系与待办 ----------

    def _lineage_order(self, candidate_id: str) -> list[str]:
        """祖先优先的拓扑顺序。"""
        order: list[str] = []
        visited: set[str] = set()

        def walk(cid: str) -> None:
            if cid in visited:
                return
            visited.add(cid)
            for parent_id in self.store.data["candidates"][cid]["parent_ids"]:
                walk(parent_id)
            order.append(cid)

        walk(candidate_id)
        return order

    def lineage(self, team: str, candidate_id: str) -> dict[str, Any]:
        """沿谱系展示机器建议、人工选择、工程与审美取舍、权属确认如何形成当前版本。"""
        candidate = self._get_candidate(candidate_id, team)
        order = self._lineage_order(candidate_id)
        members = set(order)
        brief = self.store.data["briefs"][candidate["brief_id"]]

        chain = []
        for cid in order:
            node = self.store.data["candidates"][cid]
            model = self.store.data["models"][node["model_id"]]
            chain.append(
                {
                    "candidate_id": cid,
                    "state": node["state"],
                    "created_by": node["created_by"],
                    "created_at": node["created_at"],
                    "model": {"name": model["name"], "version": model["version"], "vendor": model["vendor"]},
                    "prompt_ref": node["prompt_ref"],
                    "parent_ids": node["parent_ids"],
                    "inherited_restrictions": node["inherited_restrictions"],
                    "license_snapshot": node["license_snapshot"],
                    "contribution": node["contribution"],
                }
            )

        reviews = [
            r for r in self.store.data["reviews"].values() if r["candidate_id"] in members
        ]
        reviews.sort(key=lambda r: r["created_at"])
        simulations = [
            s
            for s in self.store.data["simulations"].values()
            if members & set(s["candidate_ids"])
        ]
        decisions = [
            d for d in self.store.data["decisions"].values() if d["candidate_id"] == candidate_id
        ]
        events = [
            e
            for e in self.store.data["events"]
            if e["payload"].get("candidate_id") in members
            or e["payload"].get("brief_id") == brief["brief_id"]
        ]
        return {
            "brief": {
                "brief_id": brief["brief_id"],
                "title": brief["title"],
                "objectives": brief["objectives"],
                "constraints": brief["constraints"],
            },
            "chain": chain,
            "reviews": reviews,
            "simulations": simulations,
            "decisions": decisions,
            "events": events,
        }

    def pending_todos(self, team: str, within_days: int = 30) -> dict[str, Any]:
        """重启后依然有效的待办：在评审/待查候选、待决例外、临近或已到期许可。"""
        horizon = (datetime.fromisoformat(self._today()) + timedelta(days=within_days)).date()
        horizon_str = horizon.isoformat()
        visible_briefs = {b["brief_id"] for b in self.list_briefs(team)}
        candidates = [
            {
                "candidate_id": c["candidate_id"],
                "brief_id": c["brief_id"],
                "state": c["state"],
            }
            for c in self.store.data["candidates"].values()
            if c["brief_id"] in visible_briefs
            and c["state"] in (models.IN_REVIEW, models.QUARANTINED)
        ]
        exceptions = [
            e
            for e in self.store.data["exceptions"].values()
            if e["status"] == "pending" and e["brief_id"] in visible_briefs
        ]
        expiring = []
        for license_ in self.store.data["licenses"].values():
            if license_["team"] != team:
                continue
            valid_until = self._current_version(license_)["valid_until"]
            if valid_until and valid_until <= horizon_str:
                expiring.append(
                    {
                        "license_id": license_["license_id"],
                        "owner": license_["owner"],
                        "valid_until": valid_until,
                        "expired": valid_until < self._today(),
                    }
                )
        return {"reviews": candidates, "exceptions": exceptions, "expiring_licenses": expiring}
