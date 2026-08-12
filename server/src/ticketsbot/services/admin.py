from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from ..models import (AdminAudit, Attachment, BridgeSequence, NotificationOutbox,
                      SheetSyncOutbox, Ticket, TicketEvent, TicketTombstone, utcnow)

TERMINAL = {"решена", "отклонена"}


def ticket_snapshot(ticket: Ticket) -> dict:
    result = {}
    for column in Ticket.__table__.columns:
        value = getattr(ticket, column.name)
        result[column.name] = value.isoformat() if hasattr(value, "isoformat") else value
    return result


async def archive_ticket(session, number: str, session_id: str) -> Ticket:
    ticket = (await session.execute(select(Ticket).where(Ticket.number == number))).scalar_one_or_none()
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.archived_at is None:
        ticket.archived_at = utcnow()
        session.add(AdminAudit(session_id=session_id, action="archive_ticket",
                               ticket_number=number, payload_json="{}"))
    await session.commit()
    return ticket


async def delete_ticket(session, number: str, confirmation: str, session_id: str,
                        retention_days: int) -> TicketTombstone:
    if confirmation != number:
        raise HTTPException(400, "Exact ticket number confirmation is required")
    ticket = (await session.execute(select(Ticket).where(Ticket.number == number))).scalar_one_or_none()
    if not ticket:
        existing = (await session.execute(select(TicketTombstone).where(
            TicketTombstone.number == number))).scalar_one_or_none()
        if existing:
            return existing
        raise HTTPException(404, "Ticket not found")
    if ticket.status not in TERMINAL:
        raise HTTPException(409, "Only resolved or rejected tickets can be deleted")

    now = utcnow()
    stmt = insert(BridgeSequence).values(entity_key="ticket:" + number, version=1).on_conflict_do_update(
        index_elements=[BridgeSequence.entity_key], set_={"version": BridgeSequence.version + 1},
    ).returning(BridgeSequence.version)
    sequence = int((await session.execute(stmt)).scalar_one())
    tombstone = TicketTombstone(number=number, ticket_id=ticket.id,
                                snapshot_json=json.dumps(ticket_snapshot(ticket), ensure_ascii=False),
                                sequence=sequence, deleted_at=now)
    session.add(tombstone)
    session.add(AdminAudit(session_id=session_id, action="delete_ticket", ticket_number=number,
                           payload_json=json.dumps({"status": ticket.status}, ensure_ascii=False)))

    attachments = list((await session.execute(select(Attachment).where(
        Attachment.ticket_id == ticket.id))).scalars())
    for attachment in attachments:
        attachment.ticket_id = None
        attachment.quarantined_at = now
        attachment.retain_until = now + timedelta(days=retention_days)

    # Ticket FK data and stale pending upserts must disappear in this same transaction.
    await session.execute(delete(TicketEvent).where(TicketEvent.ticket_id == ticket.id))
    # Exact structured match; SQLite JSON extraction cannot confuse IDs 1 and 10.
    await session.execute(delete(NotificationOutbox).where(
        NotificationOutbox.payload_json.op("->>")("$.ticket_id") == ticket.id))
    await session.execute(delete(SheetSyncOutbox).where(
        SheetSyncOutbox.entity_type == "ticket", SheetSyncOutbox.entity_id == str(ticket.id)))
    dedupe = f"ticket-delete:{number}:{sequence}"
    session.add(SheetSyncOutbox(dedupe_key=dedupe, entity_type="ticket_tombstone",
                                entity_id=number, operation="delete_ticket",
                                payload_json=json.dumps({"number": number, "sequence": sequence})))
    await session.delete(ticket)
    await session.commit()
    return tombstone
