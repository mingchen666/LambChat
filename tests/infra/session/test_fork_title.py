from types import SimpleNamespace

import pytest

from src.infra.session.manager import SessionManager
from src.kernel.schemas.session import Session


def test_build_fork_session_name_reuses_source_title() -> None:
    assert SessionManager._build_fork_session_name("Budget planning") == "Budget planning (Fork)"


def test_build_fork_session_name_falls_back_for_blank_title() -> None:
    assert SessionManager._build_fork_session_name("  ") == "New Chat (Fork)"


def test_build_fork_session_name_does_not_stack_fork_suffix() -> None:
    assert (
        SessionManager._build_fork_session_name("Budget planning (Fork)")
        == "Budget planning (Fork)"
    )


class _AsyncTraceCursor:
    def __init__(self, traces):
        self._traces = traces
        self.to_list_called = False

    def sort(self, *_args):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._traces:
            raise StopAsyncIteration
        return self._traces.pop(0)

    async def to_list(self, length=None):
        self.to_list_called = True
        return list(self._traces)


class _FakeTraceCollection:
    def __init__(self, traces):
        self.cursor = _AsyncTraceCursor(list(traces))
        self.inserted_docs = None

    def find(self, *_args, **_kwargs):
        return self.cursor

    async def insert_many(self, docs):
        self.inserted_docs = docs


class _FakeSessionStorage:
    def __init__(self):
        self.deleted_session_ids = []
        self.rebuilt_session_ids = []

    async def create(self, session_data, user_id=None):
        return Session(
            id="target", user_id=user_id, name=session_data.name, metadata=session_data.metadata
        )

    async def delete(self, session_id):
        self.deleted_session_ids.append(session_id)
        return True

    async def rebuild_search_index(self, session_id):
        self.rebuilt_session_ids.append(session_id)
        return True


@pytest.mark.asyncio
async def test_clone_history_streams_traces_until_target_run() -> None:
    traces = [
        {"run_id": "run-1", "events": [], "started_at": 1},
        {"run_id": "run-2", "events": [], "started_at": 2},
        {"run_id": "run-3", "events": [], "started_at": 3},
    ]
    collection = _FakeTraceCollection(traces)
    manager = SessionManager()
    manager._trace_storage = SimpleNamespace(collection=collection)

    cloned_docs = await manager._clone_history_to_session(
        source_session=Session(id="source", user_id="user"),
        target_session=Session(id="target", user_id="user"),
        target={
            "run_id": "run-2",
            "target_type": "assistant",
            "completed_run_ids": ["run-1", "run-2"],
        },
        user_id="user",
    )

    assert len(cloned_docs) == 2
    assert collection.cursor.to_list_called is False
    assert collection.inserted_docs is not None
    assert [doc["run_id"] for doc in collection.inserted_docs] == ["run-1", "run-2"]


@pytest.mark.asyncio
async def test_fork_session_continues_when_checkpoint_clone_fails(monkeypatch) -> None:
    source_events = [
        {
            "event_type": "user:message",
            "data": {"content": "hello", "message_id": "run-1:user"},
            "timestamp": "2026-01-01T00:00:00Z",
        },
        {
            "event_type": "message:chunk",
            "data": {"content": "hi"},
            "timestamp": "2026-01-01T00:00:01Z",
        },
        {
            "event_type": "message:chunk",
            "data": {"content": " there"},
            "timestamp": "2026-01-01T00:00:02Z",
        },
        {
            "event_type": "done",
            "data": {"status": "completed"},
            "timestamp": "2026-01-01T00:00:03Z",
        },
    ]
    manager = SessionManager()
    storage = _FakeSessionStorage()
    manager.storage = storage
    manager._trace_storage = SimpleNamespace(
        collection=_FakeTraceCollection(
            [
                {
                    "run_id": "run-1",
                    "trace_id": "trace-1",
                    "session_id": "source",
                    "user_id": "user",
                    "events": source_events,
                    "event_count": len(source_events),
                    "status": "completed",
                    "started_at": 1,
                }
            ]
        )
    )

    async def _get_session(session_id):
        return Session(id=session_id, user_id="user", name="Source", metadata={})

    async def _resolve_target(_session_id, _message_id):
        return {
            "run_id": "run-1",
            "target_type": "assistant",
            "turn_index": 1,
            "completed_run_ids": ["run-1"],
        }

    async def _fail_clone(*_args, **_kwargs):
        raise ValueError("missing checkpoint")

    seeded_messages = []

    async def _seed_checkpoint(_thread_id, messages):
        seeded_messages.extend(messages)
        return 1

    manager.get_session = _get_session
    manager._resolve_fork_target = _resolve_target
    monkeypatch.setattr("src.infra.session.manager.clone_checkpoints_for_fork", _fail_clone)
    monkeypatch.setattr("src.infra.session.manager.seed_checkpoint_from_messages", _seed_checkpoint)

    result = await manager.fork_session_from_message("source", "run-1", "user")

    assert result["session"].id == "target"
    assert result["copied_checkpoint_count"] == 1
    assert result["copied_trace_count"] == 1
    assert storage.deleted_session_ids == []

    inserted_trace = manager.trace_storage.collection.inserted_docs[0]
    assert inserted_trace["session_id"] == "target"
    assert inserted_trace["user_id"] == "user"
    assert inserted_trace["events"] == source_events
    assert inserted_trace["event_count"] == len(source_events)
    assert [type(message).__name__ for message in seeded_messages] == [
        "HumanMessage",
        "AIMessage",
    ]
    assert seeded_messages[0].content == "hello"
    assert seeded_messages[1].content == "hi there"
