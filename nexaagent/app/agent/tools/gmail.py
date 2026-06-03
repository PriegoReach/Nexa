"""Tool de ENVÍO de correo con Gmail (P27).

EL PUNTO MÁS ALTO DE LA ESCALERA DE IRREVERSIBILIDAD del proyecto:
  - un correo enviado NO se deshace (un evento se borra; un correo ya llegó),
  - el daño es a una PERSONA REAL (un tercero), no a la propia cuenta,
  - y el 7B redacta el CONTENIDO (prosa para un humano, no args estructurados).
Por eso la confirmación NO es cosmética: es la única barrera entre "el 7B propuso"
y "un correo equivocado llegó a alguien". send_email SOLO PROPONE; el envío real
(perform_send_email) ocurre exclusivamente desde el registro CONFIRMABLE_ACTIONS
tras un "sí" explícito del usuario.

IDEMPOTENCIA — LADO EMISOR (obligado): la Gmail API users.messages.send NO admite
clave de idempotencia de cliente (a diferencia de Calendar, que acepta un `id` y
da 409 lado-receptor). Enviar el mismo raw dos veces entrega DOS correos. Por eso
replicamos webhook_events (P22-B): registrar-primero en sent_emails con UNIQUE
sobre idempotency_key; si choca, NO se reenvía. Sesgo a no-duplicar.

httpx directo + email.message de stdlib (criterio del proyecto: nada de
google-api-python-client pesado). Timeout explícito (lección P15).
"""
import asyncio
import base64
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

import httpx
from langchain_core.tools import tool
from sqlalchemy import text

from app.agent import pending
from app.agent.confirmable import confirmable_action
from app.core.log_context import conversation_id_var
from app.db.worker_db import worker_session
from app.integrations.google_oauth import NoGoogleAccount, get_valid_token

logger = logging.getLogger("nexa.tools")

_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_HTTP_TIMEOUT = 20

# Validación deliberadamente simple: "algo@algo.algo" sin espacios. No pretende
# validar RFC 5322 completo (imposible con regex); solo descarta basura evidente
# antes de guardar un pending (no agendamos un envío a un destinatario que no es
# ni un email).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _run_async(coro):
    """Puente síncrono->async (mismo molde que las demás tools): la tool es
    síncrona para langchain pero set_pending es async."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _idempotency_key(to: str, subject: str, body: str) -> str:
    """Clave sobre los VALORES normalizados (patrón P21/P22-B). Mismo correo
    (mismo destinatario+asunto+cuerpo) -> misma clave -> el UNIQUE bloquea el
    reenvío. A prueba de replay, no de correos legítimamente distintos."""
    basis = f"{to.strip().lower()}|{subject.strip().lower()}|{body.strip().lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _build_question(to: str, subject: str, body: str) -> str:
    """La pregunta de confirmación LITERAL: muestra los TRES campos y el cuerpo
    COMPLETO (no resumido). Este texto es el que el usuario verá y aprobará — y,
    gracias al override 'propuesta sin modelo' del orquestador, es EXACTAMENTE lo
    que se enviará. 'Lo mostrado = lo enviado'."""
    return (
        "Voy a enviar este correo:\n\n"
        f"Para: {to}\n"
        f"Asunto: {subject}\n\n"
        f"{body}\n\n"
        "¿Lo envío? Responde sí para confirmar o no para cancelar."
    )


def _build_mime(to: str, subject: str, body: str) -> str:
    """Construye el mensaje RFC 2822 (MIME) y lo codifica en base64url, como pide
    la Gmail API. EmailMessage de stdlib maneja el encoding UTF-8 del asunto
    (RFC 2047) y del cuerpo automáticamente. No fijamos From: Gmail usa la cuenta
    autenticada."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


@confirmable_action("send_email")
async def perform_send_email(args: dict) -> str:
    """El ENVÍO REAL del correo. Ejecutor del registro P27: firma (args: dict) -> str.
    NO lo llama el modelo: lo invoca la rama de confirmación del orquestador tras un
    "sí". `args` trae {to, subject, body}.

    Registrar-primero (P22-B): INSERT ON CONFLICT DO NOTHING. Si la clave ya existía
    -> ese correo ya se envió -> NO reenviar. Si insertó -> enviar y registrar el
    resultado real (sent/failed).
    """
    to = args["to"]
    subject = args.get("subject", "")
    body = args.get("body", "")
    key = _idempotency_key(to, subject, body)

    # PASO 1: registrar ANTES de enviar (sesgo a no-duplicar). row None = la clave
    # ya existe = el correo ya salió antes -> no se reenvía.
    async with worker_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO sent_emails (idempotency_key, to_addr, subject, status) "
                "VALUES (:key, :to, :subject, 'pending') "
                "ON CONFLICT (idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {"key": key, "to": to, "subject": subject},
        )
        row = result.first()
        await session.commit()
    if row is None:
        logger.info("send_email duplicate ignored", extra={"idempotency_key": key})
        return "Ese correo ya se había enviado (no lo reenvié)."
    email_id = row[0]

    # PASO 2: obtener token y enviar vía Gmail API (fuera de la sesión; el registro
    # ya está commiteado, así que el sesgo a no-duplicar se mantiene aun si esto falla).
    try:
        token = await get_valid_token()
    except NoGoogleAccount:
        await _mark_status(email_id, "failed")
        return ("No hay una cuenta de Google conectada. Conéctala primero "
                "(autorización OAuth) y vuelve a intentarlo.")

    raw = _build_mime(to, subject, body)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_GMAIL_SEND_URL, json={"raw": raw}, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        await _mark_status(email_id, "failed")
        code = exc.response.status_code
        logger.warning("gmail send failed", extra={"status": code, "email_id": email_id})
        if code in (401, 403):
            return ("Google rechazó el envío (token o permisos de Gmail). "
                    "Reconecta la cuenta.")
        return "No pude enviar el correo ahora mismo (error de Gmail); quedó registrado como fallido."
    except httpx.HTTPError as exc:
        await _mark_status(email_id, "failed")
        logger.warning("gmail send http error", extra={"email_id": email_id, "exc": str(exc)})
        return "No pude contactar a Gmail (problema de red); el correo quedó registrado como fallido."

    # PASO 3: registrar el resultado real.
    await _mark_status(email_id, "sent")
    logger.info("send_email executed", extra={"email_id": email_id, "idempotency_key": key})
    return f"Correo enviado a {to} (asunto: '{subject}')."


async def _mark_status(email_id: int, status: str) -> None:
    async with worker_session() as session:
        await session.execute(
            text("UPDATE sent_emails SET status = :s WHERE id = :id"),
            {"s": status, "id": email_id},
        )
        await session.commit()


@tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email on the user's behalf via Gmail. Use this whenever the user
    asks to send/write an email or message to someone (e.g. "envía un correo a
    juan@x.com diciendo que...", "escríbele a María que...").
    This does NOT send the email directly: it shows the full email (recipient,
    subject and body) and asks the user to confirm; it is sent only after they agree.

    Args:
        to: The recipient's email address.
        subject: A short subject line for the email.
        body: The full body text of the email, written in plain language.
    """
    to = to.strip()
    if not _EMAIL_RE.match(to):
        # Destinatario inválido -> NO se guarda pending (no agendamos un envío a
        # algo que no es un email). El modelo recibe un error claro.
        return (f"'{to}' no parece una dirección de correo válida. "
                "Dame un email con el formato nombre@dominio.com.")

    question = _build_question(to, subject, body)
    description = f"el envío del correo a {to} (asunto: '{subject}')"

    cid_raw = conversation_id_var.get()
    conversation_id = int(cid_raw) if cid_raw and cid_raw != "-" else None
    if conversation_id is not None:
        # set_pending es async; la tool es síncrona para langchain -> puente.
        _run_async(
            pending.set_pending(
                conversation_id,
                {
                    "action": "send_email",
                    "args": {"to": to, "subject": subject, "body": body},
                    "description": description,
                    "question": question,   # texto LITERAL para el override (P27)
                },
            )
        )
    logger.info("send_email proposed", extra={"to": to})
    return question
