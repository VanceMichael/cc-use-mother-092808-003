"""方案治理服务的记录构造与校验。

所有记录都是可 JSON 序列化的字典，持久化与重启恢复不依赖对象身份。
"""

from __future__ import annotations

from typing import Any

# 候选状态
DRAFT = "draft"  # 草稿
IN_REVIEW = "in_review"  # 评审中
QUARANTINED = "quarantined"  # 待查（仿真回执冲突等原因）
APPROVED = "approved"  # 评审通过
REJECTED = "rejected"  # 评审否决
FROZEN = "frozen"  # 量产冻结

REVIEW_DECISIONS = ("approve", "reject", "note")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _non_empty_strings(values: Any, message: str) -> None:
    _require(
        isinstance(values, list)
        and len(values) > 0
        and all(isinstance(v, str) and v.strip() for v in values),
        message,
    )


def make_brief(
    brief_id: str,
    title: str,
    objectives: list[str],
    constraints: list[str],
    team: str,
    confidential: bool,
    created_by: str,
    created_at: str,
) -> dict[str, Any]:
    """任务目标与约束，是一切候选的根。"""
    _require(bool(title.strip()), "任务标题不能为空")
    _non_empty_strings(objectives, "任务目标不能为空")
    _non_empty_strings(constraints, "任务约束不能为空")
    _require(bool(team.strip()), "责任团队不能为空")
    return {
        "brief_id": brief_id,
        "title": title,
        "objectives": list(objectives),
        "constraints": list(constraints),
        "team": team,
        "confidential": bool(confidential),
        "created_by": created_by,
        "created_at": created_at,
    }


def make_license(
    license_id: str,
    owner: str,
    team: str,
    restrictions: list[str],
    valid_until: str | None,
    recorded_at: str,
) -> dict[str, Any]:
    """许可及其版本历史；history 只追加，旧版本永远可回溯。"""
    _require(bool(owner.strip()), "许可方不能为空")
    version = {
        "version": 1,
        "restrictions": sorted(set(restrictions)),
        "valid_until": valid_until,
        "recorded_at": recorded_at,
    }
    return {
        "license_id": license_id,
        "owner": owner,
        "team": team,
        "history": [version],
    }


def make_asset(
    asset_id: str,
    name: str,
    rights_source: str,
    license_id: str,
    team: str,
    created_at: str,
) -> dict[str, Any]:
    """训练素材及其权利来源。"""
    _require(bool(name.strip()), "素材名称不能为空")
    _require(bool(rights_source.strip()), "素材权利来源不能为空")
    return {
        "asset_id": asset_id,
        "name": name,
        "rights_source": rights_source,
        "license_id": license_id,
        "team": team,
        "created_at": created_at,
    }


def make_model(
    model_id: str,
    name: str,
    version: str,
    vendor: str,
    license_id: str,
    created_at: str,
) -> dict[str, Any]:
    """供应商模型版本及其许可。"""
    _require(bool(name.strip()) and bool(version.strip()), "模型名称与版本不能为空")
    return {
        "model_id": model_id,
        "name": name,
        "version": version,
        "vendor": vendor,
        "license_id": license_id,
        "created_at": created_at,
    }


def make_candidate(
    candidate_id: str,
    brief_id: str,
    parent_ids: list[str],
    model_id: str,
    prompt_ref: str,
    asset_ids: list[str],
    created_by: str,
    created_at: str,
    inherited_restrictions: list[str],
    license_snapshot: dict[str, int],
    contribution: dict[str, Any],
) -> dict[str, Any]:
    """生成候选：记录模型版本、提示内容、素材、派生关系与许可继承。"""
    _require(bool(prompt_ref.strip()), "提示内容记录不能为空")
    return {
        "candidate_id": candidate_id,
        "brief_id": brief_id,
        "parent_ids": list(parent_ids),
        "model_id": model_id,
        "prompt_ref": prompt_ref,
        "asset_ids": list(asset_ids),
        "created_by": created_by,
        "created_at": created_at,
        "state": DRAFT,
        "state_before_quarantine": None,
        "inherited_restrictions": sorted(set(inherited_restrictions)),
        "license_snapshot": dict(license_snapshot),
        "contribution": contribution,
    }


def make_simulation(
    session_id: str,
    candidate_ids: list[str],
    params: dict[str, Any],
    summary_digest: str,
    recorded_at: str,
) -> dict[str, Any]:
    """仿真回执；同一 session_id 幂等，摘要冲突记入 conflicts。"""
    _non_empty_strings(candidate_ids, "仿真必须关联候选")
    _require(bool(summary_digest.strip()), "仿真摘要不能为空")
    return {
        "session_id": session_id,
        "candidate_ids": list(candidate_ids),
        "params": dict(params),
        "summary_digest": summary_digest,
        "recorded_at": recorded_at,
        "conflicts": [],
    }


def make_review(
    review_id: str,
    candidate_id: str,
    reviewer: str,
    decision: str,
    engineering_notes: str,
    aesthetic_notes: str,
    license_snapshot: dict[str, int],
    restrictions_at_review: list[str],
    created_at: str,
) -> dict[str, Any]:
    """人工评审：记录工程与审美取舍，并快照评审当时的许可版本。"""
    _require(decision in REVIEW_DECISIONS, "评审结论无效")
    return {
        "review_id": review_id,
        "candidate_id": candidate_id,
        "reviewer": reviewer,
        "decision": decision,
        "engineering_notes": engineering_notes,
        "aesthetic_notes": aesthetic_notes,
        "license_snapshot": dict(license_snapshot),
        "restrictions_at_review": sorted(set(restrictions_at_review)),
        "created_at": created_at,
    }


def make_exception(
    exception_id: str,
    license_id: str,
    brief_id: str,
    requester: str,
    reason: str,
    created_at: str,
) -> dict[str, Any]:
    """许可例外申请；批准人不得与申请人为同一人。"""
    _require(bool(reason.strip()), "例外理由不能为空")
    return {
        "exception_id": exception_id,
        "license_id": license_id,
        "brief_id": brief_id,
        "requester": requester,
        "reason": reason,
        "status": "pending",
        "approver": None,
        "created_at": created_at,
        "decided_at": None,
    }


def make_decision(
    decision_id: str,
    candidate_id: str,
    reviewer: str,
    approved: bool,
    rationale: str,
    created_at: str,
) -> dict[str, Any]:
    """量产决定。"""
    _require(bool(rationale.strip()), "量产决定理由不能为空")
    return {
        "decision_id": decision_id,
        "candidate_id": candidate_id,
        "reviewer": reviewer,
        "approved": bool(approved),
        "rationale": rationale,
        "created_at": created_at,
    }
