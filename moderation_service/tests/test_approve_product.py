from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app, repository


client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_store():
    repository.reset()
    yield
    repository.reset()


def auth_headers(moderator_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {moderator_id}"}


def product_with_skus(product_id: str, sku_id: str | None = None) -> dict:
    return {
        "id": product_id,
        "title": "Phone",
        "description": "Product for moderation",
        "status": "ON_MODERATION",
        "deleted": False,
        "blocked": False,
        "category": {"id": str(uuid4()), "name": "Electronics"},
        "images": [{"url": "/s3/product.jpg", "ordering": 0}],
        "characteristics": [{"name": "Brand", "value": "Neo"}],
        "skus": [
            {
                "id": sku_id or str(uuid4()),
                "name": "base",
                "price": 1000,
                "active_quantity": 3,
            }
        ],
    }


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "B2B error",
                request=httpx.Request("POST", "http://b2b:8000"),
                response=httpx.Response(self.status_code),
            )


class Recorder:
    def __init__(self, response: FakeResponse) -> None:
        self.calls: list[dict] = []
        self.response = response

    def set_response(self, response: FakeResponse) -> None:
        self.response = response

    def __len__(self) -> int:
        return len(self.calls)

    def __getitem__(self, index: int) -> dict:
        return self.calls[index]

    def __eq__(self, other: object) -> bool:
        return self.calls == other


@pytest.fixture()
def b2b_get(monkeypatch):
    recorder = Recorder(FakeResponse(200, {"skus": [{"id": str(uuid4())}]}))

    def fake_get(url, headers, timeout):
        recorder.calls.append({"url": url, "headers": headers, "timeout": timeout})
        return recorder.response

    monkeypatch.setattr("main.httpx.get", fake_get)
    return recorder


@pytest.fixture()
def b2b_post(monkeypatch):
    recorder = Recorder(FakeResponse(200))

    def fake_post(url, json, headers, timeout):
        recorder.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return recorder.response

    monkeypatch.setattr("main.httpx.post", fake_post)
    return recorder


def create_card(
    *,
    product_id: str,
    moderator_id: str | None,
    status_value: str,
    json_after: dict | None = None,
    blocking_reason_id: str | None = None,
) -> None:
    repository.create_test_card(
        product_id=product_id,
        seller_id=str(uuid4()),
        status_value=status_value,
        moderator_id=moderator_id,
        json_after=json_after or product_with_skus(product_id),
        blocking_reason_id=blocking_reason_id,
        date_moderation=datetime.now(timezone.utc).isoformat() if status_value == "IN_REVIEW" else None,
    )


def test_approve_transitions_to_moderated_and_emits_event(b2b_get, b2b_post):
    product_id = str(uuid4())
    moderator_id = str(uuid4())
    create_card(product_id=product_id, moderator_id=moderator_id, status_value="IN_REVIEW")
    repository.add_test_field_report(product_id)

    response = client.post(
        f"/api/v1/products/{product_id}/approve",
        json={"moderator_comment": "ok"},
        headers=auth_headers(moderator_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["product_id"] == product_id
    assert body["status"] == "APPROVED"

    card = repository.get_card(product_id)
    assert card["status"] == "APPROVED"
    assert card["date_moderation"] is not None
    assert card["moderator_comment"] == "ok"
    assert card["blocking_reason_id"] is None
    assert repository.count_field_reports(product_id) == 0

    assert len(b2b_get) == 1
    assert b2b_get[0]["url"] == f"http://b2b:8000/api/v1/products/{product_id}"

    assert len(b2b_post) == 1
    posted = b2b_post[0]
    assert posted["url"] == "http://b2b:8000/api/v1/moderation/events"
    assert "idempotency_key" in posted["json"]
    assert "occurred_at" in posted["json"]
    assert posted["json"]["product_id"] == product_id
    assert posted["json"]["event_type"] == "MODERATED"


def test_openapi_ticket_approve_path_accepts_comment_and_returns_ticket(b2b_get, b2b_post):
    product_id = str(uuid4())
    moderator_id = str(uuid4())
    create_card(product_id=product_id, moderator_id=moderator_id, status_value="IN_REVIEW")
    ticket_id = repository.get_card(product_id)["id"]

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/approve",
        json={"comment": "OpenAPI approve"},
        headers=auth_headers(moderator_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == ticket_id
    assert body["product_id"] == product_id
    assert body["status"] == "APPROVED"
    assert body["decision_at"] is None

    card = repository.get_card(product_id)
    assert card["status"] == "APPROVED"
    assert card["moderator_comment"] == "OpenAPI approve"
    assert len(b2b_get) == 1
    assert len(b2b_post) == 1
    assert b2b_post[0]["json"]["event_type"] == "MODERATED"


def test_approve_others_card_returns_403(b2b_get, b2b_post):
    product_id = str(uuid4())
    owner_moderator_id = str(uuid4())
    current_moderator_id = str(uuid4())
    create_card(product_id=product_id, moderator_id=owner_moderator_id, status_value="IN_REVIEW")

    response = client.post(
        f"/api/v1/products/{product_id}/approve",
        json={"moderator_comment": "ok"},
        headers=auth_headers(current_moderator_id),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_ASSIGNED_TO_YOU"
    assert repository.get_card(product_id)["status"] == "IN_REVIEW"
    assert b2b_get == []
    assert b2b_post == []


def test_approve_after_edited_returns_409(b2b_get, b2b_post):
    product_id = str(uuid4())
    moderator_id = str(uuid4())
    create_card(product_id=product_id, moderator_id=None, status_value="PENDING")

    response = client.post(
        f"/api/v1/products/{product_id}/approve",
        json={"moderator_comment": "ok"},
        headers=auth_headers(moderator_id),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PRODUCT_NOT_IN_REVIEW"
    assert repository.get_card(product_id)["status"] == "PENDING"
    assert b2b_get == []
    assert b2b_post == []


def test_approve_without_sku_returns_409(b2b_get, b2b_post):
    product_id = str(uuid4())
    moderator_id = str(uuid4())
    create_card(product_id=product_id, moderator_id=moderator_id, status_value="IN_REVIEW")
    b2b_get.set_response(FakeResponse(200, {"skus": []}))

    response = client.post(
        f"/api/v1/products/{product_id}/approve",
        json={"moderator_comment": "ok"},
        headers=auth_headers(moderator_id),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PRODUCT_WITHOUT_SKU"
    assert repository.get_card(product_id)["status"] == "IN_REVIEW"
    assert repository.count_field_reports(product_id) == 0
    assert b2b_post == []


def test_approve_b2b_event_failure_keeps_card_in_review(b2b_get, b2b_post):
    failing_client = TestClient(app, raise_server_exceptions=False)
    product_id = str(uuid4())
    moderator_id = str(uuid4())
    blocking_reason_id = "a7b8c9d0-1234-5678-ef01-890123456789"
    create_card(
        product_id=product_id,
        moderator_id=moderator_id,
        status_value="IN_REVIEW",
        blocking_reason_id=blocking_reason_id,
    )
    repository.add_test_field_report(product_id)
    b2b_post.set_response(FakeResponse(500))

    response = failing_client.post(
        f"/api/v1/products/{product_id}/approve",
        json={"moderator_comment": "approve comment"},
        headers=auth_headers(moderator_id),
    )

    assert response.status_code == 500
    card = repository.get_card(product_id)
    assert card["status"] == "IN_REVIEW"
    assert card["moderator_comment"] != "approve comment"
    assert card["blocking_reason_id"] == blocking_reason_id
    assert repository.count_field_reports(product_id) == 1
    assert len(b2b_post) == 1
