"""Événements d'agenda au format iCalendar (.ics), importables dans Google
Agenda, Outlook ou l'agenda du téléphone."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


def _echapper(texte: str) -> str:
    return texte.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def evenement_ics(uid: str, debut: datetime, titre: str, description: str, duree: timedelta = timedelta(hours=1)) -> str:
    format_ = "%Y%m%dT%H%M%S"
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//recherche-stage//FR",
            "BEGIN:VEVENT",
            f"UID:{uid}@recherche-stage",
            f"DTSTAMP:{datetime.now().strftime(format_)}",
            f"DTSTART:{debut.strftime(format_)}",
            f"DTEND:{(debut + duree).strftime(format_)}",
            f"SUMMARY:{_echapper(titre)}",
            f"DESCRIPTION:{_echapper(description)}",
            "BEGIN:VALARM",
            "TRIGGER:-PT24H",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_echapper(titre)}",
            "END:VALARM",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )


def ecrire_evenement(chemin: Path, uid: str, debut: datetime, titre: str, description: str) -> Path:
    chemin.write_text(evenement_ics(uid, debut, titre, description), encoding="utf-8")
    return chemin
