"""Turn the Google Form export into one clean CSV the app can import (Admin → Coppie iscritte).

Rows with a team name are couples (no second player = partial couple); rows without a team name are
party-only guests, who can still enter as a couple at check-in with the phone they left on the form.

Usage: python -m tools.form_to_csv "~/Downloads/Registrazione ... .csv" [-o data/iscritti.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

HEADER = ["squadra", "giocatore1", "giocatore2", "telefono1", "telefono2", "da_verificare", "iscritto_il"]
TOURNAMENT_ANSWER = "torneo"
PHONE_RE = re.compile(r"\+?\d[\d ]{7,}\d")
SEPARATORS = re.compile(r"\s*(?:&|,|\+|/|\be\b|\band\b)\s*", re.IGNORECASE)


@dataclass
class Team:
    name: str
    p1: str
    p2: str
    phones: list[str]
    issues: list[str] = field(default_factory=list)

    def row(self) -> list[str]:
        phones = self.phones + ["", ""]
        # " | " keeps the notes column free of the ";" delimiter the app splits on.
        return [self.name, self.p1, self.p2, phones[0], phones[1], " | ".join(self.issues)]


@dataclass
class Guest:
    name: str
    phone: str
    registered_at: str = ""

    def row(self) -> list[str]:
        return ["", self.name, "", self.phone, "", "solo serata", self.registered_at]


def form_timestamp(raw: str) -> str:
    """Google Forms exports "10/19/2026 21:37:49" (month first); the app stores ISO minutes."""
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y %H:%M:%S").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return ""


def clean(text: str) -> str:
    return " ".join(text.split())


def capitalize(name: str) -> str:
    # Only fix names typed all in lowercase; keep deliberate casing like "TizioX" or "LA".
    return " ".join(w[:1].upper() + w[1:] for w in name.split()) if name.islower() else name


def parse_phones(raw: str) -> list[str]:
    phones = []
    for match in PHONE_RE.findall(raw):
        digits = re.sub(r"\D", "", match)
        if digits.startswith("39") and len(digits) == 12:
            digits = digits[2:]
        if len(digits) == 10 and digits.startswith("3") and digits not in phones:
            phones.append(digits)
    return phones


def split_players(raw: str) -> tuple[str, str, list[str]]:
    issues = []
    text = clean(raw)
    note = re.findall(r"\(([^)]*)\)", text)
    if note:
        issues.append(f"nota: {', '.join(note)}")
        text = clean(re.sub(r"\([^)]*\)", "", text))
    parts = [p for p in SEPARATORS.split(text) if p]
    if len(parts) == 2:
        return capitalize(parts[0]), capitalize(parts[1]), issues
    words = text.split()
    if len(words) == 1:
        issues.append("coppia parziale: manca il compagno")
        return capitalize(text), "", issues
    if len(parts) == 1 and len(words) in (2, 4):
        half = len(words) // 2
        issues.append(f"giocatori senza separatore («{text}»): divisione da controllare")
        return capitalize(" ".join(words[:half])), capitalize(" ".join(words[half:])), issues
    issues.append(f"giocatori non divisibili («{text}»): da completare a mano")
    return capitalize(text), "?", issues


def similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio() >= 0.85


def read_form(path: Path) -> tuple[list[Team], list[Guest]]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    teams: list[Team] = []
    guests: list[Guest] = []
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        if TOURNAMENT_ANSWER not in row[1].casefold():
            phones = parse_phones(row[6])
            if phones:
                guests.append(Guest(capitalize(clean(re.sub(r"[+\d]", " ", row[6]))), phones[0], form_timestamp(row[0])))
            continue
        p1, p2, issues = split_players(row[3])
        phones = parse_phones(row[4])
        if not phones:
            issues.append(f"telefono non valido («{clean(row[4])}»)")
        teams.append(Team(clean(row[2]), p1, p2, phones, issues))
    teams = merge_duplicates(teams)
    # A guest whose phone already belongs to a couple was registered by (or with) that couple.
    team_phones = {ph for t in teams for ph in t.phones}
    return teams, [g for g in guests if g.phone not in team_phones]


def merge_duplicates(teams: list[Team]) -> list[Team]:
    merged: list[Team] = []
    for t in teams:
        same = next(
            (m for m in merged if similar(m.name, t.name) and similar(f"{m.p1} {m.p2}", f"{t.p1} {t.p2}")), None
        )
        if same is None:
            merged.append(t)
            continue
        same.phones += [p for p in t.phones if p not in same.phones]
        same.issues.append(f"iscritta due volte (anche come «{t.name}»): unite, tenuti entrambi i telefoni")
    return merged


def write_csv(teams: list[Team], guests: list[Guest], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(HEADER)
        writer.writerows(t.row() for t in teams)
        writer.writerows(g.row() for g in guests)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("form_csv", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("data/iscritti.csv"))
    args = parser.parse_args()

    teams, guests = read_form(args.form_csv.expanduser())
    write_csv(teams, guests, args.output)
    print(f"{len(teams)} coppie e {len(guests)} iscritti solo serata → {args.output}")
    for t in teams:
        for issue in t.issues:
            print(f"  ! {t.name}: {issue}")


if __name__ == "__main__":
    main()
