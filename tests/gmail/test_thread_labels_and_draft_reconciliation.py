from unittest.mock import MagicMock

import pytest

from core.server import server
from core.tool_registry import get_tool_components
from gmail.gmail_tools import (
    _format_thread_content,
    list_gmail_drafts,
    modify_gmail_thread_labels,
    upsert_gmail_draft,
)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.mark.asyncio
async def test_modify_gmail_thread_labels_uses_threads_modify_and_normalizes():
    service = MagicMock()

    result = await _unwrap(modify_gmail_thread_labels)(
        service=service,
        user_google_email="user@example.com",
        thread_id=" thread-123 ",
        add_label_ids=["STARRED", "STARRED"],
        remove_label_ids=["INBOX", "UNREAD"],
    )

    modify = service.users.return_value.threads.return_value.modify
    modify.assert_called_once_with(
        userId="me",
        id="thread-123",
        body={
            "addLabelIds": ["STARRED"],
            "removeLabelIds": ["INBOX", "UNREAD"],
        },
    )
    assert "Thread ID: thread-123" in result
    assert "Added labels: STARRED" in result
    assert "Removed labels: INBOX, UNREAD" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thread_id", "add_ids", "remove_ids", "error"),
    [
        ("", ["STARRED"], None, "thread_id must be a non-empty string"),
        ("thread-1", None, None, "At least one"),
        ("thread-1", ["INBOX"], ["INBOX"], "cannot be added and removed"),
        ("thread-1", [""], None, "non-empty label IDs"),
        (
            "thread-1",
            [f"Label_{index}" for index in range(101)],
            None,
            "more than 100",
        ),
    ],
)
async def test_modify_gmail_thread_labels_rejects_ambiguous_requests(
    thread_id, add_ids, remove_ids, error
):
    service = MagicMock()

    with pytest.raises(ValueError, match=error):
        await _unwrap(modify_gmail_thread_labels)(
            service=service,
            user_google_email="user@example.com",
            thread_id=thread_id,
            add_label_ids=add_ids,
            remove_label_ids=remove_ids,
        )

    assert service.users.return_value.threads.return_value.modify.call_count == 0


def test_modify_gmail_thread_labels_schema_and_annotations_are_accurate():
    component = get_tool_components(server)["modify_gmail_thread_labels"]
    schema = component.parameters["properties"]

    for field_name in ("add_label_ids", "remove_label_ids"):
        assert schema[field_name]["type"] == "array"
        assert schema[field_name]["items"] == {"type": "string"}

    assert component.annotations.readOnlyHint is False
    assert component.annotations.destructiveHint is True
    assert component.annotations.idempotentHint is True
    assert component.annotations.openWorldHint is True


@pytest.mark.asyncio
async def test_list_gmail_drafts_returns_stable_ids_metadata_and_pagination():
    service = MagicMock()
    drafts_resource = service.users.return_value.drafts.return_value
    drafts_resource.list.return_value.execute.return_value = {
        "drafts": [
            {
                "id": "draft-1",
                "message": {"id": "summary-message-1", "threadId": "thread-1"},
            }
        ],
        "nextPageToken": "next-123",
    }
    drafts_resource.get.return_value.execute.return_value = {
        "id": "draft-1",
        "message": {
            "id": "message-1",
            "threadId": "thread-1",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Open case"},
                    {"name": "To", "value": "vendor@example.com"},
                ]
            },
        },
    }

    result = await _unwrap(list_gmail_drafts)(
        service=service,
        user_google_email="user@example.com",
        page_size=10,
        page_token="page-1",
        query=" in:drafts newer_than:7d ",
    )

    drafts_resource.list.assert_called_once_with(
        userId="me",
        maxResults=10,
        pageToken="page-1",
        q="in:drafts newer_than:7d",
    )
    drafts_resource.get.assert_called_once_with(
        userId="me",
        id="draft-1",
        format="metadata",
    )
    assert "Draft ID: draft-1" in result
    assert "Gmail Message ID: message-1" in result
    assert "Thread ID: thread-1" in result
    assert "Subject: Open case" in result
    assert "To: vendor@example.com" in result
    assert "page_token='next-123'" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [0, 501, True, "25"])
async def test_list_gmail_drafts_rejects_invalid_page_size(page_size):
    service = MagicMock()

    with pytest.raises(ValueError, match="page_size"):
        await _unwrap(list_gmail_drafts)(
            service=service,
            user_google_email="user@example.com",
            page_size=page_size,
        )

    assert service.users.return_value.drafts.return_value.list.call_count == 0


@pytest.mark.asyncio
async def test_list_gmail_drafts_fails_closed_when_metadata_fetch_fails():
    service = MagicMock()
    drafts_resource = service.users.return_value.drafts.return_value
    drafts_resource.list.return_value.execute.return_value = {
        "drafts": [{"id": "draft-1", "message": {"id": "message-1"}}]
    }
    drafts_resource.get.return_value.execute.side_effect = RuntimeError(
        "API unavailable"
    )

    with pytest.raises(RuntimeError, match="API unavailable"):
        await _unwrap(list_gmail_drafts)(
            service=service,
            user_google_email="user@example.com",
        )


def test_list_gmail_drafts_schema_matches_google_limit():
    component = get_tool_components(server)["list_gmail_drafts"]
    page_size_schema = component.parameters["properties"]["page_size"]

    assert page_size_schema["minimum"] == 1
    assert page_size_schema["maximum"] == 500


def test_list_gmail_drafts_annotations_are_read_only_and_idempotent():
    component = get_tool_components(server)["list_gmail_drafts"]

    assert component.annotations.readOnlyHint is True
    assert component.annotations.destructiveHint is False
    assert component.annotations.idempotentHint is True
    assert component.annotations.openWorldHint is True


@pytest.mark.asyncio
async def test_upsert_gmail_draft_reconciles_by_updating_the_same_draft_id():
    service = MagicMock()
    drafts_resource = service.users.return_value.drafts.return_value
    drafts_resource.update.return_value.execute.return_value = {"id": "draft-1"}

    kwargs = {
        "service": service,
        "user_google_email": "user@example.com",
        "draft_id": "draft-1",
        "to": "vendor@example.com",
        "subject": "Open case",
        "body": "Prepared reply",
        "include_signature": False,
    }
    first_result = await _unwrap(upsert_gmail_draft)(**kwargs)
    second_result = await _unwrap(upsert_gmail_draft)(**kwargs)

    assert drafts_resource.update.call_count == 2
    assert drafts_resource.create.call_count == 0
    for call in drafts_resource.update.call_args_list:
        assert call.kwargs["userId"] == "me"
        assert call.kwargs["id"] == "draft-1"
        assert set(call.kwargs["body"]) == {"message"}
        assert set(call.kwargs["body"]["message"]) == {"raw"}
    assert first_result == "Draft updated! Draft ID: draft-1"
    assert second_result == first_result


@pytest.mark.asyncio
async def test_upsert_gmail_draft_without_id_creates_once():
    service = MagicMock()
    drafts_resource = service.users.return_value.drafts.return_value
    drafts_resource.create.return_value.execute.return_value = {"id": "draft-new"}

    result = await _unwrap(upsert_gmail_draft)(
        service=service,
        user_google_email="user@example.com",
        to="vendor@example.com",
        subject="Open case",
        body="Prepared reply",
        include_signature=False,
    )

    drafts_resource.create.assert_called_once()
    assert drafts_resource.update.call_count == 0
    assert result == "Draft created! Draft ID: draft-new"


@pytest.mark.asyncio
async def test_upsert_gmail_draft_rejects_blank_id_instead_of_creating_duplicate():
    service = MagicMock()

    with pytest.raises(ValueError, match="draft_id must be a non-empty string"):
        await _unwrap(upsert_gmail_draft)(
            service=service,
            user_google_email="user@example.com",
            draft_id="   ",
            subject="Open case",
            body="Prepared reply",
            include_signature=False,
        )

    drafts_resource = service.users.return_value.drafts.return_value
    assert drafts_resource.create.call_count == 0
    assert drafts_resource.update.call_count == 0


def test_upsert_gmail_draft_annotations_explain_mixed_create_update_semantics():
    component = get_tool_components(server)["upsert_gmail_draft"]

    assert component.annotations.readOnlyHint is False
    assert component.annotations.destructiveHint is True
    assert component.annotations.idempotentHint is False
    assert component.annotations.openWorldHint is True


def test_formatted_thread_exposes_each_internal_gmail_message_id():
    thread = {
        "messages": [
            {
                "id": "gmail-message-1",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Open case"},
                        {"name": "Message-ID", "value": "<rfc-1@example.com>"},
                    ]
                },
            },
            {
                "id": "gmail-message-2",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Re: Open case"},
                        {"name": "Message-ID", "value": "<rfc-2@example.com>"},
                    ]
                },
            },
        ]
    }

    result = _format_thread_content(thread, "thread-1")

    assert "Gmail Message ID: gmail-message-1" in result
    assert "Gmail Message ID: gmail-message-2" in result
    assert "Message-ID: <rfc-1@example.com>" in result
    assert "Message-ID: <rfc-2@example.com>" in result
