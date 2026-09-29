"""Demo run records for P4 (plan 5.10, decision D-035).

One run of the fixed demo script produces one :class:`DemoRun`: the ordered steps,
each step's status/reason/duration, aggregate totals, the mode that was actually
used and, when available, the answering outcome.  Two runs over the same corpus
and the same confirmed query must be comparable field by field, so ``run_id`` is
derived from the run's *content* rather than from the clock.

A run record is written to ``methods/literature_poc/runs/`` — run history, not the
formal method protocol (plan section 6.2).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.literature.models import canonical_hash

RUN_RECORD_DIR = Path("methods") / "literature_poc" / "runs"
_FORBIDDEN_KEY_PARTS = ("key", "token", "secret", "password", "authorization", "credential")


class StepStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class DemoStep(BaseModel):
    """One recorded step of the demo script."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    status: StepStatus
    reason: str | None = Field(default=None, max_length=200)
    duration_ms: int = Field(default=0, ge=0)
    counts: dict[str, int] = Field(default_factory=dict)
    detail: dict[str, str] = Field(default_factory=dict)


class DemoRun(BaseModel):
    """One complete demo run, comparable across repeats."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    mode: str = Field(min_length=1, max_length=32)
    provider: str = Field(min_length=1, max_length=64)
    confirmed_query: str = Field(min_length=1, max_length=2000)
    question: str = Field(default="", max_length=2000)
    steps: tuple[DemoStep, ...]
    totals: dict[str, int]
    answer_status: str | None = Field(default=None, max_length=32)
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    git_commit: str | None = Field(default=None, max_length=64)

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)


def _assert_no_secret_keys(value: Mapping[str, Any], *, field: str) -> None:
    for key, item in value.items():
        lowered = str(key).casefold()
        if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
            raise ValueError(f"{field} must not record secret-like keys: {key}")
        if isinstance(item, Mapping):
            _assert_no_secret_keys(item, field=field)


class DemoRunRecorder:
    """Collect demo steps and finish with a comparable run record."""

    def __init__(
        self,
        *,
        mode: str,
        provider: str,
        confirmed_query: str,
        question: str = "",
        started_at: datetime | None = None,
        git_commit: str | None = None,
    ) -> None:
        self._mode = mode.strip()
        self._provider = provider.strip()
        self._confirmed_query = confirmed_query.strip()
        self._question = question.strip()
        self._started_at = (started_at or datetime.now(UTC)).astimezone(UTC)
        self._git_commit = git_commit
        self._steps: list[DemoStep] = []

    @property
    def steps(self) -> tuple[DemoStep, ...]:
        return tuple(self._steps)

    def record(
        self,
        *,
        name: str,
        status: StepStatus,
        reason: str | None = None,
        duration_ms: int = 0,
        counts: Mapping[str, int] | None = None,
        detail: Mapping[str, str] | None = None,
    ) -> DemoStep:
        if status is not StepStatus.OK and not reason:
            raise ValueError("a step that is not ok must carry a reason")
        normalized_counts = {str(key): int(value) for key, value in (counts or {}).items()}
        normalized_detail = {str(key): str(value) for key, value in (detail or {}).items()}
        _assert_no_secret_keys(normalized_counts, field="counts")
        _assert_no_secret_keys(normalized_detail, field="detail")
        step = DemoStep(
            name=name,
            status=status,
            reason=reason,
            duration_ms=duration_ms,
            counts=normalized_counts,
            detail=normalized_detail,
        )
        self._steps.append(step)
        return step

    def finish(
        self,
        *,
        answer_status: str | None = None,
        finished_at: datetime | None = None,
    ) -> DemoRun:
        finished = (finished_at or datetime.now(UTC)).astimezone(UTC)
        totals = {
            "step": len(self._steps),
            "ok": sum(1 for step in self._steps if step.status is StepStatus.OK),
            "failed": sum(1 for step in self._steps if step.status is StepStatus.FAILED),
            "skipped": sum(1 for step in self._steps if step.status is StepStatus.SKIPPED),
        }
        run_id = (
            "run_"
            + canonical_hash(
                {
                    "mode": self._mode,
                    "provider": self._provider,
                    "confirmed_query": self._confirmed_query,
                    "question": self._question,
                    "answer_status": answer_status,
                    "steps": [
                        {
                            "name": step.name,
                            "status": step.status.value,
                            "reason": step.reason,
                            "counts": step.counts,
                        }
                        for step in self._steps
                    ],
                }
            )[:24]
        )
        return DemoRun(
            run_id=run_id,
            mode=self._mode,
            provider=self._provider,
            confirmed_query=self._confirmed_query,
            question=self._question,
            steps=tuple(self._steps),
            totals=totals,
            answer_status=answer_status,
            started_at=self._started_at,
            finished_at=finished,
            duration_ms=max(0, int((finished - self._started_at).total_seconds() * 1000)),
            git_commit=self._git_commit,
        )


def run_record_filename(run: DemoRun) -> str:
    """One file per run so repeated runs stay comparable side by side."""

    stamp = run.started_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{run.run_id}.json"


def write_run_record(run: DemoRun, directory: str | Path | None = None) -> Path:
    """Persist one run record under the run history directory."""

    target_dir = Path(directory) if directory is not None else RUN_RECORD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / run_record_filename(run)
    target.write_text(
        json.dumps(json.loads(run.model_dump_json()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


__all__ = [
    "RUN_RECORD_DIR",
    "DemoRun",
    "DemoRunRecorder",
    "DemoStep",
    "StepStatus",
    "run_record_filename",
    "write_run_record",
]
