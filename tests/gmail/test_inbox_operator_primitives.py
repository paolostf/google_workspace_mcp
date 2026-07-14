import json
import ssl
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

import gmail.gmail_tools as gmail_tools
from core.server import server
from core.tool_registry import get_tool_components
from gmail.gmail_tools import (
    batch_modify_gmail_thread_labels,
    get_gmail_messages_metadata_batch,
    search_gmail_message_index,
)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class _FakeRequest:
    def __init__(self, service, kind, resource_id, outcomes):
        self._service = service
        self._kind = kind
        self._resource_id = resource_id
        self._outcomes = outcomes

    def execute(self, **kwargs):
        self._service.execute_calls.append((self._kind, self._resource_id, kwargs))
        outcomes = self._outcomes[self._resource_id]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeMessagesResource:
    def __init__(self, service):
        self._service = service

    def get(self, **kwargs):
        self._service.message_get_calls.append(kwargs)
        return _FakeRequest(
            self._service,
            "message_get",
            kwargs["id"],
            self._service.message_outcomes,
        )


class _FakeThreadsResource:
    def __init__(self, service):
        self._service = service

    def modify(self, **kwargs):
        self._service.thread_modify_calls.append(kwargs)
        return _FakeRequest(
            self._service,
            "thread_modify",
            kwargs["id"],
            self._service.thread_outcomes,
        )


class _FakeUsersResource:
    def __init__(self, service):
        self._messages = _FakeMessagesResource(service)
        self._threads = _FakeThreadsResource(service)

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads


class _FakeBatch:
    def __init__(self, callback):
        self._callback = callback
        self._requests = []

    def add(self, request, request_id):
        self._requests.append((request_id, request))

    def execute(self):
        for request_id, request in self._requests:
            try:
                response = request.execute()
                self._callback(request_id, response, None)
            except Exception as error:
                self._callback(request_id, None, error)


class _FakeGmailService:
    def __init__(self, *, message_outcomes=None, thread_outcomes=None):
        self.message_outcomes = message_outcomes or {}
        self.thread_outcomes = thread_outcomes or {}
        self.message_get_calls = []
        self.thread_modify_calls = []
        self.execute_calls = []
        self.batch_count = 0
        self._users = _FakeUsersResource(self)

    def users(self):
        return self._users

    def new_batch_http_request(self, callback):
        self.batch_count += 1
        return _FakeBatch(callback)


def _metadata_message(message_id):
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": ["INBOX", "UNREAD"],
        "internalDate": "1770000000000",
        "snippet": f"Snippet for {message_id}",
        "payload": {
            "headers": [
                {"name": "Subject", "value": f"Subject {message_id}"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "X-Ignored", "value": "not selected"},
            ],
            "parts": [
                {
                    "filename": f"{message_id}.pdf",
                    "mimeType": "application/pdf",
                    "body": {
                        "size": 1234,
                        "attachmentId": f"attachment-{message_id}",
                    },
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_search_gmail_message_index_returns_stable_machine_json():
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [
            {"id": "message-1", "threadId": "thread-1"},
            {"id": "message-2", "threadId": "thread-2"},
        ],
        "nextPageToken": "next-1",
        "resultSizeEstimate": 15677,
    }

    result = await _unwrap(search_gmail_message_index)(
        service=service,
        query="is:unread",
        user_google_email="user@example.com",
        page_size=500,
        page_token="page-1",
    )

    service.users.return_value.messages.return_value.list.assert_called_once_with(
        userId="me",
        q="is:unread",
        maxResults=500,
        pageToken="page-1",
    )
    assert json.loads(result) == {
        "refs": [
            {"message_id": "message-1", "thread_id": "thread-1"},
            {"message_id": "message-2", "thread_id": "thread-2"},
        ],
        "next_page_token": "next-1",
        "result_size_estimate": 15677,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [0, 501, True, "500"])
async def test_search_gmail_message_index_rejects_invalid_page_size(page_size):
    service = MagicMock()

    with pytest.raises(ValueError, match="page_size"):
        await _unwrap(search_gmail_message_index)(
            service=service,
            query="is:unread",
            user_google_email="user@example.com",
            page_size=page_size,
        )

    assert service.users.return_value.messages.return_value.list.call_count == 0


@pytest.mark.asyncio
async def test_search_gmail_message_index_rejects_incomplete_reference():
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "message-1"}]
    }

    with pytest.raises(ToolError, match="incomplete reference"):
        await _unwrap(search_gmail_message_index)(
            service=service,
            query="is:unread",
            user_google_email="user@example.com",
        )


def test_search_gmail_message_index_schema_and_annotations():
    component = get_tool_components(server)["search_gmail_message_index"]
    page_size_schema = component.parameters["properties"]["page_size"]

    assert page_size_schema["minimum"] == 1
    assert page_size_schema["maximum"] == 500
    assert component.annotations.readOnlyHint is True
    assert component.annotations.destructiveHint is False
    assert component.annotations.idempotentHint is True
    assert component.annotations.openWorldHint is True


@pytest.mark.asyncio
async def test_get_gmail_messages_metadata_batch_returns_selected_metadata():
    service = _FakeGmailService(
        message_outcomes={
            "message-1": [_metadata_message("message-1")],
            "message-2": [_metadata_message("message-2")],
        }
    )

    result = await _unwrap(get_gmail_messages_metadata_batch)(
        service=service,
        message_ids=["message-1", "message-2", "message-1"],
        user_google_email="user@example.com",
        header_names=["Subject", "From", "subject"],
    )

    parsed = json.loads(result)
    assert parsed["count"] == 2
    assert parsed["messages"][0] == {
        "message_id": "message-1",
        "thread_id": "thread-message-1",
        "label_ids": ["INBOX", "UNREAD"],
        "internal_date": "1770000000000",
        "snippet": "Snippet for message-1",
        "headers": {
            "Subject": "Subject message-1",
            "From": "sender@example.com",
        },
        "attachments": [
            {
                "filename": "message-1.pdf",
                "mime_type": "application/pdf",
                "size": 1234,
                "attachment_id": "attachment-message-1",
            }
        ],
    }
    assert len(service.message_get_calls) == 2
    assert all(call["format"] == "full" for call in service.message_get_calls)
    assert all(
        call["fields"] == gmail_tools.GMAIL_MESSAGE_METADATA_FIELDS
        for call in service.message_get_calls
    )
    assert "body(data" not in gmail_tools.GMAIL_MESSAGE_METADATA_FIELDS


@pytest.mark.asyncio
async def test_get_gmail_messages_metadata_batch_chunks_requests_at_25():
    message_ids = [f"message-{index}" for index in range(26)]
    service = _FakeGmailService(
        message_outcomes={
            message_id: [_metadata_message(message_id)] for message_id in message_ids
        }
    )

    result = await _unwrap(get_gmail_messages_metadata_batch)(
        service=service,
        message_ids=message_ids,
        user_google_email="user@example.com",
    )

    assert json.loads(result)["count"] == 26
    assert service.batch_count == 2


@pytest.mark.asyncio
async def test_get_gmail_messages_metadata_batch_rejects_more_than_100_ids():
    service = _FakeGmailService()

    with pytest.raises(ValueError, match="more than 100"):
        await _unwrap(get_gmail_messages_metadata_batch)(
            service=service,
            message_ids=[f"message-{index}" for index in range(101)],
            user_google_email="user@example.com",
        )

    assert service.batch_count == 0


@pytest.mark.asyncio
async def test_get_gmail_messages_metadata_batch_fails_closed_on_one_error():
    service = _FakeGmailService(
        message_outcomes={
            "message-1": [_metadata_message("message-1")],
            "message-2": [RuntimeError("batch failed"), RuntimeError("retry failed")],
        }
    )

    with pytest.raises(ToolError, match="failed closed"):
        await _unwrap(get_gmail_messages_metadata_batch)(
            service=service,
            message_ids=["message-1", "message-2"],
            user_google_email="user@example.com",
        )


@pytest.mark.asyncio
async def test_get_gmail_messages_metadata_batch_retries_with_backoff(monkeypatch):
    service = _FakeGmailService(
        message_outcomes={
            "message-1": [
                ssl.SSLError("batch"),
                ssl.SSLError("retry"),
                _metadata_message("message-1"),
            ]
        }
    )
    sleep = AsyncMock()
    monkeypatch.setattr(gmail_tools.asyncio, "sleep", sleep)

    result = await _unwrap(get_gmail_messages_metadata_batch)(
        service=service,
        message_ids=["message-1"],
        user_google_email="user@example.com",
    )

    assert json.loads(result)["count"] == 1
    assert len(service.execute_calls) == 3
    assert any(call.args == (1,) for call in sleep.await_args_list)


def test_get_gmail_messages_metadata_batch_schema_and_annotations():
    component = get_tool_components(server)["get_gmail_messages_metadata_batch"]
    schema = component.parameters["properties"]

    assert schema["message_ids"]["type"] == "array"
    assert schema["header_names"]["type"] == "array"
    assert component.annotations.readOnlyHint is True
    assert component.annotations.destructiveHint is False
    assert component.annotations.idempotentHint is True
    assert component.annotations.openWorldHint is True


@pytest.mark.asyncio
async def test_batch_modify_gmail_thread_labels_returns_machine_json_and_dedupes():
    service = _FakeGmailService(thread_outcomes={"thread-1": [{}], "thread-2": [{}]})

    result = await _unwrap(batch_modify_gmail_thread_labels)(
        service=service,
        user_google_email="user@example.com",
        thread_ids=[" thread-1 ", "thread-2", "thread-1"],
        add_label_ids=["STARRED", "STARRED"],
        remove_label_ids=["INBOX", "UNREAD"],
    )

    assert json.loads(result) == {
        "updated_thread_ids": ["thread-1", "thread-2"],
        "thread_count": 2,
        "added_label_ids": ["STARRED"],
        "removed_label_ids": ["INBOX", "UNREAD"],
    }
    assert service.batch_count == 1
    assert service.thread_modify_calls == [
        {
            "userId": "me",
            "id": "thread-1",
            "body": {
                "addLabelIds": ["STARRED"],
                "removeLabelIds": ["INBOX", "UNREAD"],
            },
        },
        {
            "userId": "me",
            "id": "thread-2",
            "body": {
                "addLabelIds": ["STARRED"],
                "removeLabelIds": ["INBOX", "UNREAD"],
            },
        },
    ]


@pytest.mark.asyncio
async def test_batch_modify_gmail_thread_labels_rejects_more_than_100_unique_ids():
    service = _FakeGmailService()

    with pytest.raises(ValueError, match="more than 100 unique IDs"):
        await _unwrap(batch_modify_gmail_thread_labels)(
            service=service,
            user_google_email="user@example.com",
            thread_ids=[f"thread-{index}" for index in range(101)],
            remove_label_ids=["INBOX"],
        )

    assert service.batch_count == 0


@pytest.mark.asyncio
async def test_batch_modify_gmail_thread_labels_chunks_requests_at_25():
    thread_ids = [f"thread-{index}" for index in range(26)]
    service = _FakeGmailService(
        thread_outcomes={thread_id: [{}] for thread_id in thread_ids}
    )

    result = await _unwrap(batch_modify_gmail_thread_labels)(
        service=service,
        user_google_email="user@example.com",
        thread_ids=thread_ids,
        remove_label_ids=["UNREAD"],
    )

    assert json.loads(result)["thread_count"] == 26
    assert service.batch_count == 2


@pytest.mark.asyncio
async def test_batch_modify_gmail_thread_labels_retries_transient_failures(
    monkeypatch,
):
    service = _FakeGmailService(
        thread_outcomes={"thread-1": [ssl.SSLError("batch"), ssl.SSLError("retry"), {}]}
    )
    sleep = AsyncMock()
    monkeypatch.setattr(gmail_tools.asyncio, "sleep", sleep)

    result = await _unwrap(batch_modify_gmail_thread_labels)(
        service=service,
        user_google_email="user@example.com",
        thread_ids=["thread-1"],
        remove_label_ids=["UNREAD"],
    )

    assert json.loads(result)["updated_thread_ids"] == ["thread-1"]
    assert len(service.execute_calls) == 3
    assert any(call.args == (1,) for call in sleep.await_args_list)


@pytest.mark.asyncio
async def test_batch_modify_gmail_thread_labels_fails_closed_after_retry():
    service = _FakeGmailService(
        thread_outcomes={
            "thread-1": [RuntimeError("batch failed"), RuntimeError("retry failed")]
        }
    )

    with pytest.raises(ToolError, match="failed closed"):
        await _unwrap(batch_modify_gmail_thread_labels)(
            service=service,
            user_google_email="user@example.com",
            thread_ids=["thread-1"],
            remove_label_ids=["INBOX"],
        )


def test_batch_modify_gmail_thread_labels_schema_and_annotations():
    component = get_tool_components(server)["batch_modify_gmail_thread_labels"]
    schema = component.parameters["properties"]

    for field_name in ("thread_ids", "add_label_ids", "remove_label_ids"):
        assert schema[field_name]["type"] == "array"
        assert schema[field_name]["items"] == {"type": "string"}

    assert component.annotations.readOnlyHint is False
    assert component.annotations.destructiveHint is True
    assert component.annotations.idempotentHint is True
    assert component.annotations.openWorldHint is True
