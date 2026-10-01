"""方案治理服务（Design Proposal Governance Service）。

为研发中心提供人工智能辅助设计方案的统一版本登记与谱系治理：

- 素材权利来源、供应商模型许可、提示内容、生成候选、仿真参数与回执、
  人工评审与修改、派生/合并关系、量产决定共享同一版本记录；
- 候选合并时继承最严许可并按路径分摊贡献，同一来源经多次派生不重复计算；
- 机密项目对无密级团队不可见；提交人不得批准自己的许可例外；
- 许可收窄对旧评审保持可验证（按当时快照），对新派生立即生效；
- 仿真设备重传同一会话只记录一次；摘要变化时关联候选进入待查；
- 服务重启后待办与到期许可延续；量产冻结输出完整谱系视图。
"""

from src.governance.errors import (
    ConflictError,
    ConfidentialityError,
    GovernanceError,
    LicenseConstraintError,
    NotFoundError,
    SegregationError,
)
from src.governance.store import AppendOnlyStore
from src.governance.service import GovernanceService

__all__ = [
    "AppendOnlyStore",
    "ConfidentialityError",
    "ConflictError",
    "GovernanceError",
    "GovernanceService",
    "LicenseConstraintError",
    "NotFoundError",
    "SegregationError",
]
