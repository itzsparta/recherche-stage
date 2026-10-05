"""Envoi SMTP et lecture IMAP. Les mots de passe viennent de variables
d'environnement (pour Gmail : un mot de passe d'application)."""

from __future__ import annotations

import email
import email.header
import email.message
import imaplib
import mimetypes
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Protocol

from ..config import ParamsIMAP, ParamsSMTP, secret
from ..sources import texte_brut


def construire_message(
    expediteur: str,
    destinataire: str,
    objet: str,
    corps: str,
    pieces: list[Path] = (),
    en_reponse_a: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = expediteur
    message["To"] = destinataire
    message["Subject"] = objet
    message["Date"] = formatdate(localtime=True)
    domaine = expediteur.rsplit("@", 1)[-1].strip(">") or "localhost"
    message["Message-ID"] = make_msgid(domain=domaine)
    if en_reponse_a:
        message["In-Reply-To"] = en_reponse_a
        message["References"] = en_reponse_a
    message.set_content(corps)
    for piece in pieces:
        type_mime, _ = mimetypes.guess_type(piece.name)
        principal, secondaire = (type_mime or "application/octet-stream").split("/", 1)
        message.add_attachment(piece.read_bytes(), maintype=principal, subtype=secondaire, filename=piece.name)
    return message


class Expediteur(Protocol):
    def envoyer(self, message: EmailMessage) -> str: ...


class ExpediteurSMTP:
    def __init__(self, params: ParamsSMTP):
        self.params = params

    def envoyer(self, message: EmailMessage) -> str:
        p = self.params
        contexte = ssl.create_default_context()
        if p.port == 465:
            serveur = smtplib.SMTP_SSL(p.hote, p.port, context=contexte, timeout=30)
        else:
            serveur = smtplib.SMTP(p.hote, p.port, timeout=30)
            serveur.starttls(context=contexte)
        with serveur:
            serveur.login(p.utilisateur, secret(p.mot_de_passe_env))
            serveur.send_message(message)
        return message["Message-ID"]


@dataclass
class MessageRecu:
    message_id: str
    expediteur: str
    objet: str
    corps: str
    date: datetime
    references: list[str]
    html: str = ""


class Boite(Protocol):
    def messages_depuis(self, depuis: datetime) -> list[MessageRecu]: ...


def _partie(message: email.message.Message, type_: str) -> str:
    for partie in message.walk():
        if partie.get_content_type() == type_ and not partie.get_filename():
            charge = partie.get_payload(decode=True) or b""
            return charge.decode(partie.get_content_charset() or "utf-8", errors="replace")
    return ""


def _corps_texte(message: email.message.Message) -> str:
    return _partie(message, "text/plain") or texte_brut(_partie(message, "text/html"))


class BoiteIMAP:
    def __init__(self, params: ParamsIMAP, dossier: str = ""):
        self.params = params.model_copy(update={"dossier": dossier}) if dossier else params

    def messages_depuis(self, depuis: datetime) -> list[MessageRecu]:
        p = self.params
        recus: list[MessageRecu] = []
        with imaplib.IMAP4_SSL(p.hote, p.port, ssl_context=ssl.create_default_context()) as boite:
            boite.login(p.utilisateur, secret(p.mot_de_passe_env))
            boite.select(p.dossier, readonly=True)
            _, donnees = boite.search(None, "SINCE", depuis.strftime("%d-%b-%Y"))
            for numero in donnees[0].split():
                _, brut = boite.fetch(numero, "(RFC822)")
                message = email.message_from_bytes(brut[0][1])
                try:
                    date = parsedate_to_datetime(message["Date"]).replace(tzinfo=None)
                except (TypeError, ValueError):
                    date = datetime.now()
                references = f"{message.get('In-Reply-To', '')} {message.get('References', '')}".split()
                recus.append(
                    MessageRecu(
                        message_id=message.get("Message-ID", ""),
                        expediteur=parseaddr(message.get("From", ""))[1].lower(),
                        objet=str(email.header.make_header(email.header.decode_header(message.get("Subject", "")))),
                        corps=_corps_texte(message),
                        date=date,
                        references=references,
                        html=_partie(message, "text/html"),
                    )
                )
        return recus
