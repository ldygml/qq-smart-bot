from __future__ import annotations

import logging
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .memory import ChatMemory
from .onebot import OneBotClient, extract_message_content
from .shared_memory import SharedMemoryStore

logger = logging.getLogger(__name__)

HISTORY_CURSOR_PREFIX = "history_sync:cursor:"
HISTORY_RESULT_STATE = "history_sync:last_result"
HISTORY_RUN_STATE = "history_sync:last_run_at"


@dataclass
class HistorySyncResult:
    started_at: int
    conversations: int = 0
    messages_seen: int = 0
    imported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors and not self.conversations:
            return "failed"
        if self.errors:
            return "partial"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": int(time.time()),
            "conversations": self.conversations,
            "messages_seen": self.messages_seen,
            "imported": self.imported,
            "skipped": self.skipped,
            "errors": self.errors[:20],
        }


def history_cursor_key(message_type: str, peer_id: int) -> str:
    return f"{HISTORY_CURSOR_PREFIX}{message_type}:{peer_id}"


async def sync_recent_history(
    *,
    client: OneBotClient,
    memory: ChatMemory,
    shared_memory: SharedMemoryStore,
    max_days: int = 30,
    max_pages: int = 10,
    page_size: int = 50,
    max_conversations: int = 40,
    max_rows: int = 160,
    observe_memory: bool = True,
    on_group_message: Callable[[int, str, str, str], None] | None = None,
) -> dict[str, Any]:
    """Import QQ messages received while the bot was offline.

    This function never sends a QQ message. It reads newest-to-oldest and stops
    at the last checkpoint, so restarting a bot does not replay old replies.
    """
    now = int(time.time())
    cutoff = now - max(1, int(max_days)) * 86400
    result = HistorySyncResult(started_at=now)
    try:
        contacts = await client.get_recent_contact(
            count=max(1, int(max_conversations))
        )
    except Exception as exc:
        message = f"recent contacts: {type(exc).__name__}: {exc}"
        logger.warning("Offline history sync could not list contacts: %s", exc)
        result.errors.append(message)
        return result.as_dict()

    eligible = [
        contact
        for contact in contacts
        if not int(contact.get("msg_time") or 0)
        or int(contact.get("msg_time") or 0) >= cutoff
    ]
    eligible.sort(
        key=lambda item: int(item.get("msg_time") or 0),
        reverse=True,
    )
    eligible = eligible[: max(1, int(max_conversations))]

    for contact in eligible:
        message_type = str(contact.get("message_type") or contact.get("chat_type"))
        peer_id = int(contact.get("peer_uin") or 0)
        contact_name = str(contact.get("name") or peer_id)
        if peer_id <= 0 or message_type not in {"private", "group"}:
            continue
        checkpoint = shared_memory.get_state(
            history_cursor_key(message_type, peer_id),
            "",
        )
        try:
            messages = await _fetch_history(
                client=client,
                message_type=message_type,
                peer_id=peer_id,
                checkpoint=checkpoint,
                cutoff=cutoff,
                max_pages=max_pages,
                page_size=page_size,
            )
            imported, skipped = _import_messages(
                messages=messages,
                message_type=message_type,
                peer_id=peer_id,
                contact_name=contact_name,
                memory=memory,
                shared_memory=shared_memory,
                max_rows=max_rows,
                observe_memory=observe_memory,
                on_group_message=on_group_message,
            )
            result.conversations += 1
            result.messages_seen += len(messages)
            result.imported += imported
            result.skipped += skipped
            if messages:
                newest_id = _message_id(messages[-1])
                if newest_id:
                    shared_memory.set_state(
                        history_cursor_key(message_type, peer_id),
                        newest_id,
                    )
        except Exception as exc:
            error = (
                f"{message_type}:{peer_id} ({contact_name}): "
                f"{type(exc).__name__}: {exc}"
            )
            logger.warning("Offline history sync failed for %s: %s", peer_id, exc)
            result.errors.append(error)

    payload = result.as_dict()
    shared_memory.set_state(HISTORY_RESULT_STATE, _json_dumps(payload))
    shared_memory.set_state(HISTORY_RUN_STATE, str(payload["finished_at"]))
    return payload


async def _fetch_history(
    *,
    client: OneBotClient,
    message_type: str,
    peer_id: int,
    checkpoint: str,
    cutoff: int,
    max_pages: int,
    page_size: int,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    known_ids: set[str] = set()
    cursor: int | str = 0
    for _ in range(max(1, int(max_pages))):
        if message_type == "group":
            page = await client.get_group_msg_history(
                peer_id,
                message_seq=cursor,
                count=page_size,
                reverse_order=True,
            )
        else:
            page = await client.get_friend_msg_history(
                peer_id,
                message_seq=cursor,
                count=page_size,
                reverse_order=True,
            )
        if not page:
            break

        page = [item for item in page if _message_id(item)]
        if not page:
            break
        checkpoint_index = -1
        if checkpoint:
            for index, item in enumerate(page):
                if _message_id(item) == checkpoint:
                    checkpoint_index = index
                    break

        older_page = page
        if checkpoint_index >= 0:
            older_page = page[checkpoint_index + 1:]

        older_page = [
            item
            for item in older_page
            if _message_id(item) not in known_ids
        ]
        for item in older_page:
            known_ids.add(_message_id(item))
        collected = older_page + collected

        oldest_at = min(
            (
                int(item.get("time") or 0)
                for item in page
                if int(item.get("time") or 0) > 0
            ),
            default=0,
        )
        if checkpoint_index >= 0:
            break
        if oldest_at and oldest_at < cutoff:
            break

        next_cursor = _message_id(page[0])
        if not next_cursor or str(next_cursor) == str(cursor):
            break
        cursor = next_cursor

    return [
        item
        for item in collected
        if not int(item.get("time") or 0)
        or int(item.get("time") or 0) >= cutoff
    ]


def _import_messages(
    *,
    messages: list[dict[str, Any]],
    message_type: str,
    peer_id: int,
    contact_name: str,
    memory: ChatMemory,
    shared_memory: SharedMemoryStore,
    max_rows: int,
    observe_memory: bool,
    on_group_message: Callable[[int, str, str, str], None] | None,
) -> tuple[int, int]:
    imported = 0
    skipped = 0
    last_group_user_id = 0
    for item in messages:
        message_id = _message_id(item)
        if not message_id:
            skipped += 1
            continue
        sender = item.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        try:
            sender_id = int(sender.get("user_id") or 0)
        except (TypeError, ValueError):
            sender_id = 0
        sender_name = str(
            sender.get("card")
            or sender.get("nickname")
            or (contact_name if message_type == "private" else sender_id)
        ).strip()[:120]
        role = (
            "assistant"
            if str(item.get("post_type") or "") == "message_sent"
            else "user"
        )
        content = extract_message_content(
            item.get("message"),
            self_id=sender_id if role == "assistant" else None,
            raw_message=str(item.get("raw_message") or ""),
        ).text.strip()
        if not content:
            content = str(item.get("raw_message") or "").strip()
        if not content:
            skipped += 1
            continue

        if message_type == "group":
            if role == "user" and sender_id > 0:
                last_group_user_id = sender_id
            thread_user_id = (
                sender_id if role == "user" else last_group_user_id
            )
            if thread_user_id <= 0:
                skipped += 1
                continue
            conversation_id = f"group:{peer_id}:user:{thread_user_id}"
        else:
            conversation_id = f"private:{peer_id}"
            if role == "user" and sender_id <= 0:
                sender_id = peer_id

        external_key = (
            f"qq:{message_type}:{peer_id}:{message_id}"
        )
        is_new = memory.archive_message(
            external_key=external_key,
            conversation_id=conversation_id,
            message_type=message_type,
            group_id=peer_id if message_type == "group" else None,
            sender_user_id=sender_id,
            sender_name=sender_name,
            role=role,
            content=content,
            event_time=int(item.get("time") or 0),
            # Offline messages are evidence for long-term memory, never the
            # active reply thread. Otherwise the bot can answer an old prompt
            # immediately after startup.
            add_to_chat=False,
            max_rows=max_rows,
        )
        if is_new:
            imported += 1
        else:
            skipped += 1

        if sender_id > 0 and role == "user":
            shared_memory.record_person(sender_id, sender_name)
        if (
            role == "user"
            and observe_memory
            and len(content) >= 4
            and not content.startswith("/")
        ):
            shared_memory.observe_message(
                conversation_id=conversation_id,
                message_type=message_type,
                group_id=peer_id if message_type == "group" else None,
                speaker_user_id=sender_id,
                speaker_name=sender_name,
                content=content,
            )
        if message_type == "group" and on_group_message is not None:
            on_group_message(peer_id, role, sender_name, content)
    return imported, skipped


def _message_id(item: dict[str, Any]) -> str:
    value = item.get("message_id") or item.get("id") or ""
    return str(value)


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
