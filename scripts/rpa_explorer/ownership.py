"""Immutable receiver fence for one task-owned editor session.

The caller retains evidence before advancing the observation and calls admit at
the last rejectable point before each UI dispatch. This class cannot attest to
browser focus, filesystem retention or actual process cleanup.
"""
from dataclasses import dataclass, replace
import re


class FenceRejected(ValueError):
    pass


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class SessionFence:
    session_id: str
    generation: int
    candidate_sha256: str
    observation_sha256: str
    attempt_set: tuple[str, ...]
    closed: bool = False

    def __post_init__(self):
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("Session identity required")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("Positive generation required")
        if not _digest(self.candidate_sha256) or not _digest(self.observation_sha256):
            raise ValueError("SHA256 identities required")
        if not isinstance(self.attempt_set, tuple) or not self.attempt_set or any(not isinstance(x, str) or not x for x in self.attempt_set):
            raise ValueError("Immutable nonempty attempt set required")
        if len(set(self.attempt_set)) != len(self.attempt_set):
            raise ValueError("Attempt identities must be unique")

    def binding(self):
        return {"session_id": self.session_id, "generation": self.generation,
                "candidate_sha256": self.candidate_sha256,
                "observation_sha256": self.observation_sha256,
                "attempt_set": list(self.attempt_set)}

    def admit(self, batch):
        if self.closed:
            raise FenceRejected("Session lease closed")
        binding = batch.get("binding")
        if not isinstance(binding, dict) or set(binding) != set(self.binding()):
            raise FenceRejected("Exact receiver binding required")
        if type(binding["generation"]) is not int or binding != self.binding():
            raise FenceRejected("Stale or foreign receiver binding")

    def observe(self, evidence_sha256):
        if self.closed or not _digest(evidence_sha256):
            raise FenceRejected("Cannot advance closed lease or invalid evidence")
        return replace(self, observation_sha256=evidence_sha256)

    def close(self):
        return self if self.closed else replace(self, generation=self.generation + 1, closed=True)
