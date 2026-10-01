"""设计谱系的纯算法：来源标识、贡献分摊、许可继承、快照校验。

设计要点
--------

* **来源去重**：每个素材权利来源、供应商模型或提示只有一个稳定来源键
  （如 ``"material:m-01"``）。候选的贡献向量只保存“叶子来源”的份额，
  因此 A→B→C 连续派生时，A 不会因为同时出现在 B 的直接输入和 B 的
  传递谱系里而被计算两次。

* **合并分摊**：合并把父代向量按权重加权求和后归一化；同一来源经多条
  路径汇入时份额相加（这是它应得的总贡献），但仍只是一个来源条目。

* **许可继承**：候选的有效许可 = 全部来源的权限交集 ∧ 限制/义务并集。
  收窄后的许可只影响收窄之后新建的派生；旧候选与旧评审携带当时快照。

所有份额用 :class:`fractions.Fraction` 精确表示，序列化为 ``"p/q"`` 文本，
避免浮点归一化在审计时产生误差。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from src.governance.hashing import content_hash

# 规范权限词汇；交集运算依赖这一固定集合。
PERMISSIONS: tuple[str, ...] = ("use", "derive", "merge", "produce", "publish")
SHARE_ONE = "1"

ContributionVector = dict[str, Fraction]
ShareVector = dict[str, str]


def source_key(kind: str, identifier: str) -> str:
    """生成叶子来源的稳定键。"""
    if kind not in {"material", "model", "prompt", "external"}:
        raise ValueError(f"未知来源类型: {kind}")
    return f"{kind}:{identifier}"


def serialize_shares(vector: ContributionVector) -> ShareVector:
    """把分数向量序列化为可入日志的字符串映射。"""
    return {key: str(value) for key, value in sorted(vector.items())}


def deserialize_shares(raw: Mapping[str, str]) -> ContributionVector:
    """从事件负载还原分数向量。"""
    return {key: Fraction(value) for key, value in raw.items()}


def combine_inputs(
    direct_sources: Iterable[tuple[str, Fraction]] = (),
    weighted_parents: Sequence[tuple[ContributionVector, Fraction]] = (),
) -> ContributionVector:
    """把直接来源与父代谱系合并为归一化贡献向量。

    ``direct_sources`` 为本步骤直接引入的叶子来源及其权重；
    ``weighted_parents`` 为父代候选的（贡献向量, 权重）。
    结果所有权重之和归一化为 1；同一来源跨路径出现只保留一个键，
    权重累加，绝不复制成多条来源。
    """
    total: ContributionVector = {}
    weight_sum = Fraction(0)

    for key, weight in direct_sources:
        if weight <= 0:
            raise ValueError("来源权重必须为正")
        total[key] = total.get(key, Fraction(0)) + weight
        weight_sum += weight

    for parent_vector, weight in weighted_parents:
        if weight <= 0:
            raise ValueError("父代权重必须为正")
        if not parent_vector:
            raise ValueError("父代谱系没有任何叶子来源")
        parent_total = sum(parent_vector.values())
        for key, share in parent_vector.items():
            total[key] = total.get(key, Fraction(0)) + weight * (share / parent_total)
        weight_sum += weight

    if weight_sum == 0:
        raise ValueError("候选至少需要一个来源")
    return {key: value / weight_sum for key, value in total.items()}


def equal_weights(keys: Iterable[str]) -> list[tuple[str, Fraction]]:
    """为一组来源生成等权输入。"""
    keys = list(keys)
    if not keys:
        raise ValueError("至少需要一个直接来源")
    share = Fraction(1, len(keys))
    return [(key, share) for key in keys]


def effective_allowance(
    shares: ContributionVector,
    source_grants: Mapping[str, set[str]],
    source_obligations: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Any]:
    """计算候选的有效许可。

    :param shares: 候选的归一化贡献向量（键即叶子来源）。
    :param source_grants: 每个来源当前授予的权限集合。
    :param source_obligations: 每个来源当前承担的义务/限制标签。
    :returns: ``allows``（交集）、``obligations``（并集）以及每个被收窄
        权限的约束来源 ``restricted_by``，供谱系视图解释限制从何而来。
    """
    if not shares:
        raise ValueError("空谱系无法计算许可")
    sources = set(shares)
    missing = sources - set(source_grants)
    if missing:
        raise ValueError(f"来源缺少许可登记: {sorted(missing)}")

    allows = set(PERMISSIONS)
    for source in sources:
        allows &= source_grants[source]

    obligations: set[str] = set()
    for source in sources:
        if source_obligations and source in source_obligations:
            obligations.update(source_obligations[source])

    restricted_by: dict[str, list[str]] = {}
    for permission in PERMISSIONS:
        if permission not in allows:
            blockers = sorted(s for s in sources if permission not in source_grants[s])
            if blockers:
                restricted_by[permission] = blockers

    return {
        "allows": allows,
        "obligations": obligations,
        "restricted_by": restricted_by,
    }


def shares_fingerprint(shares: Mapping[str, Fraction | str]) -> str:
    """贡献向量的内容指纹，评审快照用它锁定当时的分摊结果。"""
    normalized = {
        key: str(value if isinstance(value, Fraction) else Fraction(value))
        for key, value in sorted(shares.items())
    }
    return content_hash(normalized)


def build_review_snapshot(
    *,
    candidate_hash: str,
    shares: Mapping[str, Fraction],
    grant_terms: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """构造评审时刻的可验证快照。

    ``grant_terms`` 以来源键为键，值为该来源当时生效的许可版本信息
    （grant_id / version / terms_hash / allows / expires_at）。
    快照写入评审事件后永不改变，日后许可收窄也能按当时资料复核。
    """
    allows_snapshot = {
        source: {
            "grant_id": terms["grant_id"],
            "version": terms["version"],
            "terms_hash": terms["terms_hash"],
            "allows": sorted(terms["allows"]),
            "expires_at": terms.get("expires_at"),
        }
        for source, terms in sorted(grant_terms.items())
    }
    return {
        "candidate_hash": candidate_hash,
        "shares_hash": shares_fingerprint(shares),
        "grants": allows_snapshot,
    }


def verify_review_snapshot(
    snapshot: Mapping[str, Any],
    *,
    candidate_hash: str,
    shares: Mapping[str, Fraction],
    historical_terms: Mapping[str, Mapping[str, Any]],
) -> None:
    """按“当时资料”复核旧评审；任一项不符即抛出 :class:`ValueError`。

    ``historical_terms`` 必须提供快照中记录的那个许可版本（而不是最新
    版本），从而保证收窄之后旧评审依旧可验证。
    """
    if snapshot["candidate_hash"] != candidate_hash:
        raise ValueError("候选内容与评审快照不一致")
    if snapshot["shares_hash"] != shares_fingerprint(shares):
        raise ValueError("贡献分摊与评审快照不一致")

    recorded = snapshot["grants"]
    if set(recorded) != {str(k) for k in shares}:
        raise ValueError("快照来源集合与谱系不一致")
    for source, recorded_terms in recorded.items():
        terms = historical_terms.get(source)
        if terms is None:
            raise ValueError(f"来源 {source} 的历史许可版本已无法核验")
        for field in ("grant_id", "version", "terms_hash"):
            if recorded_terms[field] != terms[field]:
                raise ValueError(f"来源 {source} 的{field}与快照不一致")
        if set(recorded_terms["allows"]) != set(terms["allows"]):
            raise ValueError(f"来源 {source} 的权限与快照不一致")
