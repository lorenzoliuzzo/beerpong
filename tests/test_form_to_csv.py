import csv
from pathlib import Path

import pytest

from tools.form_to_csv import parse_phones, read_form, split_players, write_csv

# All names and numbers below are invented: real registrations never belong in the repository.
FORM_HEADER = [
    "Timestamp", "Vuoi partecipare al torneo oppure fare serata?", "Nome squadra",
    "Nome dei due partecipanti della squadra", "Numero di telefono del referente", "Condividerai?", "Nome e numero", "Condividerai?",
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Gastone8 & Topolino veloce", ("Gastone8", "Topolino veloce")),
        ("Ada Rossi, Bice Teresa Verdi ", ("Ada Rossi", "Bice Teresa Verdi")),
        ("ugo e ines", ("Ugo", "Ines")),
        ("olga neri + tito ", ("Olga Neri", "Tito")),
        ("Rita Blu e Zeno Gialli", ("Rita Blu", "Zeno Gialli")),
    ],
)
def test_split_players_with_separator(raw: str, expected: tuple[str, str]) -> None:
    p1, p2, issues = split_players(raw)
    assert (p1, p2) == expected and issues == []


def test_split_players_without_separator_is_flagged() -> None:
    p1, p2, issues = split_players("Aldo baglio carla bruni")
    assert (p1, p2) == ("Aldo baglio", "Carla Bruni")
    assert issues and "controllare" in issues[0]
    p1, p2, issues = split_players("TizioX Caio Sempronio")
    assert p2 == "?" and "a mano" in issues[0]


def test_split_players_keeps_parenthesis_as_note() -> None:
    p1, p2, issues = split_players("Gino Pino (zio come riserva)")
    assert (p1, p2) == ("Gino", "Pino")
    assert any("zio come riserva" in i for i in issues)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3330000001", ["3330000001"]),
        ("333 000 0002 ", ["3330000002"]),
        ("+39 333 000 0003", ["3330000003"]),
        ("3330000004, +39 333 000 0005", ["3330000004", "3330000005"]),
        ("non ho il telefono", []),
        ("12345", []),
    ],
)
def test_parse_phones(raw: str, expected: list[str]) -> None:
    assert parse_phones(raw) == expected


def test_single_player_is_a_partial_couple() -> None:
    p1, p2, issues = split_players("ottavia")
    assert (p1, p2) == ("Ottavia", "") and "parziale" in issues[0]


def test_read_form_splits_couples_and_guests_and_merges_duplicates(tmp_path: Path) -> None:
    src = tmp_path / "form.csv"
    with src.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FORM_HEADER)
        w.writerow(["10/18/2026 21:56:14", "Torneo, spacco tutti", "le lontre ", "ada e bice ", "3330000010", "Sì", "", ""])
        w.writerow(["10/18/2026 21:57:05", "Torneo, spacco tutti", "le lontre!", "ada e bice", "3330000011", "No", "", ""])
        w.writerow(["10/19/2026 17:12:33", "Serata in tranquillità", "", "", "", "", "3330000012", "Sì"])
        w.writerow(["10/22/2026 15:37:06", "Serata in tranquillità", "", "", "", "", "Ottavia 333 000 0013", "Sì"])
        # Same phone as the first couple: already part of it, not a separate guest.
        w.writerow(["10/22/2026 16:00:00", "Serata in tranquillità", "", "", "", "", "3330000010", "Sì"])
        w.writerow(["10/21/2026 16:31:38", "Torneo, spacco tutti", "I Gufi", "Gufo Reale", "senza numero", "Sì", "", ""])

    teams, guests = read_form(src)
    assert [(g.name, g.phone, g.registered_at) for g in guests] == [
        ("", "3330000012", "2026-10-19T17:12"), ("Ottavia", "3330000013", "2026-10-22T15:37"),
    ]
    assert [t.name for t in teams] == ["le lontre", "I Gufi"]
    assert teams[0].phones == ["3330000010", "3330000011"]
    assert any("due volte" in i for i in teams[0].issues)
    assert any("telefono non valido" in i for i in teams[1].issues)

    out = tmp_path / "iscritti.csv"
    write_csv(teams, guests, out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "squadra;giocatore1;giocatore2;telefono1;telefono2;da_verificare;iscritto_il"
    assert lines[1].startswith("le lontre;Ada;Bice;3330000010;3330000011;")
    assert lines[-1] == ";Ottavia;;3330000013;;solo serata;2026-10-22T15:37"
