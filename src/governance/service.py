"""方案治理服务的应用层。

所有状态改变都以领域事件写入追加式日志，读模型由事件重放得到，因此
进程在评审期间重启后，待办（许可例外审批、仿真核查）、到期许可与候选
状态全部延续，不存在独立于日志之外、会丢失的内存状态。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from src.governance.errors import (
    ConfidentialityError,
    ConflictError,
    GovernanceError,
    LicenseConstraintError,
    NotFoundError,
    SegregationError,
    ValidationError,
)
from src.governance.hashing import content_hash
from src.governance.lineage import (
    PERMISSIONS,
    ContributionVector,
    build_review_snapshot,
    combine_inputs,
    deserialize_shares,
    effective_allowance,
    serialize_shares,
    source_key,
    verify_review_snapshot,
)
from src.governance.store import AppendOnlyStore

# 候选生命周期
ST_ACTIVE = "active"                    # 可继续派生/送审
ST_UNDER_REVIEW = "under_review"        # 已有人工评审
ST_SUSPENDED = "suspended_pending_check"  # 仿真摘要存疑，停在待查
ST_FROZEN = "production_frozen"         # 量产冻结

SOURCE_KINDS = ("material", "model", "prompt", "external")


def _require_fields(payload: Mapping[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        value = payload.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValidationError(f"缺少必填字段: {field}")


class GovernanceService:
    """事件溯源的方案治理服务。"""

    def __init__(
        self,
        store: AppendOnlyStore,
    ) -> None:
        self._store = store
        self._lock = threading.RLock()
        # ---- 读模型（全部可由事件重放重建） ----
        self.projects: dict[str, dict[str, Any]] = {}
        self.clearance: dict[str, set[str]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        # source -> [grant version ...]，按版本递增；当前版本按生效时间选取
        self.grants: dict[str, list[dict[str, Any]]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.sim_sessions: dict[str, dict[str, Any]] = {}
        self.reviews: list[dict[str, Any]] = []
        self.exceptions: dict[str, dict[str, Any]] = {}
        self.investigations: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self._replay()

    # ------------------------------------------------------------------
    # 事件回放
    # ------------------------------------------------------------------
    def _replay(self) -> None:
        for event in self._store.replay():
            self._apply(event["type"], event["payload"], event["ts"], replay=True)

    def _record(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = self._store.append(event_type, payload)
        self._apply(event_type, payload, event["ts"], replay=False)
        return event

    def _apply(self, event_type: str, p: dict[str, Any], ts: int, *, replay: bool) -> None:  # noqa: ARG002
        if event_type == "ProjectCreated":
            self.projects[p["project_id"]] = {
                "project_id": p["project_id"],
                "name": p["name"],
                "confidential": p["confidential"],
                "owner_team": p["owner_team"],
            }
            self.clearance[p["project_id"]] = set(p["clearance_teams"]) | {p["owner_team"]}
        elif event_type == "ClearanceGranted":
            self.clearance[p["project_id"]].add(p["team_id"])
        elif event_type == "ArtifactRegistered":
            self.artifacts[p["source"]] = {
                "source": p["source"],
                "kind": p["kind"],
                "project_id": p["project_id"],
                "identifier": p["identifier"],
                "title": p["title"],
                "content_hash": p["content_hash"],
                "registered_by": p["registered_by"],
            }
            self.grants[p["source"]] = [_freeze_grant(p["grant"], version=1)]
        elif event_type == "LicenseNarrowed":
            history = self.grants[p["source"]]
            new_version = len(history) + 1
            grant = _freeze_grant(p["grant"], version=new_version, supersedes=history[-1]["version"])
            history.append(grant)
        elif event_type == "CandidateGenerated":
            self._upsert_candidate(p, parents=[])
        elif event_type == "CandidateRefined":
            self._upsert_candidate(p, parents=[(p["parent"], 1)])
        elif event_type == "CandidatesMerged":
            self._upsert_candidate(p, parents=list(p["parent_weights"].items()))
        elif event_type == "ReviewRecorded":
            self.reviews.append(p)
            candidate = self.candidates[p["candidate_id"]]
            if candidate["status"] == ST_ACTIVE:
                candidate["status"] = ST_UNDER_REVIEW
        elif event_type == "SimulationRecorded":
            self.sim_sessions[p["session_key"]] = {
                "session_key": p["session_key"],
                "device_id": p["device_id"],
                "session_id": p["session_id"],
                "candidate_id": p["candidate_id"],
                "params_hash": p["params_hash"],
                "summary_hash": p["summary_hash"],
                "version": 1,
                "receipt_event": p["receipt_id"],
            }
        elif event_type == "SimulationAmended":
            session = self.sim_sessions[p["session_key"]]
            session["version"] += 1
            session["params_hash"] = p["params_hash"]
            session["summary_hash"] = p["summary_hash"]
            self.investigations[p["investigation_id"]] = {
                "investigation_id": p["investigation_id"],
                "session_key": p["session_key"],
                "affected_candidates": list(p["affected_candidates"]),
                "status": "open",
                "opened_at": ts,
                "closed_at": None,
                "resolution": None,
            }
            for candidate_id, previous_status in p["previous_status"].items():
                candidate = self.candidates[candidate_id]
                candidate["status_before_suspension"] = previous_status
                candidate["status"] = ST_SUSPENDED
        elif event_type == "InvestigationResolved":
            investigation = self.investigations[p["investigation_id"]]
            investigation["status"] = "resolved"
            investigation["closed_at"] = ts
            investigation["resolution"] = p["resolution"]
            # 仍被其他未关闭核查覆盖的候选保持待查。
            still_suspect = {
                candidate_id
                for other in self.investigations.values()
                if other["status"] == "open"
                for candidate_id in other["affected_candidates"]
            }
            for candidate_id in investigation["affected_candidates"]:
                if candidate_id in still_suspect:
                    continue
                candidate = self.candidates[candidate_id]
                if candidate["status"] == ST_SUSPENDED:
                    candidate["status"] = candidate["status_before_suspension"]
        elif event_type == "LicenseExceptionRequested":
            self.exceptions[p["exception_id"]] = {**p, "status": "requested", "decided_by": None, "decided_at": None}
        elif event_type == "LicenseExceptionDecided":
            exception = self.exceptions[p["exception_id"]]
            exception["status"] = p["decision"]
            exception["decided_by"] = p["decided_by"]
            exception["decided_at"] = ts
        elif event_type == "ProductionFrozen":
            self.decisions[p["candidate_id"]] = p
            self.candidates[p["candidate_id"]]["status"] = ST_FROZEN

    def _upsert_candidate(
        self,
        p: Mapping[str, Any],
        *,
        parents: list[tuple[str, int]],
    ) -> None:
        candidate = {
            "candidate_id": p["candidate_id"],
            "project_id": p["project_id"],
            "content_hash": p["content_hash"],
            "label": p.get("label", p["candidate_id"]),
            "shares": deserialize_shares(p["shares"]),
            "parents": dict(parents),
            "direct_sources": list(p.get("direct_sources", ())),
            "created_by": p["created_by"],
            "created_at_event": p.get("generated_at_event"),
            "status": ST_ACTIVE,
            "status_before_suspension": ST_ACTIVE,
            "kind": p.get("kind", "candidate"),
        }
        self.candidates[p["candidate_id"]] = candidate

    # ------------------------------------------------------------------
    # 时间与访问控制
    # ------------------------------------------------------------------
    @property
    def now(self) -> int:
        """与事件日志共用同一时钟源，保证时间判定与事件顺序一致。"""
        return self._store.now

    def _require_access(self, project_id: str, viewer: Mapping[str, str]) -> dict[str, Any]:
        project = self.projects.get(project_id)
        allowed_teams = self.clearance.get(project_id, set())
        # 机密项目对无密级团队一律报“不存在或无权访问”，不确认项目存在。
        if project is None or viewer["team_id"] not in allowed_teams:
            raise ConfidentialityError("对象不存在或无权访问")
        return project

    def _owner_viewer(self, project_id: str) -> dict[str, str]:
        """内部链路计算时以属主团队身份读取，不改变任何权限判定。"""
        return {"user_id": "_system_", "team_id": self.projects[project_id]["owner_team"]}

    def _candidate_project(self, candidate_id: str, viewer: Mapping[str, str]) -> dict[str, Any]:
        candidate = self.candidates.get(candidate_id)
        # 候选不存在与无密级访问返回同一错误，不泄露候选（及机密项目）的存在。
        if candidate is None:
            raise ConfidentialityError("对象不存在或无权访问")
        self._require_access(candidate["project_id"], viewer)
        return candidate

    # ------------------------------------------------------------------
    # 项目与密级
    # ------------------------------------------------------------------
    def create_project(
        self,
        *,
        project_id: str,
        name: str,
        owner_team: str,
        confidential: bool,
        viewer: Mapping[str, str],
        clearance_teams: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        _require_fields({"project_id": project_id, "name": name, "owner_team": owner_team},
                        ("project_id", "name", "owner_team"))
        if project_id in self.projects:
            raise ConflictError(f"项目已存在: {project_id}")
        return self._record("ProjectCreated", {
            "project_id": project_id,
            "name": name,
            "owner_team": owner_team,
            "confidential": confidential,
            "clearance_teams": sorted(set(clearance_teams)),
            "created_by": viewer["user_id"],
        })

    def grant_clearance(self, project_id: str, team_id: str, viewer: Mapping[str, str]) -> dict[str, Any]:
        self._require_access(project_id, viewer)
        if team_id in self.clearance[project_id]:
            raise ConflictError("该团队已有密级")
        return self._record("ClearanceGranted", {
            "project_id": project_id, "team_id": team_id, "granted_by": viewer["user_id"],
        })

    # ------------------------------------------------------------------
    # 素材 / 模型 / 提示 登记与许可
    # ------------------------------------------------------------------
    def register_source(
        self,
        *,
        project_id: str,
        kind: str,
        identifier: str,
        title: str,
        content_hash_value: str,
        grant: Mapping[str, Any],
        viewer: Mapping[str, str],
    ) -> str:
        """登记素材权利来源、供应商模型或许可清晰的提示，返回来源键。

        一次登记同时写入内容指纹与首版许可条款，二者在同一事件、同一
        版本记录中绑定，避免“素材在、许可不在”的脱节。
        """
        self._require_access(project_id, viewer)
        if kind not in SOURCE_KINDS:
            raise ValidationError(f"来源类型非法: {kind}")
        _require_fields(
            {"identifier": identifier, "title": title, "content_hash": content_hash_value},
            ("identifier", "title", "content_hash"),
        )
        grant = _validate_grant(grant)
        source = source_key(kind, identifier)
        if source in self.artifacts:
            raise ConflictError(f"来源已登记: {source}")
        self._record("ArtifactRegistered", {
            "source": source,
            "kind": kind,
            "project_id": project_id,
            "identifier": identifier,
            "title": title,
            "content_hash": content_hash_value,
            "registered_by": viewer["user_id"],
            "grant": _freeze_grant(grant, version=1),
        })
        return source

    def narrow_license(
        self,
        source: str,
        *,
        grant: Mapping[str, Any],
        viewer: Mapping[str, str],
    ) -> dict[str, Any]:
        """收窄许可：新版本权限只能是旧版本的子集。

        旧版本原样保留并继续支撑旧评审快照；新版本对收窄之后的新派生
        立即生效。
        """
        artifact = self.artifacts.get(source)
        if artifact is None:
            raise NotFoundError(f"来源不存在: {source}")
        self._require_access(artifact["project_id"], viewer)
        new_grant = _validate_grant(grant)
        if new_grant["effective_from"] > self.now:
            raise ValidationError("许可收窄必须立即生效，不能预定未来时间")
        current = self.current_grant(source)
        if not set(new_grant["allows"]) <= set(current["allows"]):
            raise ValidationError("许可只能收窄，不能增加权限（如需放开请走许可例外）")
        return self._record("LicenseNarrowed", {"source": source, "grant": new_grant})

    def current_grant(self, source: str) -> dict[str, Any]:
        history = self.grants.get(source)
        if not history:
            raise NotFoundError(f"来源未登记许可: {source}")
        now = self.now
        effective = [g for g in history if g["effective_from"] <= now]
        grant = effective[-1] if effective else history[0]
        if grant["expires_at"] is not None and grant["expires_at"] <= now:
            # 已到期：权限不再授予，但条款版本仍可用于旧评审核验。
            expired = dict(grant)
            expired["allows"] = []
            expired["expired"] = True
            return expired
        return grant

    def grant_version(self, source: str, version: int) -> dict[str, Any]:
        """读取指定历史版本的许可条款（旧评审复核用）。"""
        history = self.grants.get(source)
        if not history:
            raise NotFoundError(f"来源未登记许可: {source}")
        for grant in history:
            if grant["version"] == version:
                return grant
        raise NotFoundError(f"许可版本不存在: {source}@v{version}")

    def due_licenses(self, within: int = 0) -> list[dict[str, Any]]:
        """列出已到期或将在 ``within`` 时间窗内到期的当前许可。"""
        now = self.now
        result = []
        for source, history in self.grants.items():
            grant = self.current_grant(source)
            if grant.get("expired"):
                result.append({"source": source, "grant_id": grant["grant_id"],
                               "expires_at": grant["expires_at"], "state": "expired"})
            elif grant["expires_at"] is not None and grant["expires_at"] <= now + within:
                result.append({"source": source, "grant_id": grant["grant_id"],
                               "expires_at": grant["expires_at"], "state": "due_soon"})
        return sorted(result, key=lambda item: item["expires_at"])

    # ------------------------------------------------------------------
    # 候选：生成 / 设计师修改（派生）/ 合并
    # ------------------------------------------------------------------
    def generate_candidate(
        self,
        *,
        project_id: str,
        candidate_id: str,
        model: str,
        prompt: str,
        materials: Mapping[str, str] | None = None,
        content_hash_value: str,
        label: str | None = None,
        viewer: Mapping[str, str],
    ) -> dict[str, Any]:
        """记录一次人工智能生成：模型 + 提示 + 素材共同构成叶子来源。"""
        self._require_access(project_id, viewer)
        self._require_unused_candidate(candidate_id)
        self._validate_leaf_sources(project_id, [model, prompt, *(materials or {})])
        weights = {source: int(weight) for source, weight in (materials or {}).items()}
        return self._create_candidate(
            "CandidateGenerated",
            project_id=project_id,
            candidate_id=candidate_id,
            content_hash_value=content_hash_value,
            label=label,
            viewer=viewer,
            direct=[(model, 1), (prompt, 1), *weights.items()],
            parents=[],
            extra={"model": model, "prompt": prompt, "materials": sorted(weights)},
            required_permission="derive",
        )

    def refine_candidate(
        self,
        *,
        candidate_id: str,
        parent: str,
        content_hash_value: str,
        viewer: Mapping[str, str],
        new_sources: Mapping[str, str] | None = None,
        label: str | None = None,
        change_note: str = "",
    ) -> dict[str, Any]:
        """记录设计师的人工修改，谱系完整继承父代并可加入新来源。"""
        parent_candidate = self._candidate_project(parent, viewer)
        self._require_unused_candidate(candidate_id)
        direct = list((new_sources or {}).items())
        self._validate_leaf_sources(
            parent_candidate["project_id"],
            [source for source, _ in direct],
        )
        return self._create_candidate(
            "CandidateRefined",
            project_id=parent_candidate["project_id"],
            candidate_id=candidate_id,
            content_hash_value=content_hash_value,
            label=label,
            viewer=viewer,
            direct=[(source, int(weight)) for source, weight in direct],
            parents=[parent],
            extra={"parent": parent, "change_note": change_note,
                   "kind": "designer_revision"},
            required_permission="derive",
        )

    def merge_candidates(
        self,
        *,
        project_id: str,
        candidate_id: str,
        parent_weights: Mapping[str, int],
        content_hash_value: str,
        viewer: Mapping[str, str],
        label: str | None = None,
    ) -> dict[str, Any]:
        """合并候选：许可取交集、义务取并集，贡献按权重分摊且来源去重。"""
        self._require_access(project_id, viewer)
        self._require_unused_candidate(candidate_id)
        if len(parent_weights) < 2:
            raise ValidationError("合并至少需要两个父代候选")
        for parent_id in parent_weights:
            parent = self.candidates.get(parent_id)
            if parent is None or parent["project_id"] != project_id:
                raise NotFoundError(f"父代候选不存在: {parent_id}")
            self._assert_candidate_usable(parent, "merge")
        return self._create_candidate(
            "CandidatesMerged",
            project_id=project_id,
            candidate_id=candidate_id,
            content_hash_value=content_hash_value,
            label=label,
            viewer=viewer,
            direct=[],
            parents=list(parent_weights.keys()),
            parent_weights={k: int(v) for k, v in parent_weights.items()},
            extra={"parent_weights": {k: int(v) for k, v in parent_weights.items()}},
            required_permission="merge",
        )

    def _create_candidate(
        self,
        event_type: str,
        *,
        project_id: str,
        candidate_id: str,
        content_hash_value: str,
        label: str | None,
        viewer: Mapping[str, str],
        direct: list[tuple[str, int]],
        parents: list[str],
        required_permission: str,
        extra: Mapping[str, Any],
        parent_weights: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        _require_fields({"candidate_id": candidate_id, "content_hash": content_hash_value},
                        ("candidate_id", "content_hash"))
        for parent_id in parents:
            self._assert_candidate_usable(self.candidates[parent_id], required_permission)

        weighted_parents: list[tuple[ContributionVector, int]] = []
        if parents:
            if parent_weights is None:
                parent_weights = {parent_id: 1 for parent_id in parents}
            total_weight = sum(parent_weights.values())
            if total_weight <= 0:
                raise ValidationError("父代权重之和必须为正")
            for parent_id in parents:
                parent = self.candidates[parent_id]
                weighted_parents.append((parent["shares"], parent_weights[parent_id]))
        try:
            shares = combine_inputs(
                direct_sources=[(source, Fraction(int(weight))) for source, weight in direct],
                weighted_parents=weighted_parents,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        # 新派生立即按当前许可受限（收窄/到期立刻生效）。
        allowance = self.allowance_for_shares(project_id, shares)
        if required_permission not in allowance["allows"]:
            raise LicenseConstraintError(
                f"当前许可不允许 {required_permission}："
                f"被 {allowance['restricted_by'].get(required_permission)} 限制"
            )

        payload: dict[str, Any] = {
            "project_id": project_id,
            "candidate_id": candidate_id,
            "label": label or candidate_id,
            "content_hash": content_hash_value,
            "shares": serialize_shares(shares),
            "direct_sources": sorted({source for source, _ in direct}),
            "created_by": viewer["user_id"],
            "license_at_creation": {
                "allows": sorted(allowance["allows"]),
                "obligations": sorted(allowance["obligations"]),
            },
            **extra,
        }
        event = self._record(event_type, payload)
        return {"event": event, "candidate": self.candidates[candidate_id],
                "shares": serialize_shares(shares), "allowance": _json_allowance(allowance)}

    def _validate_leaf_sources(self, project_id: str, sources: list[str]) -> None:
        for source in sources:
            artifact = self.artifacts.get(source)
            if artifact is None or artifact["project_id"] != project_id:
                raise NotFoundError(f"来源不存在: {source}")

    def _require_unused_candidate(self, candidate_id: str) -> None:
        if candidate_id in self.candidates:
            raise ConflictError(f"候选标识已存在: {candidate_id}")

    def _assert_candidate_usable(self, candidate: Mapping[str, Any], permission: str) -> None:
        if candidate["status"] == ST_SUSPENDED:
            raise LicenseConstraintError(f"候选 {candidate['candidate_id']} 停在待查，禁止继续派生")
        if candidate["status"] == ST_FROZEN:
            raise LicenseConstraintError(f"候选 {candidate['candidate_id']} 已量产冻结")
        allowance = self.allowance_for(
            candidate["candidate_id"], self._owner_viewer(candidate["project_id"])
        )
        if permission not in allowance["allows"]:
            raise LicenseConstraintError(
                f"候选 {candidate['candidate_id']} 的当前许可缺少 {permission}："
                f"被 {allowance['restricted_by'].get(permission)} 限制"
            )

    # ------------------------------------------------------------------
    # 许可计算与许可例外（职责分离）
    # ------------------------------------------------------------------
    def allowance_for_shares(self, project_id: str, shares: Mapping[str, Any]) -> dict[str, Any]:
        grants, obligations = {}, {}
        for source in shares:
            grant = self.current_grant(source)
            grants[source] = set(grant["allows"])
            obligations[source] = frozenset(grant.get("obligations", ()))
        return effective_allowance(dict(shares), grants, obligations)

    def allowance_for(self, candidate_id: str, viewer: Mapping[str, str]) -> dict[str, Any]:
        candidate = self._candidate_project(candidate_id, viewer)
        return self.allowance_for_shares(candidate["project_id"], candidate["shares"])

    def request_license_exception(
        self,
        *,
        exception_id: str,
        candidate_id: str,
        permissions: tuple[str, ...] | list[str],
        reason: str,
        expires_at: int,
        viewer: Mapping[str, str],
    ) -> dict[str, Any]:
        """提交人发起许可例外申请，产生待办，等待*他人*批准。"""
        candidate = self._candidate_project(candidate_id, viewer)
        _require_fields({"exception_id": exception_id, "reason": reason}, ("exception_id", "reason"))
        if exception_id in self.exceptions:
            raise ConflictError(f"例外申请已存在: {exception_id}")
        invalid = set(permissions) - set(PERMISSIONS)
        if invalid:
            raise ValidationError(f"未知权限: {sorted(invalid)}")
        if not permissions:
            raise ValidationError("例外至少包含一项权限")
        if expires_at <= self.now:
            raise ValidationError("例外到期时间必须晚于当前时间")
        return self._record("LicenseExceptionRequested", {
            "exception_id": exception_id,
            "project_id": candidate["project_id"],
            "candidate_id": candidate_id,
            "permissions": sorted(permissions),
            "reason": reason,
            "requested_by": viewer["user_id"],
            "expires_at": expires_at,
        })

    def decide_license_exception(
        self,
        exception_id: str,
        *,
        decision: str,
        viewer: Mapping[str, str],
        note: str = "",
    ) -> dict[str, Any]:
        """批准或驳回许可例外；提交人不得批准自己的申请。"""
        exception = self.exceptions.get(exception_id)
        if exception is None:
            raise NotFoundError(f"例外申请不存在: {exception_id}")
        self._require_access(exception["project_id"], viewer)
        if exception["status"] != "requested":
            raise ConflictError("该例外申请已有决定")
        if decision not in ("approved", "rejected"):
            raise ValidationError("决定必须是 approved 或 rejected")
        # 职责分离：提交方案/申请的人不能批准自己的许可例外。
        if exception["requested_by"] == viewer["user_id"]:
            raise SegregationError("许可例外必须由提交人之外的人员批准")
        return self._record("LicenseExceptionDecided", {
            "exception_id": exception_id,
            "decision": decision,
            "decided_by": viewer["user_id"],
            "note": note,
        })

    def _approved_exceptions(self, candidate_id: str) -> list[dict[str, Any]]:
        now = self.now
        return [
            exception for exception in self.exceptions.values()
            if exception["candidate_id"] == candidate_id
            and exception["status"] == "approved"
            and exception["expires_at"] > now
        ]

    # ------------------------------------------------------------------
    # 人工评审（多目标、机器建议 vs 人工选择、快照）
    # ------------------------------------------------------------------
    def record_review(
        self,
        *,
        review_id: str,
        candidate_id: str,
        reviewer: Mapping[str, str],
        machine_suggestions: Mapping[str, Any] | None = None,
        human_decision: str,
        tradeoffs: list[Mapping[str, Any]] | None = None,
        rights_confirmation: str = "confirmed",
        note: str = "",
    ) -> dict[str, Any]:
        """记录人工评审并固化“当时资料”快照。

        快照包含候选内容指纹、贡献向量指纹与每个来源当时的许可版本；
        日后许可收窄，旧评审仍可凭快照按当时版本复核。
        """
        candidate = self._candidate_project(candidate_id, reviewer)
        if candidate["status"] == ST_SUSPENDED:
            raise LicenseConstraintError("候选停在待查，不能形成正式评审")
        if any(existing["review_id"] == review_id for existing in self.reviews):
            raise ConflictError(f"评审已存在: {review_id}")
        _require_fields({"human_decision": human_decision}, ("human_decision",))
        for tradeoff in tradeoffs or ():
            if tradeoff.get("kind") not in ("engineering", "aesthetic"):
                raise ValidationError("取舍类型必须是 engineering 或 aesthetic")
            if not tradeoff.get("choice"):
                raise ValidationError("取舍必须记录人工选择")

        terms_at_review = {}
        for source in candidate["shares"]:
            # 用当前版本号回查条款原文，避免到期裁剪把快照里的权限清空。
            current = self.current_grant(source)
            grant = self.grant_version(source, current["version"])
            terms_at_review[source] = {
                "grant_id": grant["grant_id"],
                "version": grant["version"],
                "terms_hash": grant["terms_hash"],
                "allows": set(grant["allows"]),
                "expires_at": grant["expires_at"],
            }
        snapshot = build_review_snapshot(
            candidate_hash=candidate["content_hash"],
            shares=candidate["shares"],
            grant_terms=terms_at_review,
        )
        payload = {
            "review_id": review_id,
            "project_id": candidate["project_id"],
            "candidate_id": candidate_id,
            "reviewer": reviewer["user_id"],
            "machine_suggestions": dict(machine_suggestions or {}),
            "human_decision": human_decision,
            "tradeoffs": list(tradeoffs or []),
            "rights_confirmation": rights_confirmation,
            "note": note,
            "snapshot": snapshot,
        }
        event = self._record("ReviewRecorded", payload)
        return {"event": event, "review": payload}

    def verify_review(self, review_id: str, viewer: Mapping[str, str]) -> dict[str, Any]:
        """按评审发生时的许可版本复核旧评审。"""
        review = next((item for item in self.reviews if item["review_id"] == review_id), None)
        if review is None:
            raise NotFoundError(f"评审不存在: {review_id}")
        candidate = self._candidate_project(review["candidate_id"], viewer)
        self._verify_review_snapshot(review)
        return {"review_id": review_id, "verified": True,
                "grant_versions": {
                    source: recorded["version"]
                    for source, recorded in review["snapshot"]["grants"].items()
                }}

    # ------------------------------------------------------------------
    # 仿真回执：幂等重传 + 摘要变化待查
    # ------------------------------------------------------------------
    def record_simulation(
        self,
        *,
        device_id: str,
        session_id: str,
        candidate_id: str,
        params: Mapping[str, Any],
        summary: Mapping[str, Any] | str,
        viewer: Mapping[str, str],
    ) -> dict[str, Any]:
        """记录仿真设备回执。

        同一设备 + 同一会话为幂等键：

        * 参数与摘要完全一致的重传：不新增任何记录，返回既有回执；
        * 摘要（或参数）变化：会话升版，该候选及其全部后代停在待查，
          开出核查待办，核查关闭前不得继续派生或量产。
        """
        candidate = self._candidate_project(candidate_id, viewer)
        _require_fields({"device_id": device_id, "session_id": session_id},
                        ("device_id", "session_id"))
        params_hash = content_hash(params)
        summary_hash = content_hash(summary)
        session_key = f"{device_id}|{session_id}"

        existing = self.sim_sessions.get(session_key)
        if existing is not None:
            if existing["candidate_id"] != candidate_id:
                raise ConflictError("同一会话不能绑定到不同候选")
            if existing["params_hash"] == params_hash and existing["summary_hash"] == summary_hash:
                # 重传相同会话：只记录一次。
                return {"deduped": True, "receipt": existing, "affected_candidates": []}
            # 已量产冻结的版本是不可变的历史决定，不回改其状态；
            # 其余相关候选（含全部后代）停在待查。
            affected = [
                affected_id
                for affected_id in [candidate_id, *self.descendants(candidate_id)]
                if self.candidates[affected_id]["status"] != ST_FROZEN
            ]
            previous_status: dict[str, str] = {}
            for affected_id in affected:
                affected_candidate = self.candidates[affected_id]
                # 叠加核查时保留最初被挂起之前的状态。
                previous_status[affected_id] = (
                    affected_candidate["status_before_suspension"]
                    if affected_candidate["status"] == ST_SUSPENDED
                    else affected_candidate["status"]
                )
            investigation_id = f"inv-{session_key}-v{existing['version'] + 1}"
            event = self._record("SimulationAmended", {
                "session_key": session_key,
                "device_id": device_id,
                "session_id": session_id,
                "candidate_id": candidate_id,
                "params_hash": params_hash,
                "summary_hash": summary_hash,
                "previous_summary_hash": existing["summary_hash"],
                "affected_candidates": affected,
                "previous_status": previous_status,
                "investigation_id": investigation_id,
                "reported_by": viewer["user_id"],
            })
            return {"deduped": False, "amended": True, "event": event,
                    "affected_candidates": affected,
                    "investigation_id": investigation_id}

        receipt_id = f"sim-{session_key}-v1"
        event = self._record("SimulationRecorded", {
            "receipt_id": receipt_id,
            "session_key": session_key,
            "device_id": device_id,
            "session_id": session_id,
            "project_id": candidate["project_id"],
            "candidate_id": candidate_id,
            "params": dict(params),
            "params_hash": params_hash,
            "summary": summary if isinstance(summary, dict) else {"text": summary},
            "summary_hash": summary_hash,
            "reported_by": viewer["user_id"],
        })
        return {"deduped": False, "amended": False, "event": event,
                "receipt_id": receipt_id, "affected_candidates": []}

    def resolve_investigation(
        self,
        investigation_id: str,
        *,
        resolution: str,
        viewer: Mapping[str, str],
    ) -> dict[str, Any]:
        investigation = self.investigations.get(investigation_id)
        if investigation is None:
            raise NotFoundError(f"核查任务不存在: {investigation_id}")
        candidate = self._candidate_project(investigation["affected_candidates"][0], viewer)
        if investigation["status"] != "open":
            raise ConflictError("核查任务已关闭")
        _require_fields({"resolution": resolution}, ("resolution",))
        return self._record("InvestigationResolved", {
            "investigation_id": investigation_id,
            "project_id": candidate["project_id"],
            "resolution": resolution,
            "resolved_by": viewer["user_id"],
        })

    def descendants(self, candidate_id: str) -> list[str]:
        """返回以该候选为祖先的全部后代（传递闭包，不含自身）。"""
        children: dict[str, list[str]] = {}
        for cid, candidate in self.candidates.items():
            for parent in candidate["parents"]:
                children.setdefault(parent, []).append(cid)
        result: list[str] = []
        stack = list(children.get(candidate_id, ()))
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.append(current)
            stack.extend(children.get(current, ()))
        return sorted(result)

    # ------------------------------------------------------------------
    # 待办（重启后由事件重放延续）
    # ------------------------------------------------------------------
    def open_todos(self, viewer: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        """开放待办：许可例外、仿真核查、到期/临期许可。

        不传 ``viewer`` 时为后台运维视图（返回全部项目）；传入访问者时
        只返回其有密级的项目的待办，避免待办队列泄露机密项目的存在。
        """
        def _visible(project_id: str) -> bool:
            if viewer is None:
                return True
            return viewer["team_id"] in self.clearance.get(project_id, set())

        todos: list[dict[str, Any]] = []
        for exception in self.exceptions.values():
            if exception["status"] == "requested" and _visible(exception["project_id"]):
                todos.append({
                    "kind": "license_exception",
                    "id": exception["exception_id"],
                    "project_id": exception["project_id"],
                    "candidate_id": exception["candidate_id"],
                    "summary": f"许可例外待批准：{','.join(exception['permissions'])}",
                    "opened_by": exception["requested_by"],
                    "expires_at": exception["expires_at"],
                })
        for investigation in self.investigations.values():
            if investigation["status"] != "open":
                continue
            project_id = self.candidates[investigation["affected_candidates"][0]]["project_id"]
            if not _visible(project_id):
                continue
            todos.append({
                "kind": "simulation_investigation",
                "id": investigation["investigation_id"],
                "project_id": project_id,
                "summary": "仿真摘要变化，候选停在待查",
                "affected_candidates": investigation["affected_candidates"],
                "opened_at": investigation["opened_at"],
            })
        for item in self.due_licenses():
            project_id = self.artifacts[item["source"]]["project_id"]
            if not _visible(project_id):
                continue
            todos.append({
                "kind": "license_expiry",
                "id": f"due-{item['source']}",
                "project_id": project_id,
                "source": item["source"],
                "state": item["state"],
                "expires_at": item["expires_at"],
                "summary": ("许可已到期，新派生立即受限" if item["state"] == "expired"
                            else "许可即将到期"),
            })
        return todos

    # ------------------------------------------------------------------
    # 量产决定 / 冻结与谱系视图
    # ------------------------------------------------------------------
    def freeze_for_production(
        self,
        *,
        candidate_id: str,
        decision: str,
        viewer: Mapping[str, str],
        rationale: str = "",
    ) -> dict[str, Any]:
        """量产冻结：确认当前许可、例外、评审与待办，固化谱系包。"""
        candidate = self._candidate_project(candidate_id, viewer)
        if decision not in ("approved", "rejected"):
            raise ValidationError("量产决定必须是 approved 或 rejected")
        if candidate["status"] == ST_FROZEN:
            raise ConflictError("该候选已经形成量产决定")
        candidate_reviews = [r for r in self.reviews if r["candidate_id"] == candidate_id]
        if decision == "approved" and not candidate_reviews:
            raise ValidationError("量产批准前至少需要一条人工评审记录")
        if candidate["status"] == ST_SUSPENDED:
            raise LicenseConstraintError("候选停在待查，不能量产")
        blocking = [
            todo for todo in self.open_todos()
            if todo["kind"] == "simulation_investigation"
            and candidate_id in todo.get("affected_candidates", [])
        ]
        if blocking:
            raise LicenseConstraintError("存在未关闭的仿真核查，不能量产")

        allowance = self.allowance_for(candidate_id, viewer)
        exceptions = self._approved_exceptions(candidate_id)
        granted_by_exception = {
            permission for exception in exceptions for permission in exception["permissions"]
        }
        missing = {"produce", "publish"} - allowance["allows"] - granted_by_exception
        if decision == "approved" and missing:
            raise LicenseConstraintError(
                f"量产并进入全球产品网络缺少权限 {sorted(missing)}，且无有效许可例外"
            )

        bundle = self.lineage(candidate_id, viewer)
        bundle_hash = content_hash({
            "candidate_id": candidate_id,
            "content_hash": candidate["content_hash"],
            "shares": serialize_shares(candidate["shares"]),
            "reviews": [r["review_id"] for r in candidate_reviews],
            "exceptions": [e["exception_id"] for e in exceptions],
            "head": self._store.head(),
        })
        event = self._record("ProductionFrozen", {
            "project_id": candidate["project_id"],
            "candidate_id": candidate_id,
            "decision": decision,
            "decided_by": viewer["user_id"],
            "rationale": rationale,
            "rights_confirmed": True,
            "bundle_hash": bundle_hash,
            "log_head": self._store.head(),
            "exceptions": [e["exception_id"] for e in exceptions],
        })
        bundle["production_decision"] = self.decisions[candidate_id]
        return {"event": event, "bundle_hash": bundle_hash, "bundle": bundle}

    def lineage(self, candidate_id: str, viewer: Mapping[str, str]) -> dict[str, Any]:
        """沿谱系给出完整视图：来源→生成/修改/合并→仿真→评审→量产。"""
        candidate = self._candidate_project(candidate_id, viewer)
        project_id = candidate["project_id"]

        related: set[str] = {candidate_id}
        stack = [candidate_id]
        while stack:
            current = self.candidates[stack.pop()]
            for parent in current["parents"]:
                if parent not in related:
                    related.add(parent)
                    stack.append(parent)
        # 视图范围：当前候选及其全部祖先（当前版本由这条完整链条形成）。
        scope = related

        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        shares_by_candidate: dict[str, dict[str, str]] = {}
        for cid in sorted(related):
            item = self.candidates[cid]
            shares_by_candidate[cid] = serialize_shares(item["shares"])
            nodes[cid] = {
                "kind": item["kind"],
                "label": item["label"],
                "content_hash": item["content_hash"],
                "status": item["status"],
                "created_by": item["created_by"],
            }
            for parent, weight in item["parents"].items():
                edges.append({"from": parent, "to": cid, "relation": "parent_of", "weight": weight})
            for source in item["direct_sources"]:
                artifact = self.artifacts[source]
                nodes.setdefault(source, {
                    "kind": artifact["kind"],
                    "label": artifact["title"],
                    "content_hash": artifact["content_hash"],
                })
                edges.append({"from": source, "to": cid, "relation": "input_of"})

        simulations = []
        for session in self.sim_sessions.values():
            if session["candidate_id"] in scope:
                simulations.append(session)
                nodes[session["session_key"]] = {"kind": "simulation", "version": session["version"]}
                edges.append({"from": session["session_key"], "to": session["candidate_id"],
                              "relation": "simulates"})

        review_view = []
        for review in self.reviews:
            if review["candidate_id"] in related:
                entry = {key: review[key] for key in (
                    "review_id", "candidate_id", "reviewer", "machine_suggestions",
                    "human_decision", "tradeoffs", "rights_confirmation", "snapshot")}
                entry["snapshot_verifiable"] = self._snapshot_still_verifiable(review)
                review_view.append(entry)
                nodes[review["review_id"]] = {"kind": "review", "reviewer": review["reviewer"]}
                edges.append({"from": review["review_id"], "to": review["candidate_id"],
                              "relation": "reviews"})

        allowance = self.allowance_for_shares(project_id, candidate["shares"])
        current_terms = {
            source: {
                "grant_id": self.current_grant(source)["grant_id"],
                "version": self.current_grant(source)["version"],
                "allows": self.current_grant(source)["allows"],
                "obligations": self.current_grant(source).get("obligations", []),
                "expires_at": self.current_grant(source)["expires_at"],
            }
            for source in candidate["shares"]
        }
        decision = self.decisions.get(candidate_id)
        return {
            "project_id": project_id,
            "candidate_id": candidate_id,
            "nodes": nodes,
            "edges": edges,
            "shares": shares_by_candidate,
            "current_license": {
                "terms": current_terms,
                "effective": _json_allowance(allowance),
                "exceptions": [
                    {
                        "exception_id": e["exception_id"],
                        "permissions": e["permissions"],
                        "requested_by": e["requested_by"],
                        "decided_by": e["decided_by"],
                        "expires_at": e["expires_at"],
                    }
                    for e in self._approved_exceptions(candidate_id)
                ],
            },
            "simulations": simulations,
            "reviews": review_view,
            "production_decision": decision,
        }

    def _snapshot_still_verifiable(self, review: Mapping[str, Any]) -> bool:
        try:
            self._verify_review_snapshot(review)
        except (GovernanceError, ValueError):
            return False
        return True

    def _verify_review_snapshot(self, review: Mapping[str, Any]) -> None:
        """无访问控制的内部复核；调用方须已完成密级校验。"""
        candidate = self.candidates[review["candidate_id"]]
        historical_terms = {
            source: self.grant_version(source, recorded["version"])
            for source, recorded in review["snapshot"]["grants"].items()
        }
        verify_review_snapshot(
            review["snapshot"],
            candidate_hash=candidate["content_hash"],
            shares=candidate["shares"],
            historical_terms=historical_terms,
        )

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def event_log(self) -> list[dict[str, Any]]:
        return self._store.events


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------
def _validate_grant(grant: Mapping[str, Any]) -> dict[str, Any]:
    required = ("grant_id", "terms_hash", "effective_from", "allows")
    _require_fields(grant, required)
    allows = set(grant["allows"])
    invalid = allows - set(PERMISSIONS)
    if invalid:
        raise ValidationError(f"许可包含未知权限: {sorted(invalid)}")
    expires_at = grant.get("expires_at")
    if expires_at is not None and expires_at <= grant["effective_from"]:
        raise ValidationError("许可到期时间必须晚于生效时间")
    return {
        "grant_id": grant["grant_id"],
        "terms_hash": grant["terms_hash"],
        "effective_from": int(grant["effective_from"]),
        "expires_at": int(expires_at) if expires_at is not None else None,
        "allows": sorted(allows),
        "obligations": sorted(set(grant.get("obligations", ()))),
        "licensor": grant.get("licensor", ""),
    }


def _freeze_grant(
    grant: Mapping[str, Any],
    *,
    version: int,
    supersedes: int | None = None,
) -> dict[str, Any]:
    frozen = dict(grant)
    frozen["version"] = version
    if supersedes is not None:
        frozen["supersedes"] = supersedes
    return frozen


def _json_allowance(allowance: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "allows": sorted(allowance["allows"]),
        "obligations": sorted(allowance["obligations"]),
        "restricted_by": dict(allowance["restricted_by"]),
    }
