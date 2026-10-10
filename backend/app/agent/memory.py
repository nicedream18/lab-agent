"""Agent 的记忆系统：短期（会话上下文）+ 长期（用户偏好）。

为什么要分两层？
- 短期记忆解决「这个」指代谁、上一轮约的是哪个实验室 —— 只在会话内有效。
- 长期记忆解决「这个用户一般约什么、习惯什么时段」—— 跨会话沉淀，
  让 Agent 在用户说「还是老时间」时能真的听懂。

短期记忆放在进程内存里，而不是 Redis：这是一个演示项目，
引入 Redis 会让部署复杂度翻倍，而收益只是「多实例共享」——
本项目没有多实例场景。真要多实例时，把 _sessions 换成 Redis Hash 即可，
对外接口不用变。
"""

from __future__ import annotations

import logging
import threading
from collections import Counter, OrderedDict
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.agent.state import MAX_HISTORY_MESSAGES
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.models.user import User
from app.models.user_memory import UserMemory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 短期记忆
# ---------------------------------------------------------------------------

# 最多保留多少个会话，超出后淘汰最久未使用的（LRU）。
# 不设上限的话，进程跑久了内存会被历史会话撑满。
MAX_SESSIONS = 200

_sessions: OrderedDict[str, list[dict]] = OrderedDict()
_sessions_lock = threading.Lock()


def append_turn(conversation_id: str, question: str, answer: str) -> None:
    """把一轮问答写进短期记忆。"""
    if not conversation_id:
        return
    with _sessions_lock:
        history = _sessions.get(conversation_id) or []
        history.append({"role": "user", "content": question, "time": _now()})
        history.append({"role": "assistant", "content": answer, "time": _now()})
        # 只留最近 N 条，避免无限增长
        _sessions[conversation_id] = history[-MAX_HISTORY_MESSAGES:]
        _sessions.move_to_end(conversation_id)
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)


def get_history(conversation_id: str) -> list[dict]:
    """取回会话上下文，只返回 role/content，供拼提示词。"""
    if not conversation_id:
        return []
    with _sessions_lock:
        history = _sessions.get(conversation_id)
        if not history:
            return []
        _sessions.move_to_end(conversation_id)
        return [{"role": item["role"], "content": item["content"]} for item in history]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------

# 预约状态里唯一要排除的：已取消。
# 取消过的记录恰恰代表「本来想要、后来不要了」，算进「最常用」会失真；
# 被拒绝的记录（2）保留 —— 用户确实想约那个实验室，只是当时没约上。
CANCELLED_STATUS = 3


def _hour_of(start_time: str) -> int | None:
    """从 "14:00" 里取出小时数；取不出来返回 None。"""
    try:
        return int(str(start_time).split(":")[0])
    except (ValueError, IndexError):
        return None


def _period_of(hour: int) -> str:
    """把小时数归到 上午 / 下午 / 晚上。"""
    if hour < 12:
        return "上午"
    if hour < 18:
        return "下午"
    return "晚上"


def _profile_summary(db: Session, user_id: int) -> str:
    """从真实预约记录聚合出「最常用实验室 + 习惯时段」。

    为什么不用 user_memory.hit_count 排序：那个计数每次注入都会 +1
    （它的本职是「冷记忆淘汰」），反映的是「这段偏好被读过几次」，
    而不是「这个实验室被约过几次」—— 拿它当「最常用」的判据是错的。
    「最常用」是一个**事实**，只能对 reservations 做聚合，不能靠猜列表顺序。
    """
    filters = (
        Reservation.user_id == user_id,
        Reservation.status != CANCELLED_STATUS,
    )
    total = db.query(Reservation).filter(*filters).count()
    if not total:
        return ""

    lines = [f"## 用户画像（来自 {total} 条真实预约记录）"]

    top = (
        db.query(Reservation.lab_id, func.count(Reservation.id).label("cnt"))
        .filter(*filters)
        .group_by(Reservation.lab_id)
        .order_by(func.count(Reservation.id).desc(), Reservation.lab_id.desc())
        .first()
    )
    if top is not None:
        lab = db.get(Lab, top.lab_id)
        if lab is not None:
            lines.append(f"- 最常用实验室：{lab.name}（{top.cnt} 次）")

    starts = [row[0] for row in db.query(Reservation.start_time).filter(*filters).all()]
    period_counter: Counter[str] = Counter()
    start_counter: Counter[str] = Counter()
    for start in starts:
        hour = _hour_of(start)
        if hour is None:
            continue
        period_counter[_period_of(hour)] += 1
        start_counter[str(start)] += 1

    if period_counter:
        period, count = period_counter.most_common(1)[0]
        common_start = start_counter.most_common(1)[0][0]
        lines.append(
            f"- 习惯时段：{period}（{count} 次），最常见开始时间 {common_start}"
        )

    # 只有表头、没算出任何一条有效画像时返回空串，别给模型一张空表。
    return "\n".join(lines) if len(lines) > 1 else ""


def load_memory_context(db: Session, user_id: int, limit: int = 8) -> str:
    """读出用户偏好，拼成一段文本注入提示词。

    内容分两部分：
      1. **用户画像** —— 从真实预约记录聚合出的「最常用实验室 / 习惯时段」；
      2. **已沉淀的偏好** —— user_memory 表里逐条攒下来的结论。
    画像排在前面：它是「数出来的事实」，比自由文本更可信，也正好给模型一个
    「该先提议什么」的依据（见 prompts 里「有偏好就别干问」那条规则）。

    顺带累加明细的 hit_count —— 那是后续做「冷记忆淘汰」的依据，
    ⚠️ 它**不能**拿来当「最常用」的判据，理由见 _profile_summary。
    """
    items = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id)
        .order_by(UserMemory.hit_count.desc(), UserMemory.id.desc())
        .limit(limit)
        .all()
    )

    try:
        profile = _profile_summary(db, user_id)
    except Exception:  # noqa: BLE001
        # 画像只是锦上添花，聚合失败不该让整轮对话连明细偏好都读不到。
        logger.exception("聚合用户画像失败")
        profile = ""

    if not items and not profile:
        return "（暂无历史偏好记录，这是该用户第一次使用）"

    for item in items:
        item.hit_count = (item.hit_count or 0) + 1
    db.commit()

    parts = []
    if profile:
        parts.append(profile)
    if items:
        parts.append(
            "## 已沉淀的偏好\n" + "\n".join(f"- {item.content}" for item in items)
        )
    return "\n\n".join(parts)


def remember(
    db: Session, user_id: int, content: str, memory_type: str = "preference"
) -> UserMemory | None:
    """写入一条长期记忆，内容重复时只累加计数而不重复插入。"""
    text = (content or "").strip()
    if not text:
        return None

    exists = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id, UserMemory.content == text)
        .first()
    )
    if exists:
        exists.hit_count = (exists.hit_count or 0) + 1
        db.commit()
        return exists

    item = UserMemory(user_id=user_id, memory_type=memory_type, content=text)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def learn_from_reservation(
    db: Session, user: User, lab_name: str, start_time: str
) -> list[str]:
    """预约成功后，把「偏好实验室」和「偏好时段」沉淀成长期记忆。

    这是长期记忆的主要来源 —— 不需要用户显式设置，Agent 在干活的过程中
    自己就把偏好学出来了。
    """
    learned = []
    if lab_name:
        learned.append(f"经常预约「{lab_name}」")
    if start_time:
        # 按小时分段：上午/下午/晚上，比记录精确到分钟更适合做偏好描述
        hour = _hour_of(start_time)
        if hour is not None:
            period = _period_of(hour)
            learned.append(f"习惯预约时段：{period}（约 {start_time} 开始）")

    for text in learned:
        remember(db, user.id, text, memory_type="habit")
    return learned
