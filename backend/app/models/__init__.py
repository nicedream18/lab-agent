"""统一在这里导入所有模型。

原因：`Base.metadata.create_all()` 只能建「已经注册到 metadata 上」的表，
而表是在「模型类被 import 的那一刻」才注册的。之前在 main.py 里是靠
`import app.api` 的副作用间接触发模型导入，一旦某个 api 模块没被引用，
对应的表就会被静默漏建。

集中导入后，只要 `import app.models` 就能保证 metadata 完整，
新建表时也不用再去追调用链。
"""

from app.models.agent_trace import AgentTrace
from app.models.equipment import Equipment
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.models.user import User
from app.models.user_memory import UserMemory

__all__ = [
    "AgentTrace",
    "Equipment",
    "Lab",
    "Reservation",
    "User",
    "UserMemory",
]
