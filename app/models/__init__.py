"""ORM 模型包:集中导入全部核心表,使其注册到 app.core.db.Base.metadata。

`import app.models` 即触发全部模型注册,init_db() / 测试 db fixture 的 create_all
以及 Alembic autogenerate(env.py 导入本包)据此感知全部表结构。
"""

from app.models.note_metric import NoteMetric, NoteMetricDaily
from app.models.operator import Operator, OperatorAccountAccess
from app.models.psych_glossary import PsychGlossary
from app.models.publish_job import PublishJob
from app.models.upload_batch import UploadBatch
from app.models.video_job import VideoJob
from app.models.xhs_account import XhsAccount

__all__ = [
    "Operator",
    "OperatorAccountAccess",
    "XhsAccount",
    "PublishJob",
    "NoteMetric",
    "NoteMetricDaily",
    "UploadBatch",
    "VideoJob",
    "PsychGlossary",
]
