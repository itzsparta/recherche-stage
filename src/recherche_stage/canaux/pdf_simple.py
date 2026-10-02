"""Générateur PDF minimal pour la lettre (texte seul, Helvetica, A4),
sans dépendance externe. Encodage WinAnsi : couvre les caractères français."""

from __future__ import annotations

import textwrap
from pathlib import Path

LARGEUR, HAUTEUR = 595, 842  # A4 en points
MARGE, INTERLIGNE, CORPS = 64, 15, 11
LIGNES_PAR_PAGE = (HAUTEUR - 2 * MARGE) // INTERLIGNE
CARACTERES_PAR_LIGNE = 88


def _echapper(ligne: str) -> bytes:
    brut = ligne.encode("cp1252", errors="replace")
    return brut.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _lignes(texte: str) -> list[str]:
    lignes: list[str] = []
    for paragraphe in texte.replace("\r\n", "\n").split("\n"):
        lignes.extend(textwrap.wrap(paragraphe, CARACTERES_PAR_LIGNE) or [""])
    return lignes


def texte_vers_pdf(texte: str, chemin: str | Path) -> Path:
    lignes = _lignes(texte)
    pages = [lignes[i : i + LIGNES_PAR_PAGE] for i in range(0, len(lignes), LIGNES_PAR_PAGE)] or [[]]

    objets: list[bytes] = []
    # 1 catalogue, 2 arbre des pages, 3 police ; puis (page, contenu) par page
    ids_pages = [4 + 2 * i for i in range(len(pages))]
    objets.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objets.append(f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in ids_pages)}] /Count {len(pages)} >>".encode())
    objets.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for i, page in enumerate(pages):
        flux = [f"BT /F1 {CORPS} Tf {INTERLIGNE} TL {MARGE} {HAUTEUR - MARGE} Td".encode()]
        flux += [b"(" + _echapper(ligne) + b") Tj T*" for ligne in page]
        flux.append(b"ET")
        contenu = b"\n".join(flux)
        objets.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {LARGEUR} {HAUTEUR}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {ids_pages[i] + 1} 0 R >>".encode()
        )
        objets.append(f"<< /Length {len(contenu)} >>\nstream\n".encode() + contenu + b"\nendstream")

    sortie = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    positions = []
    for numero, objet in enumerate(objets, start=1):
        positions.append(len(sortie))
        sortie += f"{numero} 0 obj\n".encode() + objet + b"\nendobj\n"
    debut_xref = len(sortie)
    sortie += f"xref\n0 {len(objets) + 1}\n0000000000 65535 f \n".encode()
    sortie += b"".join(f"{p:010d} 00000 n \n".encode() for p in positions)
    sortie += f"trailer\n<< /Size {len(objets) + 1} /Root 1 0 R >>\nstartxref\n{debut_xref}\n%%EOF\n".encode()

    chemin = Path(chemin)
    chemin.write_bytes(bytes(sortie))
    return chemin
