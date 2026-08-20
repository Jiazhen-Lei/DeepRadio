"""Versioned snapshots for recoverable flowgraph changes."""

from __future__ import annotations

import os
import shutil
from typing import Optional

from .shared_state import SharedState


def create_snapshot(
    state: SharedState, snapshots_dir: str, state_path: str
) -> Optional[str]:
    if not state.project.grc_path and not os.path.exists(state_path):
        return None
    version = state.project.flowgraph_version
    target = os.path.join(snapshots_dir, f"v{version}")
    os.makedirs(target, exist_ok=True)
    if state.project.grc_path and os.path.isfile(state.project.grc_path):
        shutil.copy2(
            state.project.grc_path,
            os.path.join(target, os.path.basename(state.project.grc_path)),
        )
    state.save(os.path.join(target, "state.json"))
    if target not in state.coordination.snapshots:
        state.coordination.snapshots.append(target)
    return target


def restore_snapshot(snapshot_dir: str, state_path: str) -> SharedState:
    source_state = os.path.join(snapshot_dir, "state.json")
    if not os.path.isfile(source_state):
        raise FileNotFoundError(source_state)
    restored = SharedState.load(source_state)
    grc_files = [
        name for name in os.listdir(snapshot_dir) if name.endswith(".grc")
    ]
    if grc_files:
        source_grc = os.path.join(snapshot_dir, grc_files[0])
        destination = restored.project.grc_path
        if destination:
            os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
            shutil.copy2(source_grc, destination)
    restored.save(state_path)
    return restored
