"""The few states the app moves through, named once.

They are ``StrEnum`` members, so they are stored in the database and sent to templates
as the same plain strings as before, but code and templates ask them questions
(``cfg.phase.is_setup``, ``m.mode.by_shots``) instead of comparing literals.
"""
from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    """Where the whole tournament is."""

    SETUP = "setup"  # registrations and check-in, before the draw
    GROUPS = "groups"
    KNOCKOUT = "knockout"
    FINISHED = "finished"

    @property
    def is_setup(self) -> bool:
        return self is Phase.SETUP

    @property
    def started(self) -> bool:
        """The draw has been made: groups, knockout or over."""
        return self is not Phase.SETUP

    @property
    def in_play(self) -> bool:
        """Matches are being played right now."""
        return self in (Phase.GROUPS, Phase.KNOCKOUT)

    @property
    def knockout_reached(self) -> bool:
        return self in (Phase.KNOCKOUT, Phase.FINISHED)

    @classmethod
    def of(cls, value: str | None) -> Phase:
        return cls(value or cls.SETUP)


class Scoring(StrEnum):
    """How a match is scored: chosen for the tournament, changeable for a single match."""

    COPPIE = "coppie"  # the two couples enter the result
    RISULTATO = "risultato"  # the referee enters the final result
    TIRI = "tiri"  # the referee records every shot

    @property
    def by_couples(self) -> bool:
        return self is Scoring.COPPIE

    @property
    def needs_referee(self) -> bool:
        return self is not Scoring.COPPIE

    @property
    def by_shots(self) -> bool:
        return self is Scoring.TIRI

    @property
    def label(self) -> str:
        return _SCORING_LABELS[self]

    @classmethod
    def of(cls, value: str | None) -> Scoring:
        return cls(value or cls.COPPIE)


_SCORING_LABELS = {
    Scoring.COPPIE: "Le coppie inseriscono il risultato",
    Scoring.RISULTATO: "L'arbitro inserisce il risultato finale",
    Scoring.TIRI: "L'arbitro segna tiro per tiro",
}


class LivePhase(StrEnum):
    """Where a single match recorded shot by shot is."""

    PLAY = "play"
    SALVEZZA = "salvezza"  # the last cup fell: the other side shoots to save the match
    OVERTIME = "overtime"
    OVER = "over"
