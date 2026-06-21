from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app, repository


client = TestClient(app)

HARD_REASON_ID = "b4c5d6e7-8901-2345-5678-567890123456"
SOFT_REASON_ID = "a7b8c9d0-1234-5678-ef01-890123456789"
SERVICE_KEY = "b2b-to-mod-key"


@pytest.fixture(autouse=True)
def clean_store():
    repository.reset()
    yield
    repository.reset()


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "B2B error",
                request=httpx.Request("POST", "http://b2b:8000"),
                response=httpx.Response(self.status_code),
            )


class B2BRecorder:
    def __init__(self) -> None:
        self._calls: list[dict] = []
        self._response = FakeResponse(200)

    def record(self, url: str, json: dict, headers: dict) -> FakeResponse:
        self._calls.append({"url": url, "json": json, "headers": headers})
        return self._response

    def set_failure(self) -> None:
        self._response = FakeResponse(500)

    def __len__(self) -> int:
        return len(self._calls)

    def __getitem__(self, index: int) -> dict:
        return self._calls[index]

    def __eq__(self, other: object) -> bool:
        return self._calls == other


@pytest.fixture()
def b2b_requests(monkeypatch):
    recorder = B2BRecorder()
    monkeypatch.setattr(
        "main.httpx.post",
        lambda url, json, headers, timeout: recorder.record(url, json, headers),
    )
    return recorder


def auth_headers(moderator_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {moderator_id}"}


def service_key_headers() -> dict[str, str]:
    return {"X-Service-Key": SERVICE_KEY}


def product_snapshot(product_id: str) -> dict:
    return {
        "id": product_id,
        "title": "Test Product",
        "description": "Original description",
        "skus": [{"id": str(uuid4()), "name": "base", "price": 500, "active_quantity": 2}],
    }


def create_in_review_card(moderator_id: str, product_id: str | None = None) -> str:
    pid = product_id or str(uuid4())
    repository.create_test_card(
        product_id=pid,
        seller_id=str(uuid4()),
        status_value="IN_REVIEW",
        json_after=product_snapshot(pid),
        moderator_id=moderator_id,
        date_moderation=datetime.now(timezone.utc).isoformat(),
    )
    return pid


def create_hard_blocked_card(moderator_id: str, product_id: str | None = None) -> str:
    pid = product_id or str(uuid4())
    repository.create_test_card(
        product_id=pid,
        seller_id=str(uuid4()),
        status_value="HARD_BLOCKED",
        json_after=product_snapshot(pid),
        moderator_id=moderator_id,
        blocking_reason_id=HARD_REASON_ID,
        date_moderation=datetime.now(timezone.utc).isoformat(),
    )
    return pid


def decline_payload(**overrides) -> dict:
    payload = {
        "blocking_reason_id": HARD_REASON_ID,
        "moderator_comment": "Контрафакт",
        "field_reports": [],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. Happy path: IN_REVIEW -> HARD_BLOCKED
# ---------------------------------------------------------------------------

def test_hard_block_transitions_to_terminal_and_emits_event(b2b_requests):
    moderator_id = str(uuid4())
    product_id = create_in_review_card(moderator_id)

    response = client.post(
        f"/api/v1/products/{product_id}/decline",
        json=decline_payload(),
        headers=auth_headers(moderator_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["product_id"] == product_id
    assert body["status"] == "HARD_BLOCKED"

    card = repository.get_card(product_id)
    assert card["status"] == "HARD_BLOCKED"
    assert card["blocking_reason_id"] == HARD_REASON_ID
    assert card["moderator_comment"] == "Контрафакт"
    assert card["date_moderation"] is not None
    assert len(b2b_requests) == 1


# ---------------------------------------------------------------------------
# 2. B2B event carries hard_block=True
# ---------------------------------------------------------------------------

def test_hard_block_event_carries_hard_block_true(b2b_requests):
    moderator_id = str(uuid4())
    product_id = create_in_review_card(moderator_id)

    client.post(
        f"/api/v1/products/{product_id}/decline",
        json=decline_payload(),
        headers=auth_headers(moderator_id),
    )

    assert len(b2b_requests) == 1
    event = b2b_requests[0]["json"]
    assert event["product_id"] == product_id
    assert event["hard_block"] is True
    # Canonical helper sends event_type "BLOCKED" regardless of hard/soft
    assert event["event_type"] == "BLOCKED"
    assert event["blocking_reason_id"] == HARD_REASON_ID
    assert "field_reports" in event


# ---------------------------------------------------------------------------
# 3. Any mutating endpoint on HARD_BLOCKED returns 403
# ---------------------------------------------------------------------------

def test_any_modify_on_hard_blocked_returns_403(b2b_requests):
    moderator_id = str(uuid4())
    product_id = create_hard_blocked_card(moderator_id)
    ticket_id = repository.get_card(product_id)["id"]

    for url, payload in [
        (f"/api/v1/products/{product_id}/decline", decline_payload()),
        (f"/api/v1/tickets/{ticket_id}/block", decline_payload()),
        (f"/api/v1/products/{product_id}/approve", {"moderator_comment": "ok"}),
        (f"/api/v1/tickets/{ticket_id}/approve", {"comment": "ok"}),
    ]:
        resp = client.post(url, json=payload, headers=auth_headers(moderator_id))
        assert resp.status_code == 403, f"{url} should return 403, got {resp.status_code}"
        assert resp.json()["code"] == "PRODUCT_HARD_BLOCKED"

    assert repository.get_card(product_id)["status"] == "HARD_BLOCKED"
    assert b2b_requests == []


# ---------------------------------------------------------------------------
# 4. Incoming EDITED event on HARD_BLOCKED card is silently ignored
# ---------------------------------------------------------------------------

def test_edited_event_on_hard_blocked_is_ignored():
    moderator_id = str(uuid4())
    product_id = create_hard_blocked_card(moderator_id)
    repository.add_test_field_report(product_id)

    original_card = repository.get_card(product_id)

    edited_event = {
        "product_id": product_id,
        "seller_id": str(uuid4()),
        "event": "EDITED",
        "date": datetime.now(timezone.utc).isoformat(),
        "idempotency_key": str(uuid4()),
        "json_after": {
            "id": product_id,
            "title": "Новое название (должно игнорироваться)",
            "description": "Изменённое описание",
            "skus": [{"id": str(uuid4()), "name": "new", "price": 999, "active_quantity": 1}],
        },
    }

    response = client.post(
        "/api/v1/events/product",
        json=edited_event,
        headers=service_key_headers(),
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is True

    card = repository.get_card(product_id)
    assert card["status"] == "HARD_BLOCKED"
    assert card["json_after"]["title"] == "Test Product"  # original title unchanged
    assert card["moderator_id"] == moderator_id  # not reset to NULL
    assert repository.count_field_reports(product_id) == 1  # field reports intact


# ---------------------------------------------------------------------------
# 5. Incoming DELETED event removes the HARD_BLOCKED card
# ---------------------------------------------------------------------------

def test_deleted_event_removes_hard_blocked():
    moderator_id = str(uuid4())
    product_id = create_hard_blocked_card(moderator_id)

    deleted_event = {
        "product_id": product_id,
        "event": "DELETED",
        "date": datetime.now(timezone.utc).isoformat(),
        "idempotency_key": str(uuid4()),
    }

    response = client.post(
        "/api/v1/events/product",
        json=deleted_event,
        headers=service_key_headers(),
    )

    assert response.status_code == 200
    assert repository.get_card(product_id) is None

    # Second DELETED with a new idempotency_key must not raise
    second_response = client.post(
        "/api/v1/events/product",
        json={**deleted_event, "idempotency_key": str(uuid4())},
        headers=service_key_headers(),
    )
    assert second_response.status_code == 200
    assert second_response.json()["accepted"] is True


# ---------------------------------------------------------------------------
# 6. Soft-block with hard_block=false reason still produces BLOCKED
# ---------------------------------------------------------------------------

def test_soft_block_still_uses_blocked_and_hard_block_false(b2b_requests):
    moderator_id = str(uuid4())
    product_id = create_in_review_card(moderator_id)

    response = client.post(
        f"/api/v1/products/{product_id}/decline",
        json={
            "blocking_reason_id": SOFT_REASON_ID,
            "moderator_comment": "Описание некорректно",
            "field_reports": [],
        },
        headers=auth_headers(moderator_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "BLOCKED"
    assert body["hard_block"] is False

    card = repository.get_card(product_id)
    assert card["status"] == "BLOCKED"

    assert len(b2b_requests) == 1
    assert b2b_requests[0]["json"]["hard_block"] is False
    assert b2b_requests[0]["json"]["event_type"] == "BLOCKED"


# ---------------------------------------------------------------------------
# 7. B2B event failure rolls back local hard-block changes
# ---------------------------------------------------------------------------

def test_hard_block_b2b_failure_rolls_back_local_changes(b2b_requests):
    failing_client = TestClient(app, raise_server_exceptions=False)
    moderator_id = str(uuid4())
    product_id = create_in_review_card(moderator_id)
    b2b_requests.set_failure()

    response = failing_client.post(
        f"/api/v1/products/{product_id}/decline",
        json=decline_payload(),
        headers=auth_headers(moderator_id),
    )

    assert response.status_code == 500
    card = repository.get_card(product_id)
    assert card["status"] == "IN_REVIEW"
    assert card["blocking_reason_id"] is None
    assert card["moderator_comment"] is None
    assert len(b2b_requests) == 1
