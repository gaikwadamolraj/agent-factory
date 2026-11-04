"""/api/v1/chats (tenant-scoped by X-Client-Id + X-Project-Id; a chat id is required for every message)."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from ats_common.db import get_session
from ats_common.db.repositories import TenantScope
from ats_common.errors import AppError
from ats_common.security import UserClaims
from orchestrator.api.deps import current_user, tenant_scope
from orchestrator.api.v1.schemas import (AbortOut, ChatCreate, ChatDetail, ChatOut, ChatUpdate, DocumentOut,
                                         DocumentTextOut, MessageOut)
from orchestrator.assistant.runner import TurnInput, start_turn
from orchestrator.constants import DEFAULT_CHAT_TITLE, SCREEN_DEFAULT_MESSAGE, TurnAction
from orchestrator.container import get_container
from orchestrator.errors import OrchErrors
from orchestrator.models import Chat, Document, DocumentKind
from orchestrator.repositories import ChatRepository, DocumentRepository
from orchestrator.services import documents
from orchestrator.services.documents import UploadedFile, detect_type
from orchestrator.services.documents.blob_store import blob_store

router = APIRouter(prefix="/chats", tags=["chats"])


def chat_out(chat: Chat) -> ChatOut:
    return ChatOut(id=chat.id, title=chat.title, has_job_description=bool(chat.jd_text),
                   has_resume=chat.resume_document_id is not None, last_score=chat.last_score,
                   created_at=chat.created_at, updated_at=chat.updated_at)


async def _owned(session: AsyncSession, scope: TenantScope, user: UserClaims, chat_id: str) -> Chat:
    chat = await ChatRepository(session, scope).get_owned(chat_id, user.user_id) if len(chat_id) <= 32 else None
    if chat is None:
        raise AppError(OrchErrors.CHAT_NOT_FOUND)
    return chat


async def _document(session: AsyncSession, scope: TenantScope, chat: Chat, kind: DocumentKind) -> Optional[Document]:
    document_id = chat.resume_document_id if kind == DocumentKind.RESUME else chat.jd_document_id
    return await DocumentRepository(session, scope).get_scoped(document_id) if document_id else None


async def _read_upload(upload: Optional[UploadFile], allow_text: bool) -> Optional[UploadedFile]:
    """Validate and read one uploaded file (size limit, real type from the bytes)."""
    if upload is None or not upload.filename:
        return None
    limit = get_container().settings.max_pdf_bytes
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise AppError(OrchErrors.FILE_TOO_LARGE)
    content_type = detect_type(upload.filename, upload.content_type, data, allow_text)
    return UploadedFile(filename=upload.filename[-255:], content_type=content_type, data=data)


@router.post("", response_model=ChatOut, response_model_by_alias=True, status_code=201)
async def create_chat(body: ChatCreate, user: UserClaims = Depends(current_user),
                      scope: TenantScope = Depends(tenant_scope), session: AsyncSession = Depends(get_session)):
    """Create a chat in the selected project (required before sending messages)."""
    return chat_out(await ChatRepository(session, scope).create(user.user_id, body.title or DEFAULT_CHAT_TITLE))


@router.get("", response_model=list[ChatOut], response_model_by_alias=True)
async def list_chats(user: UserClaims = Depends(current_user), scope: TenantScope = Depends(tenant_scope),
                     session: AsyncSession = Depends(get_session)):
    """The user's chats in the selected project, most recent first."""
    return [chat_out(c) for c in await ChatRepository(session, scope).list_for_user(user.user_id)]


@router.get("/{chat_id}", response_model=ChatDetail, response_model_by_alias=True)
async def get_chat(chat_id: str, user: UserClaims = Depends(current_user), scope: TenantScope = Depends(tenant_scope),
                   session: AsyncSession = Depends(get_session)):
    """Chat with its documents and full message history."""
    chat = await _owned(session, scope, user, chat_id)
    resume = await _document(session, scope, chat, DocumentKind.RESUME)
    jd = await _document(session, scope, chat, DocumentKind.JD)
    running = get_container().turns.running_for_chat(chat.id)
    messages = await ChatRepository(session, scope).list_messages(chat.id)
    return ChatDetail(**chat_out(chat).model_dump(), job_description=chat.jd_text,
                      resume_document=DocumentOut.model_validate(resume) if resume else None,
                      jd_document=DocumentOut.model_validate(jd) if jd else None,
                      running_turn_id=running.turn_id if running else None,
                      messages=[MessageOut.model_validate(m) for m in messages])


@router.patch("/{chat_id}", response_model=ChatOut, response_model_by_alias=True)
async def update_chat(chat_id: str, body: ChatUpdate, user: UserClaims = Depends(current_user),
                      scope: TenantScope = Depends(tenant_scope), session: AsyncSession = Depends(get_session)):
    """Rename a chat and/or replace its job description text."""
    chat = await _owned(session, scope, user, chat_id)
    if body.title is not None:
        chat.title = body.title
    if body.job_description is not None:
        jd_text = body.job_description.strip() or None
        chat.jd_text = jd_text
        chat.jd_document_id = (await documents.ingest_text(scope, user.user_id, DocumentKind.JD, jd_text)).id \
            if jd_text else None
    await ChatRepository(session, scope).touch(chat)
    return chat_out(chat)


@router.delete("/{chat_id}", status_code=204)
async def delete_chat(chat_id: str, user: UserClaims = Depends(current_user),
                      scope: TenantScope = Depends(tenant_scope), session: AsyncSession = Depends(get_session)):
    await ChatRepository(session, scope).delete(await _owned(session, scope, user, chat_id))


@router.get("/{chat_id}/documents/{kind}/file")
async def document_file(chat_id: str, kind: DocumentKind, user: UserClaims = Depends(current_user),
                        scope: TenantScope = Depends(tenant_scope), session: AsyncSession = Depends(get_session)):
    """Original uploaded file (for the document preview panel)."""
    document = await _document(session, scope, await _owned(session, scope, user, chat_id), kind)
    data = await blob_store(get_container().settings.blob_backend, session).get(scope, document.blob_key) \
        if document and document.blob_key else None
    if data is None:
        raise AppError(OrchErrors.DOCUMENT_NOT_FOUND)
    safe_name = "".join(ch for ch in document.filename if ch.isalnum() or ch in "._- ")[:120] or "document"
    return Response(content=data, media_type=document.content_type,
                    headers={"Content-Disposition": f'inline; filename="{safe_name}"',
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/{chat_id}/documents/{kind}/text", response_model=DocumentTextOut, response_model_by_alias=True)
async def document_text(chat_id: str, kind: DocumentKind, user: UserClaims = Depends(current_user),
                        scope: TenantScope = Depends(tenant_scope), session: AsyncSession = Depends(get_session)):
    """Extracted text of the chat's resume or job description."""
    chat = await _owned(session, scope, user, chat_id)
    document = await _document(session, scope, chat, kind)
    if document is None:
        if kind == DocumentKind.JD and chat.jd_text:
            return DocumentTextOut(kind=kind.value, filename="job-description.txt", text=chat.jd_text)
        raise AppError(OrchErrors.DOCUMENT_NOT_FOUND)
    return DocumentTextOut(kind=kind.value, filename=document.filename, text=document.text)


@router.post("/{chat_id}/messages")
async def send_message(
    chat_id: str,
    content: str = Form(""),
    action: Literal["chat", "screen"] = Form("chat"),
    job_description: Optional[str] = Form(None, alias="jobDescription"),
    resume_file: Optional[UploadFile] = File(None, alias="resumeFile"),
    jd_file: Optional[UploadFile] = File(None, alias="jdFile"),
    user: UserClaims = Depends(current_user),
    scope: TenantScope = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
):
    """Send a message, optionally with a resume PDF, a JD file (PDF/TXT/MD) or JD text. Streams Server-Sent Events:
    turn_started, user_message, progress/heartbeat..., then exactly one of message | aborted | error."""
    settings = get_container().settings
    await _owned(session, scope, user, chat_id)
    if get_container().turns.running_for_chat(chat_id):
        raise AppError(OrchErrors.TURN_IN_PROGRESS)

    resume_upload = await _read_upload(resume_file, allow_text=False)
    jd_upload = await _read_upload(jd_file, allow_text=True)
    content = content.strip()
    if len(content) > settings.max_message_chars or (job_description and len(job_description) > settings.max_jd_chars):
        raise AppError(OrchErrors.MESSAGE_TOO_LONG)
    if not content:
        if action != TurnAction.SCREEN and not (resume_upload or jd_upload):
            raise AppError(OrchErrors.MESSAGE_EMPTY)
        content, action = SCREEN_DEFAULT_MESSAGE, TurnAction.SCREEN

    turn = TurnInput(user=user, scope=scope, chat_id=chat_id, content=content, action=TurnAction(action),
                     job_description=job_description, resume_upload=resume_upload, jd_upload=jd_upload)
    return StreamingResponse(start_turn(turn), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{chat_id}/turns/{turn_id}/abort", response_model=AbortOut, response_model_by_alias=True)
async def abort_turn(chat_id: str, turn_id: str, user: UserClaims = Depends(current_user),
                     scope: TenantScope = Depends(tenant_scope), session: AsyncSession = Depends(get_session)):
    """Stop a running turn: cancels the assistant and the agent's LLM calls."""
    await _owned(session, scope, user, chat_id)
    turns = get_container().turns
    turn = turns.get(turn_id)
    if turn is None or turn.chat_id != chat_id or turn.user_id != user.user_id:
        raise AppError(OrchErrors.TURN_NOT_FOUND)
    await turns.abort(turn)
    return AbortOut(turn_id=turn_id, aborted=True)
