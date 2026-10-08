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

import threading
from collections import OrderedDict
from datetime import datetime

from sqlalchemy.orm import Session

from app.agent.state import MAX_HISTORY_MESSAGES
from app.models.user import User
from app.models.user_memory import UserMemory

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
        history.append(
            {"role": "user", "content": question, "time": _now()}
        )
        history.append(
            {"role": "assistant", "content": answer, "time": _now()}
        )
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


def clear_history(conversation_id: str) -> None:
    with _sessions_lock:
        _sessions.pop(conversation_id, None)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------


def load_memory_context(db: Session, user_id: int, limit: int = 8) -> str:
    """读出用户偏好，拼成一段文本注入提示词。

    同时累加 hit_count —— 这个字段是后续做「冷记忆淘汰」的依据。
    """
    items = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id)
        .order_by(UserMemory.hit_count.desc(), UserMemory.id.desc())
        .limit(limit)
        .all()
    )
    if not items:
        return "（暂无历史偏好记录，这是该用户第一次使用）"

    for item in items:
        item.hit_count = (item.hit_count or 0) + 1
    db.commit()

    return "\n".join(f"- {item.content}" for item in items)


def remember(db: Session, user_id: int, content: str, memory_type: str = "preference") -> UserMemory | None:
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
        try:
            hour = int(start_time.split(":")[0])
        except (ValueError, IndexError):
            hour = None
        if hour is not None:
            if hour < 12:
                period = "上午"
            elif hour < 18:
                period = "下午"
            else:
                period = "晚上"
            learned.append(f"习惯预约时段：{period}（约 {start_time} 开始）")

    for text in learned:
        remember(db, user.id, text, memory_type="habit")
    return learned
