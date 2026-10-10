"""AI 聊天记录的持久化：最近 30 天的会话可查、可选、可续聊。

前端其实已经有一份 localStorage 留档，为什么还要落库：
  1. **它是单机单浏览器的** —— 换台电脑、换个浏览器，历史就没了；
  2. **它只存「最后一次」的对话**，没有「列表」这个概念，
     而这里要的恰恰是「把这一个月聊过的会话列出来让用户挑」；
  3. **它是不可信的**（用户能改、能清），不能作为产品级的记录。

保留期 30 天（RETENTION_DAYS），过期会话连同消息**物理删除**。
为什么是删除而不是「标记过期」：这些是聊天原文，留着只是隐私负债，没有审计价值。
真正需要留痕的 agent_trace / agent_tool_execution 走各自的表，不在这条清理链上。

为什么清理要同时挂在「写路径」和「后台循环」两处：
  - 写路径（record_turn 末尾）：保证「只要还在用，保留期就是准的」，
    代价是一条走索引的 DELETE ... WHERE last_time < ?，很便宜；
  - 后台循环（run_cleanup_scan）：用户连续一个月不聊天时，写路径根本不会被触发，
    过期数据就得一直躺在库里。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.ai_conversation import AiConversation
from app.models.ai_message import AiMessage

logger = logging.getLogger(__name__)

# 保留期。改这个值只会影响之后新产生的数据，历史数据在下一次清理时才按新阈值判定。
RETENTION_DAYS = 30

# 会话标题取首轮提问的前若干字 —— 侧栏一行放得下，多了要截断
TITLE_MAX_CHARS = 40

# 一次最多返回多少条会话。前端侧栏只展示最近的一屏多一点，
# 真要有上千条会话，靠翻页去翻也没有意义，不如截断。
DEFAULT_LIST_LIMIT = 50

# 后台清理间隔：一小时一次。删除是低频动作，没必要更勤。
CLEANUP_INTERVAL_SECONDS = 3600


def _make_title(question: str) -> str:
    """把首轮提问压成一行标题。"""
    # 换行会撑破侧栏那一行，统一压成空格
    title = " ".join((question or "").split())
    if len(title) > TITLE_MAX_CHARS:
        return title[:TITLE_MAX_CHARS] + "…"
    return title


def record_turn(
    db: Session, user_id: int, conversation_id: str, question: str, answer: str
) -> None:
    """把一轮完整的问答落到聊天记录里。

    调用方只应该在**已经拿到回答**时调用（见 agent_service），
    所以这里不做「只有提问没有回答」的半个轮次 —— 半截记录铺回界面只会让人困惑。

    写失败只记日志，绝不向上抛：聊天记录是「顺带保存」的能力，
    它坏了不能让用户这一轮的回答也拿不到。
    """
    if not conversation_id:
        return

    try:
        now = datetime.now()
        conversation = (
            db.query(AiConversation)
            .filter(
                AiConversation.conversation_id == conversation_id,
                AiConversation.user_id == user_id,
            )
            .first()
        )
        if conversation is None:
            conversation = AiConversation(
                user_id=user_id,
                conversation_id=conversation_id,
                title=_make_title(question),
                message_count=0,
                last_time=now,
            )
            db.add(conversation)

        db.add(
            AiMessage(
                user_id=user_id,
                conversation_id=conversation_id,
                role="user",
                content=question,
            )
        )
        db.add(
            AiMessage(
                user_id=user_id,
                conversation_id=conversation_id,
                role="assistant",
                content=answer,
            )
        )
        # 标题只在第一次定下来：后面几轮往往很短（「那后天呢？」），
        # 拿它当标题反而认不出这是哪次对话。
        conversation.message_count = (conversation.message_count or 0) + 2
        conversation.last_time = now
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("写入 AI 聊天记录失败：conversation=%s", conversation_id)
        return

    # 顺序很重要：先把自己的 last_time 刷新成 now，再清理过期数据，
    # 否则「停了一个月又接着聊」的那条会话会在同一轮里被自己删掉。
    try:
        prune_expired(db)
    except Exception:
        db.rollback()
        logger.exception("清理过期会话失败")


def list_conversations(
    db: Session, user_id: int, limit: int = DEFAULT_LIST_LIMIT
) -> list[AiConversation]:
    """按最后活跃时间倒序列出会话。"""
    return (
        db.query(AiConversation)
        .filter(AiConversation.user_id == user_id)
        .order_by(AiConversation.last_time.desc(), AiConversation.id.desc())
        .limit(limit)
        .all()
    )


def load_conversation(
    db: Session, user_id: int, conversation_id: str
) -> tuple[AiConversation, list[AiMessage]] | None:
    """取一条会话和它的全部消息；不存在或不属于该用户时返回 None。

    必须带 user_id 过滤：conversation_id 虽然是随机的，
    但也不能仅凭猜中 ID 就读到别人的聊天原文。
    """
    conversation = (
        db.query(AiConversation)
        .filter(
            AiConversation.conversation_id == conversation_id,
            AiConversation.user_id == user_id,
        )
        .first()
    )
    if conversation is None:
        return None

    messages = (
        db.query(AiMessage)
        .filter(AiMessage.conversation_id == conversation_id)
        .order_by(AiMessage.id.asc())
        .all()
    )
    return conversation, messages


def delete_conversation(db: Session, user_id: int, conversation_id: str) -> bool:
    """删除一条会话及其全部消息。返回是否真的删到了东西。"""
    try:
        removed = (
            db.query(AiConversation)
            .filter(
                AiConversation.conversation_id == conversation_id,
                AiConversation.user_id == user_id,
            )
            .delete(synchronize_session=False)
        )
        # 消息表按 conversation_id 删，不带 user_id：会话已经验过归属了，
        # 而且消息表没有唯一约束，多一个过滤条件只会让删除计划更难看懂。
        db.query(AiMessage).filter(AiMessage.conversation_id == conversation_id).delete(
            synchronize_session=False
        )
        db.commit()
        return bool(removed)
    except Exception:
        db.rollback()
        logger.exception("删除会话失败：conversation=%s", conversation_id)
        return False


def prune_expired(db: Session, days: int = RETENTION_DAYS) -> int:
    """删除超过保留期的会话，返回被清掉的会话数。"""
    cutoff = datetime.now() - timedelta(days=days)
    expired = (
        db.query(AiConversation.conversation_id)
        .filter(AiConversation.last_time < cutoff)
        .all()
    )
    ids = [row[0] for row in expired]
    if not ids:
        return 0

    db.query(AiMessage).filter(AiMessage.conversation_id.in_(ids)).delete(
        synchronize_session=False
    )
    db.query(AiConversation).filter(AiConversation.conversation_id.in_(ids)).delete(
        synchronize_session=False
    )
    db.commit()
    return len(ids)


def _prune_in_new_session() -> int:
    """后台线程里跑一次清理。

    单独开会话而不是复用请求的 db：后台任务和请求的生命周期完全无关，
    共用一个 Session 会踩线程安全问题。
    """
    db = SessionLocal()
    try:
        return prune_expired(db)
    finally:
        db.close()


async def run_cleanup_scan() -> None:
    """后台定时清理过期会话。

    用 asyncio.to_thread 包住同步的 SQL 查询：直接调用会把事件循环钉住。
    （本项目用的是同步 SQLAlchemy，没有 async session，这是最省事的接法。）
    """
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            removed = await asyncio.to_thread(_prune_in_new_session)
            if removed:
                logger.info("已清理 %d 条超过 %d 天的历史会话", removed, RETENTION_DAYS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("后台清理历史会话失败")
