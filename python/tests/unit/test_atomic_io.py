"""Regression tests for process-local atomic JSON persistence."""

from __future__ import annotations

import os
from pathlib import Path

from src.utils.atomic_io import atomic_write_json


def test_atomic_write_does_not_follow_the_final_path_component(tmp_path, monkeypatch) -> None:
    link_path = (tmp_path / "link.json").absolute()
    simulated_target = (tmp_path / "outside.json").absolute()
    original_resolve = Path.resolve
    replaced_destinations: list[Path] = []

    def simulated_resolve(path: Path, strict: bool = False) -> Path:
        absolute = Path(os.path.abspath(path))
        if absolute == link_path:
            return simulated_target
        return original_resolve(path, strict=strict)

    def capture_replace(source, destination) -> None:
        replaced_destinations.append(Path(destination))
        Path(source).unlink()

    monkeypatch.setattr(Path, "resolve", simulated_resolve)
    monkeypatch.setattr(os, "replace", capture_replace)

    atomic_write_json(link_path, {"safe": True})

    assert replaced_destinations == [link_path]
