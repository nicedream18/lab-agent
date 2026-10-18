from datetime import datetime, timedelta
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import planner, tools, tracer
from app.agent.state import AgentContext
from app.models.ai_conversation import AiConversation
from app.models.ai_message import AiMessage
from app.models.agent_trace import AgentTrace
from app.models.agent_tool_execution import AgentToolExecution
from app.models.cancellation_draft import CancellationDraft
from app.models.equipment import Equipment
from app.models.reservation import Reservation
from app.models.reservation_draft import ReservationDraft
from app.services import agent_service, conversation_service, reservation_service
from app.common.exceptions import BusinessException
from app.schemas.ai import ChatRequest
from tests.sqlite_harness import db_session, entities  # noqa: F401


def _ctx(db, user):
    ctx = AgentContext(db=db, user=user, conversation_id="c1")
    ctx.emit = lambda event: ctx.events.append(event)
    ctx.events = []
    return ctx


def test_conversation_create_continue_title_count_and_owner_scope(db_session, entities):
    user, _ = entities
    long_question = "  查一下\n" + "实验室" * 20
    conversation_service.record_turn(db_session, user.id, "thread", long_question, "答复")
    conversation_service.record_turn(db_session, user.id, "thread", "继续", "第二次答复")
    row, messages = conversation_service.load_conversation(db_session, user.id, "thread")
    assert row.title == ("查一下 " + "实验室" * 20)[:40] + "…"
    assert row.message_count == 4
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert conversation_service.load_conversation(db_session, user.id + 1, "thread") is None
    assert conversation_service.list_conversations(db_session, user.id, 1) == [row]
    conversation_service.record_turn(db_session, user.id, "", "q", "a")


def test_conversation_delete_removes_messages_and_both_drafts(db_session, entities):
    user, _ = entities
    conversation_service.record_turn(db_session, user.id, "thread", "q", "a")
    db_session.add_all([
        ReservationDraft(user_id=user.id, conversation_id="thread", target={}, expires_at=datetime.now() + timedelta(hours=1)),
        CancellationDraft(user_id=user.id, conversation_id="thread", reservation_ids=[], expires_at=datetime.now() + timedelta(hours=1)),
    ])
    db_session.commit()
    assert not conversation_service.delete_conversation(db_session, user.id + 1, "thread")
    assert conversation_service.delete_conversation(db_session, user.id, "thread")
    assert db_session.query(AiMessage).count() == 0
    assert db_session.query(ReservationDraft).count() == 0
    assert db_session.query(CancellationDraft).count() == 0


def test_conversation_prunes_expired_and_keeps_recent(db_session, entities):
    user, _ = entities
    old = datetime.now() - timedelta(days=40)
    db_session.add_all([
        AiConversation(user_id=user.id, conversation_id="old", title="old", message_count=2, last_time=old),
        AiConversation(user_id=user.id, conversation_id="new", title="new", message_count=0, last_time=datetime.now()),
        AiMessage(user_id=user.id, conversation_id="old", role="user", content="old"),
        ReservationDraft(user_id=user.id, conversation_id="old", target={}, expires_at=datetime.now()),
        CancellationDraft(user_id=user.id, conversation_id="old", reservation_ids=[], expires_at=datetime.now()),
    ])
    db_session.commit()
    assert conversation_service.prune_expired(db_session, days=30) == 1
    assert conversation_service.prune_expired(db_session, days=30) == 0
    assert [c.conversation_id for c in conversation_service.list_conversations(db_session, user.id)] == ["new"]
    assert db_session.query(AiMessage).count() == 0


def test_conversation_write_failure_is_swallowed(db_session, entities):
    user, _ = entities
    with patch.object(db_session, "commit", side_effect=RuntimeError("disk")):
        conversation_service.record_turn(db_session, user.id, "bad", "q", "a")
    assert db_session.query(AiConversation).count() == 0


def test_conversation_missing_load_delete_failure_and_prune_empty(db_session, entities):
    user, _ = entities
    assert conversation_service.load_conversation(db_session, user.id, "absent") is None
    assert conversation_service.prune_expired(db_session) == 0
    conversation_service.record_turn(db_session, user.id, "to-delete", "q", "a")
    with patch.object(db_session, "commit", side_effect=RuntimeError("write failed")):
        assert not conversation_service.delete_conversation(db_session, user.id, "to-delete")


def test_conversation_cleanup_loop_is_cancellable_and_prune_failure_is_tolerated(db_session, entities):
    user, _ = entities
    with patch.object(conversation_service, "prune_expired", side_effect=RuntimeError("cleanup")):
        conversation_service.record_turn(db_session, user.id, "prune-fails", "q", "a")

    async def stop_after_one_iteration():
        with patch.object(conversation_service.asyncio, "sleep", side_effect=asyncio.CancelledError), patch.object(
            conversation_service.asyncio, "to_thread", side_effect=RuntimeError("background")
        ):
            await conversation_service.run_cleanup_scan()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stop_after_one_iteration())


def test_reservation_availability_boundaries_and_alternatives(db_session, entities):
    user, lab = entities
    db_session.add_all([
        Reservation(user_id=user.id, lab_id=lab.id, date="2099-10-13", start_time="10:00", end_time="11:00", status=0),
        Reservation(user_id=user.id, lab_id=lab.id, date="2099-10-13", start_time="12:00", end_time="13:00", status=3),
    ])
    db_session.commit()
    assert reservation_service.has_conflict(db_session, lab.id, "2099-10-13", "09:00", "10:00") is False
    assert reservation_service.has_conflict(db_session, lab.id, "2099-10-13", "10:30", "11:30") is True
    availability = reservation_service.get_lab_availability(db_session, lab, "2099-10-13")
    assert len(availability["busy_slots"]) == 1
    assert {"start_time": "11:00", "end_time": "12:00"} in availability["free_slots"]
    alternatives = reservation_service.find_alternative_slots(db_session, lab, "2099-10-13", 120, "13:00", limit=2)
    assert alternatives == [
        {"start_time": "13:00", "end_time": "15:00"},
        {"start_time": "12:00", "end_time": "14:00"},
    ]
    assert reservation_service.find_alternative_slots(db_session, lab, "2099-10-13", 900) == []


def test_latest_reservation_and_active_count_filters(db_session, entities):
    user, lab = entities
    other = type(user)(username="other", password="x", name="Other", role="student", status=1)
    lab2 = type(lab)(name="另一实验室", location="202", capacity=5, open_time="08:00", close_time="18:00", status=1)
    db_session.add_all([other, lab2]); db_session.commit()
    rows = [
        Reservation(user_id=user.id, lab_id=lab.id, date="2099-01-01", start_time="08:00", end_time="09:00", status=0),
        Reservation(user_id=user.id, lab_id=lab2.id, date="2099-01-02", start_time="08:00", end_time="09:00", status=1),
        Reservation(user_id=user.id, lab_id=lab.id, date="2099-01-03", start_time="08:00", end_time="09:00", status=3),
        Reservation(user_id=other.id, lab_id=lab.id, date="2099-01-04", start_time="08:00", end_time="09:00", status=0),
    ]
    db_session.add_all(rows); db_session.commit()
    assert reservation_service.count_active_reservations(db_session, user.id) == 2
    assert reservation_service.get_latest_reservation(db_session, user.id).date == "2099-01-03"
    assert reservation_service.get_latest_reservation(db_session, user.id, lab_id=lab.id, date="2099-01-01").date == "2099-01-01"
    assert reservation_service.get_latest_reservation(db_session, user.id, lab_id=999) is None


def test_pending_reservations_expire_past_and_ended_today(db_session, entities):
    user, lab = entities
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 12, 12, 0)

    today = "2026-10-12"
    old_date = "2026-10-11"
    db_session.add_all([
        Reservation(user_id=user.id, lab_id=lab.id, date=old_date, start_time="08:00", end_time="09:00", status=0),
        Reservation(user_id=user.id, lab_id=lab.id, date=today, start_time="00:00", end_time="00:01", status=0),
        Reservation(user_id=user.id, lab_id=lab.id, date=today, start_time="23:00", end_time="23:59", status=0),
        Reservation(user_id=user.id, lab_id=lab.id, date=old_date, start_time="08:00", end_time="09:00", status=1),
    ])
    db_session.commit()
    with patch.object(reservation_service, "datetime", FixedDateTime), patch.object(reservation_service, "SessionLocal", return_value=db_session):
        reservation_service.expire_pending_reservations()
    assert [r.status for r in db_session.query(Reservation).order_by(Reservation.id)] == [3, 3, 0, 1]


def test_trace_success_failure_and_database_tolerance(db_session, entities):
    user, _ = entities
    ctx = _ctx(db_session, user)
    with tracer.trace_node(ctx, "analyze", "question") as box:
        box["output"] = "answer"
    assert [event["status"] for event in ctx.events] == ["start", "end"]
    assert db_session.query(AgentTrace).one().status == "success"
    with pytest.raises(ValueError):
        with tracer.trace_node(ctx, "custom", "input"):
            raise ValueError("broken")
    failed = tracer.load_trace(db_session, "c1")[-1]
    assert failed.status == "failed" and "ValueError" in failed.output
    tracer.write_tool_execution(ctx, "create_reservation", "预约", {"x": 1}, {"ok": True}, step_index=2)
    row = tracer.load_tool_executions(db_session, "c1")[0]
    assert row.is_write == 1 and row.status == "success"
    with patch.object(db_session, "commit", side_effect=RuntimeError("storage")):
        tracer.write_trace(ctx, "respond", "i", "o")
        tracer.write_tool_execution(ctx, "query", None, {}, {}, status="failed", error="e")


def test_tool_registry_filters_writes_and_duplicate_detection():
    all_tools = tools.bindable_tools(set())
    names = [item.name for item in all_tools]
    assert "create_reservation" not in names and "cancel_reservations" not in names
    allowed = tools.bindable_tools({"create_reservation"}, only={"create_reservation"})
    assert [item.name for item in allowed] == ["create_reservation"]
    successful = [{"tool": "create_reservation", "args": {"date": "tomorrow"}, "ok": True}]
    assert tools.is_duplicate_write("create_reservation", {"date": "tomorrow"}, successful)
    assert not tools.is_duplicate_write("create_reservation", {"date": "later"}, successful)
    assert not tools.is_duplicate_write("list_open_labs", {}, successful)


def test_run_tool_filters_arguments_and_normalizes_errors():
    from typing import Annotated
    from langchain_core.tools import InjectedToolArg, tool

    @tool
    def sample(ctx: Annotated[object, InjectedToolArg], value: str = "default") -> dict:
        """test tool"""
        return {"value": value}

    ctx = SimpleNamespace(db=None, user=SimpleNamespace(id=1), conversation_id="c")
    with patch.dict(tools.TOOL_REGISTRY, {"sample": sample}):
        assert tools.run_tool(ctx, "sample", {"value": "ok", "extra": 1}) == {"value": "ok", "ok": True}
        assert tools.run_tool(ctx, "unknown", {})["ok"] is False
    with patch.object(tools, "TOOL_REGISTRY", {}):
        assert "不存在" in tools.run_tool(ctx, "missing", {})["error"]


def test_agent_read_tools_query_and_write_boundaries(db_session, entities):
    user, lab = entities
    equipment = Equipment(lab_id=lab.id, name="开发主机", spec="i7", quantity=3, status=1)
    db_session.add(equipment)
    db_session.commit()
    ctx = _ctx(db_session, user)

    with patch.object(tools, "_lab_page", side_effect=[SimpleNamespace(total=0, list=[]), SimpleNamespace(total=1, list=[lab])]):
        result = tools.list_open_labs.func(ctx, "错误关键词")
    assert result["keyword_matched"] is False and result["labs"][0]["name"] == lab.name
    with patch.object(tools, "_equipment_page", side_effect=[SimpleNamespace(total=0, list=[]), SimpleNamespace(total=1, list=[equipment])]):
        result = tools.list_lab_equipments.func(ctx, lab.name, "错关键词")
    assert result["keyword_matched"] is False and result["equipments"][0]["id"] == equipment.id

    with patch.object(tools.kb_service, "search", return_value=""):
        assert tools.search_lab_docs.func(ctx, "x")["found"] is False
    with patch.object(tools.kb_service, "search", return_value="安全规范"):
        assert tools.search_lab_docs.func(ctx, "安全")["content"] == "安全规范"
    with patch.object(tools.kb_service, "search", side_effect=RuntimeError("offline")):
        assert tools.search_lab_docs.func(ctx, "安全")["ok"] is False

    requested = tools.query_lab_availability.func(ctx, lab.name, "2099-10-13", "07:00", "21:00")
    assert requested["requested"]["available"] is False
    assert "关闭" in requested["requested"]["conflict_reason"]
    permission = tools.check_user_permission.func(ctx, lab.name)
    assert permission["allowed"] is True
    user.status = 0
    assert "账号已被禁用" in tools.check_user_permission.func(ctx, lab.name)["reasons"]
    user.status = 1
    slots = tools.find_available_slots.func(ctx, lab.name, "2099-10-13", 99)
    assert slots["duration_hours"] == 8.0
    assert tools.verify_reservation.func(ctx, "missing")["exists"] is False

    # 用户身份由 ctx 决定；传入不存在编号不会读到其他人的记录。
    assert tools.cancel_reservation.func(ctx, reservation_id=999)["ok"] is False
    assert tools.cancel_reservations.func(ctx, [1])["ok"] is False


def test_agent_tools_equipment_search_and_reservation_listing(db_session, entities):
    user, lab = entities
    equipment = Equipment(lab_id=lab.id, name="示波器", spec="数字", quantity=2, status=1)
    db_session.add(equipment)
    db_session.flush()
    reservation = Reservation(user_id=user.id, lab_id=lab.id, equipment_id=equipment.id, date="2099-10-13", start_time="09:00", end_time="10:00", status=0)
    db_session.add(reservation)
    db_session.commit()
    ctx = _ctx(db_session, user)
    with patch.object(tools, "_equipment_page", side_effect=[SimpleNamespace(total=0, list=[]), SimpleNamespace(total=1, list=[SimpleNamespace(id=equipment.id, name=equipment.name, spec=equipment.spec, quantity=equipment.quantity, status=1, lab_id=lab.id, lab_name=lab.name)])]):
        found = tools.list_lab_equipments.func(ctx, "", "专业示波器 实验室")
    assert found["keyword"] == "专业示波器" and found["equipments"][0]["lab_name"] == lab.name
    listed = tools.query_user_reservations.func(ctx, status=9, limit=100)
    assert listed["returned"] == 1 and listed["reservations"][0]["reservation_id"] == reservation.id
    verified = tools.verify_reservation.func(ctx, lab.name, "2099-10-13")
    assert verified["exists"] and verified["equipment_name"] == equipment.name


def test_agent_booking_and_cancel_selection_rules(db_session, entities):
    user, lab = entities
    ctx = _ctx(db_session, user)
    missing_equipment = tools.create_reservation.func(ctx, lab.name, "2099-10-13", "09:00", "10:00", "不存在的设备")
    assert missing_equipment["ok"] is False
    created = tools.create_reservation.func(ctx, lab.name, "2099-10-13", "09:00", "10:00", remark="test")
    assert created["ok"] and created["reservation_id"]
    canceled = tools.cancel_reservation.func(ctx, reservation_id=created["reservation_id"])
    assert canceled["ok"] and canceled["status_text"] == "已取消"

    for start in ("09:00", "11:00"):
        db_session.add(Reservation(user_id=user.id, lab_id=lab.id, date="2099-10-14", start_time=start, end_time="10:00" if start == "09:00" else "12:00", status=0))
    db_session.commit()
    ambiguous = tools.cancel_reservation.func(ctx, lab_name=lab.name, date="2099-10-14")
    assert ambiguous["ok"] is False and len(ambiguous["candidates"]) == 2
    db_session.query(Reservation).filter(Reservation.date == "2099-10-14").update({"status": 1})
    db_session.commit()
    noncancelable = tools.cancel_reservation.func(ctx, lab_name=lab.name, date="2099-10-14")
    assert noncancelable["ok"] is False and noncancelable["candidates"]
    unknown_lab = tools.cancel_reservation.func(ctx, lab_name="不存在实验室", date="2099-10-15")
    assert unknown_lab["ok"] is False


@pytest.mark.anyio
async def test_planner_json_helpers_plan_normalization_and_failure_fallback():
    assert planner.load_json("```json\n{\"a\":1,}\n```") == {"a": 1}
    with pytest.raises(ValueError):
        planner.load_json("no json")
    plan = planner._normalize_plan([
        {"goal": "", "hint_tools": ["create_reservation"]},
        {"goal": "lookup", "hint_tools": ["list_open_labs", "create_reservation", "unknown"], "depends_on": [1, "bad"]},
    ], set())
    assert plan == [{"step": 1, "goal": "lookup", "hint_tools": ["list_open_labs"], "depends_on": []}]
    ctx = SimpleNamespace(llm_failures=0)
    with patch.object(planner, "_ask", side_effect=RuntimeError("offline")):
        assert (await planner.make_plan(ctx, {"user_query": "q"}))["plan"] == []
    ctx.llm_failures = planner.BREAKER_THRESHOLD
    assert (await planner.make_plan(ctx, {}))["plan"] == []


@pytest.mark.anyio
async def test_planner_analyze_authorization_uses_user_words_and_handles_bad_output():
    ctx = SimpleNamespace(llm_failures=0)
    payload = '{"slots":{"date":"2099-01-01","lab_name":"null"},"missing_slots":"bad","authorized":false,"write_followup":"booking_confirm"}'
    state = {"user_query": "帮我预约计算机实验室明天14:00到17:00", "today": "2026-10-12", "history": []}
    with patch.object(planner, "_ask", return_value=payload):
        result = await planner.analyze(ctx, state)
    assert result["authorized"] is True
    assert result["allowed_write_tools"] == ["create_reservation"]
    assert result["slots"]["date"] == "2026-10-13"
    assert "lab_name" not in result["slots"]
    assert result["missing_slots"] == []
    assert result["write_followup"] == "booking_confirm"

    with patch.object(planner, "_ask", return_value='{"slots":{},"authorized":true}'):
        question = await planner.analyze(ctx, {"user_query": "能帮我取消吗", "today": "2026-10-12"})
    assert question["authorized"] is False and question["allowed_write_tools"] == []
    with patch.object(planner, "_ask", return_value="not json"):
        assert (await planner.analyze(ctx, {"user_query": "hi", "today": "2026-10-12"}))["error"]


def test_agent_service_persists_only_successful_answers(db_session, entities):
    user, _ = entities
    request = ChatRequest(messages=[{"role": "user", "content": "hello"}])
    with patch.object(agent_service.workflow, "WorkflowRunner") as runner_cls, patch.object(agent_service, "_record_turn") as record:
        runner_cls.return_value.ainvoke = AsyncMock(return_value=" answer ")
        assert asyncio.run(agent_service.run_agent(db_session, user, request)) == "answer"
        record.assert_called_once()
        runner_cls.return_value.ainvoke = AsyncMock(return_value="  ")
        with pytest.raises(BusinessException):
            asyncio.run(agent_service.run_agent(db_session, user, request))
        runner_cls.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("bad"))
        with pytest.raises(BusinessException):
            asyncio.run(agent_service.run_agent(db_session, user, request))
        assert record.call_count == 1


def test_agent_stream_errors_do_not_persist_partial_turn(db_session, entities):
    user, _ = entities
    request = ChatRequest(messages=[{"role": "user", "content": "hello"}])
    async def broken():
        yield {"type": "token", "text": "partial"}
        raise RuntimeError("boom")
    with patch.object(agent_service.workflow, "WorkflowRunner") as runner_cls, patch.object(agent_service, "_record_turn") as record:
        runner_cls.return_value.astream = broken
        async def collect():
            return [event async for event in agent_service.stream_agent(db_session, user, request)]
        events = asyncio.run(collect())
    assert events[-1]["type"] == "error"
    record.assert_not_called()
