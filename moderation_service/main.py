import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


SERVICE_KEY_HEADER = "X-Service-Key"
DEFAULT_B2B_TO_MOD_KEY = "b2b-to-mod-key"
DEFAULT_MOD_TO_B2B_KEY = "dev-moderation-to-b2b-key"
DEFAULT_B2B_URL = "http://b2b:8000"
DEFAULT_B2B_TIMEOUT_SECONDS = 3.0
DEFAULT_DB_PATH = "moderation.sqlite3"
DEFAULT_IN_REVIEW_TIMEOUT_MINUTES = 30
ALLOWED_FIELD_NAMES = {
    "title",
    "description",
    "product_images",
    "category",
    "sku_name",
    "sku_image",
    "sku_price",
}
BLOCKING_REASONS = [
    ("a7b8c9d0-1234-5678-ef01-890123456789", "Описание не соответствует товару", False),
    ("b8c9d0e1-2345-6789-f012-901234567890", "Изображение не соответствует товару", False),
    ("c9d0e1f2-3456-7890-0123-012345678901", "Некорректная категория товара", False),
    ("d0e1f2a3-4567-8901-1234-123456789012", "Недостаточно информации о товаре", False),
    ("e1f2a3b4-5678-9012-2345-234567890123", "Нецензурные или оскорбительные материалы", False),
    ("f2a3b4c5-6789-0123-3456-345678901234", "Дублирование существующего товара", False),
    ("a3b4c5d6-7890-1234-4567-456789012345", "Некорректная цена", False),
    ("b4c5d6e7-8901-2345-5678-567890123456", "Контрафактный товар", True),
    ("c5d6e7f8-9012-3456-6789-678901234567", "Товар запрещён к продаже на территории РФ", True),
    ("d6e7f8a9-0123-4567-7890-789012345678", "Товар нарушает авторские права", True),
]

BLOCKING_REASON_CODES = {
    "a7b8c9d0-1234-5678-ef01-890123456789": "DESCRIPTION_MISMATCH",
    "b8c9d0e1-2345-6789-f012-901234567890": "IMAGE_MISMATCH",
    "c9d0e1f2-3456-7890-0123-012345678901": "INCORRECT_CATEGORY",
    "d0e1f2a3-4567-8901-1234-123456789012": "INSUFFICIENT_INFORMATION",
    "e1f2a3b4-5678-9012-2345-234567890123": "OFFENSIVE_MATERIALS",
    "f2a3b4c5-6789-0123-3456-345678901234": "DUPLICATE_PRODUCT",
    "a3b4c5d6-7890-1234-4567-456789012345": "INCORRECT_PRICE",
    "b4c5d6e7-8901-2345-5678-567890123456": "COUNTERFEIT_GOODS",
    "c5d6e7f8-9012-3456-6789-678901234567": "PROHIBITED_GOODS",
    "d6e7f8a9-0123-4567-7890-789012345678": "COPYRIGHT_VIOLATION",
}


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class IncomingB2BEvent(BaseModel):
    event_type: Literal["PRODUCT_CREATED", "PRODUCT_EDITED", "PRODUCT_DELETED"]
    idempotency_key: UUID
    occurred_at: datetime
    payload: dict[str, Any]


class CanonicalProductEvent(BaseModel):
    product_id: UUID
    seller_id: UUID | None = None
    event: Literal["CREATED", "EDITED", "DELETED"]
    date: datetime
    idempotency_key: UUID
    json_before: dict[str, Any] | None = None
    json_after: dict[str, Any] | None = None
    category_id: UUID | None = None
    queue_priority: int | None = Field(default=None, ge=1, le=4)


class ClaimQueueRequest(BaseModel):
    queue_priority: int | None = Field(default=None, ge=1, le=4)
    category_ids: list[UUID] | None = None


class FieldReportInput(BaseModel):
    field_name: str | None = None
    sku_id: UUID | None = None
    comment: str | None = Field(default=None, max_length=500)
    field_path: str | None = None
    message: str | None = Field(default=None, max_length=1000)
    severity: Literal["INFO", "WARNING", "ERROR"] | None = None


class BlockDecisionRequest(BaseModel):
    blocking_reason_id: UUID | None = None
    blocking_reason_ids: list[UUID] | None = None
    moderator_comment: str | None = Field(default=None, max_length=1000)
    comment: str | None = Field(default=None, max_length=2000)
    field_reports: list[FieldReportInput] = Field(default_factory=list)

    @property
    def reason_id(self) -> str:
        if self.blocking_reason_id is None and not self.blocking_reason_ids:
            raise business_error("BLOCKING_REASON_REQUIRED", "blocking_reason_id is required")
        reason_id = self.blocking_reason_id or self.blocking_reason_ids[0]
        return str(reason_id)

    @property
    def decision_comment(self) -> str:
        comment = self.moderator_comment if self.moderator_comment is not None else self.comment
        return (comment or "").strip()


class ApproveDecisionRequest(BaseModel):
    moderator_comment: str | None = Field(default=None, max_length=1000)
    comment: str | None = Field(default=None, max_length=2000)

    @property
    def decision_comment(self) -> str | None:
        comment = self.moderator_comment if self.moderator_comment is not None else self.comment
        return comment.strip() if comment and comment.strip() else None


class BlockingReasonCreateRequest(BaseModel):
    code: str = Field(pattern=r"^[A-Z_]+$", max_length=64)
    title: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    hard_block: bool


class BlockingReasonUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None


@dataclass(frozen=True)
class ProductEvent:
    event_type: str
    idempotency_key: str
    occurred_at: datetime
    product_id: str
    seller_id: str | None
    category_id: str | None
    queue_priority: int | None
    json_after: dict[str, Any] | None


@dataclass(frozen=True)
class BlockingReason:
    id: str
    code: str
    title: str
    description: str | None
    hard_block: bool
    is_active: bool


class ProductEventRepository:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.getenv("MODERATION_DB_PATH", DEFAULT_DB_PATH)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init_db(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS product_moderation (
                    id TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL UNIQUE,
                    seller_id TEXT NOT NULL,
                    category_id TEXT,
                    status TEXT NOT NULL,
                    queue_priority INTEGER NOT NULL CHECK (queue_priority BETWEEN 1 AND 4),
                    json_before TEXT,
                    json_after TEXT NOT NULL,
                    blocking_reason_id TEXT,
                    moderator_id TEXT,
                    moderator_comment TEXT,
                    date_created TEXT NOT NULL,
                    date_updated TEXT NOT NULL,
                    date_moderation TEXT,
                    total_active_quantity INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (blocking_reason_id)
                        REFERENCES product_blocking_reasons(id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS product_moderation_field_report (
                    id TEXT PRIMARY KEY,
                    product_moderation_id TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    sku_id TEXT,
                    comment TEXT NOT NULL,
                    date_created TEXT NOT NULL,
                    FOREIGN KEY (product_moderation_id)
                        REFERENCES product_moderation(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS product_blocking_reasons (
                    id TEXT PRIMARY KEY,
                    code TEXT,
                    title TEXT NOT NULL,
                    description TEXT,
                    hard_block INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS processed_product_events (
                    idempotency_key TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    date_processed TEXT NOT NULL
                );
                """
            )
            self._ensure_blocking_reason_schema(connection)
            self._seed_blocking_reasons(connection)

    def reset(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM product_moderation_field_report")
            connection.execute("DELETE FROM product_moderation")
            connection.execute("DELETE FROM processed_product_events")
            connection.execute("DELETE FROM product_blocking_reasons")
            self._seed_blocking_reasons(connection)

    def _ensure_blocking_reason_schema(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(product_blocking_reasons)").fetchall()
        }
        if "code" not in columns:
            connection.execute("ALTER TABLE product_blocking_reasons ADD COLUMN code TEXT")
        if "description" not in columns:
            connection.execute("ALTER TABLE product_blocking_reasons ADD COLUMN description TEXT")
        if "is_active" not in columns:
            connection.execute(
                "ALTER TABLE product_blocking_reasons ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_product_blocking_reasons_code "
            "ON product_blocking_reasons(code)"
        )

    def _seed_blocking_reasons(self, connection: sqlite3.Connection) -> None:
        connection.executemany(
            """
            INSERT INTO product_blocking_reasons (id, code, title, description, hard_block, is_active)
            VALUES (?, ?, ?, NULL, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                code = excluded.code,
                title = excluded.title,
                hard_block = excluded.hard_block
            """,
            [
                (reason_id, BLOCKING_REASON_CODES[reason_id], title, int(hard_block))
                for reason_id, title, hard_block in BLOCKING_REASONS
            ],
        )

    def get_card(self, product_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM product_moderation WHERE product_id = ?",
                (product_id,),
            ).fetchone()
        return self._card_from_row(row) if row else None

    def list_blocking_reasons(
        self,
        *,
        hard_block: bool | None = None,
        is_active: bool | None = True,
    ) -> list[dict[str, Any]]:
        filters = []
        params: list[Any] = []
        if hard_block is not None:
            filters.append("hard_block = ?")
            params.append(int(hard_block))
        if is_active is not None:
            filters.append("is_active = ?")
            params.append(int(is_active))

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""
            SELECT *
            FROM product_blocking_reasons
            {where_clause}
            ORDER BY hard_block ASC, title ASC
        """
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._blocking_reason_response_from_row(row) for row in rows]

    def create_blocking_reason(self, request: BlockingReasonCreateRequest) -> dict[str, Any]:
        reason_id = str(uuid4())
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO product_blocking_reasons (
                        id, code, title, description, hard_block, is_active
                    )
                    VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (
                        reason_id,
                        request.code,
                        request.title,
                        request.description,
                        int(request.hard_block),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM product_blocking_reasons WHERE id = ?",
                    (reason_id,),
                ).fetchone()
        except sqlite3.IntegrityError as error:
            raise conflict_error("BLOCKING_REASON_CODE_EXISTS", "Blocking reason code already exists") from error
        return self._blocking_reason_response_from_row(row)

    def update_blocking_reason(
        self,
        reason_id: str,
        request: BlockingReasonUpdateRequest,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM product_blocking_reasons WHERE id = ?",
                (reason_id,),
            ).fetchone()
            if row is None:
                raise not_found_error("BLOCKING_REASON_NOT_FOUND", "Blocking reason not found")

            connection.execute(
                """
                UPDATE product_blocking_reasons
                SET title = COALESCE(?, title),
                    description = CASE WHEN ? THEN ? ELSE description END,
                    is_active = COALESCE(?, is_active)
                WHERE id = ?
                """,
                (
                    request.title,
                    request.description is not None,
                    request.description,
                    int(request.is_active) if request.is_active is not None else None,
                    reason_id,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM product_blocking_reasons WHERE id = ?",
                (reason_id,),
            ).fetchone()
        return self._blocking_reason_response_from_row(updated_row)

    def deactivate_blocking_reason(self, reason_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE product_blocking_reasons SET is_active = 0 WHERE id = ?",
                (reason_id,),
            )
            if cursor.rowcount == 0:
                raise not_found_error("BLOCKING_REASON_NOT_FOUND", "Blocking reason not found")

    def create_test_card(
        self,
        *,
        product_id: str,
        seller_id: str,
        status_value: str,
        json_after: dict[str, Any],
        queue_priority: int = 1,
        moderator_id: str | None = None,
        blocking_reason_id: str | None = None,
        date_created: str | None = None,
        date_moderation: str | None = None,
    ) -> None:
        now = now_iso()
        created_at = date_created or now
        clean_after = strip_private_fields(json_after)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO product_moderation (
                    id, product_id, seller_id, category_id, status, queue_priority,
                    json_before, json_after, blocking_reason_id, moderator_id,
                    moderator_comment, date_created, date_updated, date_moderation,
                    total_active_quantity
                )
                VALUES (?, ?, ?, NULL, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    product_id,
                    seller_id,
                    status_value,
                    queue_priority,
                    dump_json(clean_after),
                    blocking_reason_id,
                    moderator_id,
                    created_at,
                    now,
                    date_moderation,
                    total_active_quantity(clean_after),
                ),
            )

    def add_test_field_report(self, product_id: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM product_moderation WHERE product_id = ?",
                (product_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Card not found")
            connection.execute(
                """
                INSERT INTO product_moderation_field_report (
                    id, product_moderation_id, field_name, sku_id, comment, date_created
                )
                VALUES (?, ?, 'title', NULL, 'bad title', ?)
                """,
                (str(uuid4()), row["id"], now_iso()),
            )

    def count_field_reports(self, product_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM product_moderation_field_report fr
                JOIN product_moderation pm ON pm.id = fr.product_moderation_id
                WHERE pm.product_id = ?
                """,
                (product_id,),
            ).fetchone()
        return int(row["total"])

    def get_field_reports(self, product_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT fr.field_name, fr.sku_id, fr.comment
                FROM product_moderation_field_report fr
                JOIN product_moderation pm ON pm.id = fr.product_moderation_id
                WHERE pm.product_id = ?
                ORDER BY fr.date_created ASC, fr.id ASC
                """,
                (product_id,),
            ).fetchall()
        return [
            {
                "field_name": row["field_name"],
                "sku_id": row["sku_id"],
                "comment": row["comment"],
            }
            for row in rows
        ]

    def block_card(
        self,
        *,
        product_id: str | None = None,
        ticket_id: str | None = None,
        moderator_id: str,
        reason_id: str,
        moderator_comment: str,
        field_reports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_decision_card(connection, product_id=product_id, ticket_id=ticket_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ErrorResponse(code="NOT_FOUND", message="Product moderation card not found").model_dump(),
                )
            self._validate_decision_card(row, moderator_id)

            reason = self._get_blocking_reason(connection, reason_id)
            if reason is None:
                raise business_error("BLOCKING_REASON_NOT_FOUND", "Blocking reason not found")

            status_value = "HARD_BLOCKED" if reason.hard_block else "BLOCKED"
            now = now_iso()
            connection.execute(
                """
                UPDATE product_moderation
                SET status = ?,
                    blocking_reason_id = ?,
                    moderator_comment = ?,
                    date_moderation = ?,
                    date_updated = ?
                WHERE id = ?
                """,
                (status_value, reason.id, moderator_comment, now, now, row["id"]),
            )
            connection.execute(
                "DELETE FROM product_moderation_field_report WHERE product_moderation_id = ?",
                (row["id"],),
            )
            connection.executemany(
                """
                INSERT INTO product_moderation_field_report (
                    id, product_moderation_id, field_name, sku_id, comment, date_created
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(uuid4()),
                        row["id"],
                        report["field_name"],
                        report.get("sku_id"),
                        report["comment"],
                        now,
                    )
                    for report in field_reports
                ],
            )

            send_moderation_event_to_b2b(
                product_id=row["product_id"],
                moderator_id=moderator_id,
                reason_id=reason.id,
                moderator_comment=moderator_comment,
                field_reports=field_reports,
                hard_block=reason.hard_block,
            )
            updated_row = connection.execute(
                "SELECT * FROM product_moderation WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
            return self._ticket_response_from_row(updated_row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def approve_card(
        self,
        *,
        product_id: str | None = None,
        ticket_id: str | None = None,
        moderator_id: str,
        moderator_comment: str | None,
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_decision_card(connection, product_id=product_id, ticket_id=ticket_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ErrorResponse(code="NOT_FOUND", message="Product moderation card not found").model_dump(),
                )
            self._validate_decision_card(row, moderator_id)

            b2b_product = get_b2b_product(row["product_id"])
            if not b2b_product.get("skus"):
                raise conflict_error("PRODUCT_WITHOUT_SKU", "Product has no SKUs, cannot approve")

            now = now_iso()
            connection.execute(
                """
                UPDATE product_moderation
                SET status = 'APPROVED',
                    date_moderation = ?,
                    moderator_comment = ?,
                    blocking_reason_id = NULL,
                    date_updated = ?
                WHERE id = ?
                """,
                (now, moderator_comment, now, row["id"]),
            )
            connection.execute(
                "DELETE FROM product_moderation_field_report WHERE product_moderation_id = ?",
                (row["id"],),
            )
            send_approve_event_to_b2b(
                product_id=row["product_id"],
                moderator_id=moderator_id,
                moderator_comment=moderator_comment,
            )
            updated_row = connection.execute(
                "SELECT * FROM product_moderation WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
            return self._ticket_response_from_row(updated_row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def process_event(self, event: ProductEvent) -> bool:
        with self.connect() as connection:
            if self._event_processed(connection, event.idempotency_key):
                return True

            card = connection.execute(
                "SELECT * FROM product_moderation WHERE product_id = ?",
                (event.product_id,),
            ).fetchone()

            if event.event_type == "PRODUCT_CREATED":
                self._process_created(connection, event, card)
            elif event.event_type == "PRODUCT_EDITED":
                self._process_edited(connection, event, card)
            elif event.event_type == "PRODUCT_DELETED":
                self._process_deleted(connection, event.product_id)
            else:
                raise business_error("UNKNOWN_EVENT", "Unsupported product event")

            connection.execute(
                """
                INSERT INTO processed_product_events (
                    idempotency_key, product_id, event_type, occurred_at, date_processed
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.idempotency_key,
                    event.product_id,
                    event.event_type,
                    event.occurred_at.isoformat(),
                    now_iso(),
                ),
            )
            return False

    def claim_next_card(
        self,
        *,
        moderator_id: str,
        queue_priority: int | None = None,
        category_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._return_expired_reviews(connection)

            active_card = connection.execute(
                """
                SELECT 1
                FROM product_moderation
                WHERE status = 'IN_REVIEW' AND moderator_id = ?
                LIMIT 1
                """,
                (moderator_id,),
            ).fetchone()
            if active_card is not None:
                raise conflict_error(
                    "MODERATOR_ALREADY_HAS_IN_REVIEW",
                    "Moderator already has an active IN_REVIEW ticket",
                )

            where_parts = ["status = 'PENDING'"]
            params: list[Any] = []
            if queue_priority is not None:
                where_parts.append("queue_priority = ?")
                params.append(queue_priority)
            if category_ids:
                placeholders = ", ".join("?" for _ in category_ids)
                where_parts.append(f"category_id IN ({placeholders})")
                params.extend(category_ids)

            row = connection.execute(
                f"""
                SELECT *
                FROM product_moderation
                WHERE {" AND ".join(where_parts)}
                ORDER BY queue_priority ASC, date_created ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            now = now_iso()
            cursor = connection.execute(
                """
                UPDATE product_moderation
                SET status = 'IN_REVIEW',
                    moderator_id = ?,
                    date_moderation = ?,
                    date_updated = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (moderator_id, now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                raise conflict_error("TICKET_ALREADY_CLAIMED", "Ticket was already claimed")

            claimed = connection.execute(
                "SELECT * FROM product_moderation WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
            return self._ticket_response_from_row(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _return_expired_reviews(self, connection: sqlite3.Connection) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=in_review_timeout_minutes())
        now = now_iso()
        connection.execute(
            """
            UPDATE product_moderation
            SET status = 'PENDING',
                moderator_id = NULL,
                date_moderation = NULL,
                date_updated = ?
            WHERE status = 'IN_REVIEW'
              AND date_moderation IS NOT NULL
              AND date_moderation <= ?
            """,
            (now, cutoff.isoformat()),
        )

    def _event_processed(self, connection: sqlite3.Connection, idempotency_key: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM processed_product_events WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return row is not None

    def _process_created(
        self,
        connection: sqlite3.Connection,
        event: ProductEvent,
        card: sqlite3.Row | None,
    ) -> None:
        if card and card["status"] == "HARD_BLOCKED":
            return
        if card:
            raise business_error("PRODUCT_ALREADY_EXISTS", "Duplicate PRODUCT_CREATED event")
        if event.seller_id is None or event.json_after is None:
            raise business_error("INVALID_EVENT_PAYLOAD", "PRODUCT_CREATED requires seller_id and json_after")

        now = now_iso()
        json_after = strip_private_fields(event.json_after)
        connection.execute(
            """
            INSERT INTO product_moderation (
                id, product_id, seller_id, category_id, status, queue_priority,
                json_before, json_after, blocking_reason_id, moderator_id,
                moderator_comment, date_created, date_updated, date_moderation,
                total_active_quantity
            )
            VALUES (?, ?, ?, ?, 'PENDING', ?, NULL, ?, NULL, NULL, NULL, ?, ?, NULL, ?)
            """,
            (
                str(uuid4()),
                event.product_id,
                event.seller_id,
                event.category_id,
                event.queue_priority or 3,
                dump_json(json_after),
                now,
                now,
                total_active_quantity(json_after),
            ),
        )

    def _process_edited(
        self,
        connection: sqlite3.Connection,
        event: ProductEvent,
        card: sqlite3.Row | None,
    ) -> None:
        if card is None:
            raise business_error("PRODUCT_NOT_FOUND", "PRODUCT_EDITED event references unknown product")
        if card["status"] == "HARD_BLOCKED":
            return
        if event.seller_id is None or event.json_after is None:
            raise business_error("INVALID_EVENT_PAYLOAD", "PRODUCT_EDITED requires seller_id and json_after")

        old_status = card["status"]
        json_before = load_json(card["json_after"])
        json_after = strip_private_fields(event.json_after)
        active_quantity = total_active_quantity(json_after)
        queue_priority = next_queue_priority(old_status, card["queue_priority"], active_quantity)

        connection.execute(
            """
            UPDATE product_moderation
            SET seller_id = ?,
                category_id = ?,
                status = 'PENDING',
                queue_priority = ?,
                json_before = ?,
                json_after = ?,
                moderator_id = NULL,
                date_updated = ?,
                total_active_quantity = ?
            WHERE product_id = ?
            """,
            (
                event.seller_id,
                event.category_id or card["category_id"],
                queue_priority,
                dump_json(json_before),
                dump_json(json_after),
                now_iso(),
                active_quantity,
                event.product_id,
            ),
        )
        connection.execute(
            """
            DELETE FROM product_moderation_field_report
            WHERE product_moderation_id = ?
            """,
            (card["id"],),
        )

    def _process_deleted(
        self,
        connection: sqlite3.Connection,
        product_id: str,
    ) -> None:
        connection.execute(
            "DELETE FROM product_moderation WHERE product_id = ?",
            (product_id,),
        )

    def _get_decision_card(
        self,
        connection: sqlite3.Connection,
        *,
        product_id: str | None = None,
        ticket_id: str | None = None,
    ) -> sqlite3.Row | None:
        if product_id is not None:
            return connection.execute(
                "SELECT * FROM product_moderation WHERE product_id = ?",
                (product_id,),
            ).fetchone()
        if ticket_id is not None:
            return connection.execute(
                "SELECT * FROM product_moderation WHERE id = ?",
                (ticket_id,),
            ).fetchone()
        raise ValueError("product_id or ticket_id is required")

    def _validate_decision_card(self, row: sqlite3.Row, moderator_id: str) -> None:
        if row["status"] == "HARD_BLOCKED":
            raise forbidden_error("PRODUCT_HARD_BLOCKED", "Product is permanently blocked")
        if row["status"] != "IN_REVIEW":
            raise conflict_error("PRODUCT_NOT_IN_REVIEW", "Product is not in review")
        if row["moderator_id"] != moderator_id:
            raise forbidden_error("NOT_ASSIGNED_TO_YOU", "Product moderation card is not assigned to you")

    def _get_blocking_reason(self, connection: sqlite3.Connection, reason_id: str) -> BlockingReason | None:
        row = connection.execute(
            "SELECT * FROM product_blocking_reasons WHERE id = ? AND is_active = 1",
            (reason_id,),
        ).fetchone()
        if row is None:
            return None
        return BlockingReason(
            id=row["id"],
            code=row["code"],
            title=row["title"],
            description=row["description"],
            hard_block=bool(row["hard_block"]),
            is_active=bool(row["is_active"]),
        )

    def _blocking_reason_response_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "code": row["code"],
            "title": row["title"],
            "description": row["description"],
            "hard_block": bool(row["hard_block"]),
            "is_active": bool(row["is_active"]),
        }

    def _blocked_response(
        self,
        *,
        product_id: str,
        reason: BlockingReason,
        moderator_comment: str,
        field_reports: list[dict[str, Any]],
        hard_block: bool,
    ) -> dict[str, Any]:
        return {
            "product_id": product_id,
            "status": "HARD_BLOCKED" if hard_block else "BLOCKED",
            "hard_block": hard_block,
            "blocking_reason": {
                "id": reason.id,
                "title": reason.title,
                "comment": moderator_comment,
            },
            "field_reports": field_reports,
        }

    def _card_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "product_id": row["product_id"],
            "seller_id": row["seller_id"],
            "category_id": row["category_id"],
            "status": row["status"],
            "queue_priority": row["queue_priority"],
            "json_before": load_json(row["json_before"]) if row["json_before"] else None,
            "json_after": load_json(row["json_after"]),
            "blocking_reason_id": row["blocking_reason_id"],
            "moderator_id": row["moderator_id"],
            "moderator_comment": row["moderator_comment"],
            "date_created": row["date_created"],
            "date_updated": row["date_updated"],
            "date_moderation": row["date_moderation"],
            "total_active_quantity": row["total_active_quantity"],
        }

    def _ticket_response_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        claimed_at = row["date_moderation"]
        return {
            "id": row["id"],
            "product_id": row["product_id"],
            "seller_id": row["seller_id"],
            "category_id": row["category_id"],
            "kind": "EDIT" if row["json_before"] else "CREATE",
            "status": row["status"],
            "queue_priority": row["queue_priority"],
            "assigned_moderator_id": row["moderator_id"],
            "claimed_at": claimed_at,
            "claim_expires_at": claim_expires_at(claimed_at),
            "decision_at": None,
            "created_at": row["date_created"],
            "updated_at": row["date_updated"],
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def in_review_timeout_minutes() -> int:
    raw_value = os.getenv("MODERATION_IN_REVIEW_TIMEOUT_MINUTES")
    if raw_value is None:
        return DEFAULT_IN_REVIEW_TIMEOUT_MINUTES
    return max(1, int(raw_value))


def claim_expires_at(claimed_at: str | None) -> str | None:
    if claimed_at is None:
        return None
    return (datetime.fromisoformat(claimed_at) + timedelta(minutes=in_review_timeout_minutes())).isoformat()


def dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_json(value: str) -> dict[str, Any]:
    return json.loads(value)


def strip_private_fields(product_data: dict[str, Any]) -> dict[str, Any]:
    clean_product = dict(product_data)
    clean_skus = []
    for sku in clean_product.get("skus", []):
        clean_sku = dict(sku)
        clean_sku.pop("cost_price", None)
        clean_sku.pop("reserved_quantity", None)
        clean_skus.append(clean_sku)
    clean_product["skus"] = clean_skus
    return clean_product


def total_active_quantity(product_data: dict[str, Any]) -> int:
    total = 0
    for sku in product_data.get("skus", []):
        total += int(sku.get("active_quantity", sku.get("activeQuantity", 0)) or 0)
    return total


def next_queue_priority(old_status: str, current_priority: int, active_quantity: int) -> int:
    if old_status == "BLOCKED":
        return 2
    if old_status in {"MODERATED", "APPROVED"}:
        return 3 if active_quantity > 0 else 4
    return current_priority


def business_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorResponse(code=code, message=message).model_dump(),
    )


def conflict_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=ErrorResponse(code=code, message=message).model_dump(),
    )


def not_found_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorResponse(code=code, message=message).model_dump(),
    )


def forbidden_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=ErrorResponse(code=code, message=message).model_dump(),
    )


def require_service_key(x_service_key: str | None) -> None:
    expected = os.getenv("B2B_TO_MOD_KEY", DEFAULT_B2B_TO_MOD_KEY)
    if x_service_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ErrorResponse(
                code="UNAUTHORIZED",
                message=f"Missing or invalid {SERVICE_KEY_HEADER}",
            ).model_dump(),
        )


def require_moderator_id(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ErrorResponse(code="UNAUTHORIZED", message="Missing bearer token").model_dump(),
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return str(UUID(token))
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ErrorResponse(code="UNAUTHORIZED", message="Invalid bearer token").model_dump(),
        ) from error


def normalize_field_reports(raw_reports: list[FieldReportInput]) -> list[dict[str, Any]]:
    normalized_reports: list[dict[str, Any]] = []
    for report in raw_reports:
        field_name = report.field_name or field_name_from_path(report.field_path)
        if field_name not in ALLOWED_FIELD_NAMES:
            raise business_error("INVALID_FIELD_NAME", "field_reports.field_name is invalid")

        comment = report.comment if report.comment is not None else report.message
        if comment is None or not comment.strip():
            raise business_error("INVALID_FIELD_REPORT", "field_reports.comment is required")

        normalized_reports.append(
            {
                "field_name": field_name,
                "sku_id": str(report.sku_id) if report.sku_id else None,
                "comment": comment.strip(),
            }
        )
    return normalized_reports


def field_name_from_path(field_path: str | None) -> str | None:
    if field_path is None:
        return None
    path = field_path.strip()
    if path in ALLOWED_FIELD_NAMES:
        return path
    if path.startswith("images") or path.startswith("product_images"):
        return "product_images"
    if path.startswith("category"):
        return "category"
    if path.startswith("skus"):
        if ".name" in path:
            return "sku_name"
        if ".image" in path or ".images" in path:
            return "sku_image"
        if ".price" in path:
            return "sku_price"
    return path if path in ALLOWED_FIELD_NAMES else None


def b2b_unavailable_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=ErrorResponse(code="B2B_UNAVAILABLE", message="B2B service is unavailable").model_dump(),
    )


def get_b2b_product(product_id: str) -> dict[str, Any]:
    b2b_url = os.getenv("B2B_URL", DEFAULT_B2B_URL).rstrip("/")
    timeout = float(os.getenv("B2B_TIMEOUT_SECONDS", str(DEFAULT_B2B_TIMEOUT_SECONDS)))
    try:
        response = httpx.get(
            f"{b2b_url}/api/v1/products/{product_id}",
            headers={"X-Service-Key": os.getenv("MOD_TO_B2B_KEY", DEFAULT_MOD_TO_B2B_KEY)},
            timeout=timeout,
        )
    except httpx.HTTPError as error:
        raise b2b_unavailable_error() from error
    if response.status_code != status.HTTP_200_OK:
        raise b2b_unavailable_error()
    return response.json()


def send_approve_event_to_b2b(
    *,
    product_id: str,
    moderator_id: str,
    moderator_comment: str | None,
) -> None:
    b2b_url = os.getenv("B2B_URL", DEFAULT_B2B_URL).rstrip("/")
    timeout = float(os.getenv("B2B_TIMEOUT_SECONDS", str(DEFAULT_B2B_TIMEOUT_SECONDS)))
    payload = {
        "idempotency_key": str(uuid4()),
        "product_id": product_id,
        "event_type": "MODERATED",
        "occurred_at": now_iso(),
        "moderator_id": moderator_id,
        "moderator_comment": moderator_comment,
    }
    response = httpx.post(
        f"{b2b_url}/api/v1/moderation/events",
        json=payload,
        headers={"X-Service-Key": os.getenv("MOD_TO_B2B_KEY", DEFAULT_MOD_TO_B2B_KEY)},
        timeout=timeout,
    )
    response.raise_for_status()


def send_moderation_event_to_b2b(
    *,
    product_id: str,
    moderator_id: str,
    reason_id: str,
    moderator_comment: str,
    field_reports: list[dict[str, Any]],
    hard_block: bool,
) -> None:
    b2b_url = os.getenv("B2B_URL", DEFAULT_B2B_URL).rstrip("/")
    timeout = float(os.getenv("B2B_TIMEOUT_SECONDS", str(DEFAULT_B2B_TIMEOUT_SECONDS)))
    payload = {
        "idempotency_key": str(uuid4()),
        "product_id": product_id,
        "event_type": "BLOCKED",
        "occurred_at": now_iso(),
        "moderator_id": moderator_id,
        "moderator_comment": moderator_comment,
        "blocking_reason_id": reason_id,
        "hard_block": hard_block,
        "field_reports": field_reports,
    }
    response = httpx.post(
        f"{b2b_url}/api/v1/moderation/events",
        json=payload,
        headers={"X-Service-Key": os.getenv("MOD_TO_B2B_KEY", DEFAULT_MOD_TO_B2B_KEY)},
        timeout=timeout,
    )
    response.raise_for_status()


def parse_openapi_event(incoming: IncomingB2BEvent) -> ProductEvent:
    payload = incoming.payload
    return ProductEvent(
        event_type=incoming.event_type,
        idempotency_key=str(incoming.idempotency_key),
        occurred_at=incoming.occurred_at,
        product_id=str_required(payload, "product_id"),
        seller_id=str_optional(payload, "seller_id"),
        category_id=str_optional(payload, "category_id"),
        queue_priority=int_optional(payload, "queue_priority"),
        json_after=dict_optional(payload, "json_after"),
    )


def parse_canonical_event(incoming: CanonicalProductEvent) -> ProductEvent:
    event_type = {
        "CREATED": "PRODUCT_CREATED",
        "EDITED": "PRODUCT_EDITED",
        "DELETED": "PRODUCT_DELETED",
    }[incoming.event]
    default_priority = 1 if incoming.event == "CREATED" else incoming.queue_priority
    return ProductEvent(
        event_type=event_type,
        idempotency_key=str(incoming.idempotency_key),
        occurred_at=incoming.date,
        product_id=str(incoming.product_id),
        seller_id=str(incoming.seller_id) if incoming.seller_id else None,
        category_id=str(incoming.category_id) if incoming.category_id else None,
        queue_priority=default_priority,
        json_after=incoming.json_after,
    )


def str_required(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if value is None:
        raise business_error("INVALID_EVENT_PAYLOAD", f"{field_name} is required")
    return str(value)


def str_optional(payload: dict[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    return str(value) if value is not None else None


def int_optional(payload: dict[str, Any], field_name: str) -> int | None:
    value = payload.get(field_name)
    return int(value) if value is not None else None


def dict_optional(payload: dict[str, Any], field_name: str) -> dict[str, Any] | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise business_error("INVALID_EVENT_PAYLOAD", f"{field_name} must be an object")
    return value


repository = ProductEventRepository()
app = FastAPI(title="NeoMarket Moderation API")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.post("/api/v1/b2b/events", status_code=status.HTTP_202_ACCEPTED)
def receive_b2b_product_event(
    incoming: IncomingB2BEvent,
    response: Response,
    x_service_key: str | None = Header(default=None, alias=SERVICE_KEY_HEADER),
) -> dict[str, Any]:
    require_service_key(x_service_key)
    duplicate = repository.process_event(parse_openapi_event(incoming))
    response.status_code = status.HTTP_202_ACCEPTED
    return {"accepted": True, "duplicate": duplicate}


@app.post("/api/v1/events/product", status_code=status.HTTP_200_OK)
def receive_canonical_product_event(
    incoming: CanonicalProductEvent,
    x_service_key: str | None = Header(default=None, alias=SERVICE_KEY_HEADER),
) -> dict[str, Any]:
    require_service_key(x_service_key)
    duplicate = repository.process_event(parse_canonical_event(incoming))
    return {"accepted": True, "duplicate": duplicate}


@app.get("/api/v1/product-moderation/{product_id}")
def get_product_moderation(product_id: UUID) -> dict[str, Any]:
    card = repository.get_card(str(product_id))
    if card is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(code="NOT_FOUND", message="Product moderation card not found").model_dump(),
        )
    return card


@app.get("/api/v1/blocking-reasons")
def list_blocking_reasons(
    hard_block: bool | None = None,
    is_active: bool | None = True,
) -> list[dict[str, Any]]:
    return repository.list_blocking_reasons(hard_block=hard_block, is_active=is_active)


@app.get("/api/v1/product-blocking-reasons")
def list_product_blocking_reasons(
    hard_block: bool | None = None,
    is_active: bool | None = True,
) -> list[dict[str, Any]]:
    return repository.list_blocking_reasons(hard_block=hard_block, is_active=is_active)


@app.post("/api/v1/blocking-reasons", status_code=status.HTTP_201_CREATED)
def create_blocking_reason(
    request: BlockingReasonCreateRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    require_moderator_id(authorization)
    return repository.create_blocking_reason(request)


@app.patch("/api/v1/blocking-reasons/{reason_id}", status_code=status.HTTP_200_OK)
def update_blocking_reason(
    reason_id: UUID,
    request: BlockingReasonUpdateRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    require_moderator_id(authorization)
    return repository.update_blocking_reason(str(reason_id), request)


@app.delete("/api/v1/blocking-reasons/{reason_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_blocking_reason(
    reason_id: UUID,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Response:
    require_moderator_id(authorization)
    repository.deactivate_blocking_reason(str(reason_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/v1/queue/claim", status_code=status.HTTP_200_OK)
def claim_next_queue_ticket(
    response: Response,
    request: ClaimQueueRequest | None = None,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any] | None:
    moderator_id = require_moderator_id(authorization)
    claimed = repository.claim_next_card(
        moderator_id=moderator_id,
        queue_priority=request.queue_priority if request else None,
        category_ids=[str(cat_id) for cat_id in request.category_ids] if request and request.category_ids else None,
    )
    if claimed is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        return None
    return claimed


@app.post("/api/v1/products/{product_id}/decline", status_code=status.HTTP_200_OK)
def decline_product(
    product_id: UUID,
    request: BlockDecisionRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    moderator_id = require_moderator_id(authorization)
    return repository.block_card(
        product_id=str(product_id),
        moderator_id=moderator_id,
        reason_id=request.reason_id,
        moderator_comment=request.decision_comment,
        field_reports=normalize_field_reports(request.field_reports),
    )


@app.post("/api/v1/products/{product_id}/approve", status_code=status.HTTP_200_OK)
def approve_product(
    product_id: UUID,
    request: ApproveDecisionRequest | None = None,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    moderator_id = require_moderator_id(authorization)
    return repository.approve_card(
        product_id=str(product_id),
        moderator_id=moderator_id,
        moderator_comment=request.decision_comment if request else None,
    )


@app.post("/api/v1/tickets/{ticket_id}/approve", status_code=status.HTTP_200_OK)
def approve_ticket(
    ticket_id: UUID,
    request: ApproveDecisionRequest | None = None,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    moderator_id = require_moderator_id(authorization)
    return repository.approve_card(
        ticket_id=str(ticket_id),
        moderator_id=moderator_id,
        moderator_comment=request.decision_comment if request else None,
    )


@app.post("/api/v1/tickets/{ticket_id}/block", status_code=status.HTTP_200_OK)
def block_ticket(
    ticket_id: UUID,
    request: BlockDecisionRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    moderator_id = require_moderator_id(authorization)
    return repository.block_card(
        ticket_id=str(ticket_id),
        moderator_id=moderator_id,
        reason_id=request.reason_id,
        moderator_comment=request.decision_comment,
        field_reports=normalize_field_reports(request.field_reports),
    )
