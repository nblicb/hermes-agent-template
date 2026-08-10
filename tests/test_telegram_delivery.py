import asyncio
from types import SimpleNamespace

import pytest

import rate_limit


class FakeAdapter:
    def __init__(self):
        self._bot = SimpleNamespace(token="test-token")
        self.original_calls = []

    async def send(self, *args, **kwargs):
        self.original_calls.append((args, kwargs))
        return SimpleNamespace(success=True, message_id="plain-1")


@pytest.fixture(autouse=True)
def rich_messages_enabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RICH_MESSAGES_ENABLED", "1")
    monkeypatch.setattr(
        rate_limit,
        "_telegram_rich_send_result",
        lambda result: SimpleNamespace(
            success=True,
            message_id=str(result["message_id"]),
            raw_response=result,
        ),
    )
    monkeypatch.setattr(
        rate_limit,
        "_telegram_suppressed_send_result",
        lambda: SimpleNamespace(success=True, message_id=None, raw_response={"suppressed": True}),
    )


@pytest.mark.asyncio
async def test_rich_wrapper_accepts_hermes_keyword_send_contract(monkeypatch):
    adapter = FakeAdapter()
    rich_calls = []
    monkeypatch.setattr(
        rate_limit,
        "_post_telegram_rich_markdown",
        lambda token, chat_id, text: rich_calls.append((token, chat_id, text))
        or {"message_id": 42},
    )

    rate_limit._wrap_telegram_adapter(adapter)
    result = await adapter.send(
        chat_id="353559286",
        content="PLTR earnings result",
        reply_to="123",
        metadata={"notify": True},
    )

    assert result.success is True
    assert result.message_id == "42"
    assert rich_calls == [("test-token", "353559286", "PLTR earnings result")]
    assert adapter.original_calls == []


@pytest.mark.asyncio
async def test_rich_wrapper_accepts_progress_positional_contract(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setattr(
        rate_limit,
        "_post_telegram_rich_markdown",
        lambda token, chat_id, text: {"message_id": 7},
    )

    rate_limit._wrap_telegram_adapter(adapter)
    result = await adapter.send("353559286", "正在查询财报...")

    assert result.success is True
    assert result.message_id == "7"
    assert adapter.original_calls == []


@pytest.mark.asyncio
async def test_rich_failure_preserves_exact_original_call(monkeypatch):
    adapter = FakeAdapter()

    def fail_rich(*_args):
        raise RuntimeError("rich endpoint unavailable")

    monkeypatch.setattr(rate_limit, "_post_telegram_rich_markdown", fail_rich)
    rate_limit._wrap_telegram_adapter(adapter)
    kwargs = {
        "chat_id": "353559286",
        "content": "final answer",
        "reply_to": "123",
        "metadata": {"notify": True},
    }

    result = await adapter.send(**kwargs)

    assert result.success is True
    assert result.message_id == "plain-1"
    assert adapter.original_calls == [((), kwargs)]


@pytest.mark.asyncio
async def test_internal_memory_plan_is_never_delivered(monkeypatch):
    adapter = FakeAdapter()
    rich_calls = []
    monkeypatch.setattr(
        rate_limit,
        "_post_telegram_rich_markdown",
        lambda *args: rich_calls.append(args) or {"message_id": 8},
    )
    leaked_plan = (
        "I need to save the user's shared data tip about accessing earnings "
        "conference calls via FMP data and MCP to persistent memory for future "
        "reference, then acknowledge the information for the user."
    )

    rate_limit._wrap_telegram_adapter(adapter)
    result = await adapter.send(chat_id="353559286", content=leaked_plan)

    assert result.success is True
    assert result.message_id is None
    assert rich_calls == []
    assert adapter.original_calls == []


@pytest.mark.asyncio
async def test_reasoning_block_is_removed_before_delivery(monkeypatch):
    adapter = FakeAdapter()
    rich_calls = []
    monkeypatch.setattr(
        rate_limit,
        "_post_telegram_rich_markdown",
        lambda token, chat_id, text: rich_calls.append((token, chat_id, text))
        or {"message_id": 9},
    )

    rate_limit._wrap_telegram_adapter(adapter)
    result = await adapter.send(
        chat_id="353559286",
        content="<thinking>Need to call a tool.</thinking>美光最新财报如下。",
    )

    assert result.success is True
    assert result.message_id == "9"
    assert rich_calls == [("test-token", "353559286", "美光最新财报如下。")]


def test_market_memory_language_is_not_mistaken_for_internal_memory():
    answer = "Micron memory demand remains above supply, according to management."

    assert rate_limit._sanitize_telegram_output(answer) == answer


@pytest.mark.asyncio
async def test_handler_failure_still_cancels_and_deletes_progress():
    deleted = []
    progress_started = asyncio.Event()

    async def progress():
        progress_started.set()
        await asyncio.sleep(60)

    progress_task = asyncio.create_task(progress())
    await progress_started.wait()

    async def failing_handler(_runner, _event):
        raise RuntimeError("delivery failed")

    async def delete_message(**kwargs):
        deleted.append(kwargs)

    adapter = SimpleNamespace(
        _bot=SimpleNamespace(delete_message=delete_message),
    )

    with pytest.raises(RuntimeError, match="delivery failed"):
        await rate_limit._call_with_telegram_progress_cleanup(
            failing_handler,
            object(),
            object(),
            progress_task=progress_task,
            adapter=adapter,
            chat_id="353559286",
            status_msg_id=99,
        )

    assert progress_task.cancelled()
    assert deleted == [{"chat_id": "353559286", "message_id": 99}]
