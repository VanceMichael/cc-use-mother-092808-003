"""治理服务的领域错误类型。"""

from __future__ import annotations


class GovernanceError(Exception):
    """所有治理规则冲突的基类。"""


class NotFoundError(GovernanceError):
    """引用了不存在的工件或事件。"""


class ConflictError(GovernanceError):
    """版本冲突或重复提交（乐观并发 / 幂等）。"""


class ConfidentialityError(GovernanceError):
    """无密级访问机密项目，且不能以任何形式确认其存在。"""


class SegregationError(GovernanceError):
    """职责分离冲突，例如提交人批准自己的许可例外。"""


class LicenseConstraintError(GovernanceError):
    """当前许可不允许请求的派生、合并或发布动作。"""


class ValidationError(GovernanceError):
    """命令缺少必填字段或取值非法。"""
