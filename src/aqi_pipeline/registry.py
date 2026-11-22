"""File-based model registry with champion and shadow stages.

Layout::

    registry/
      index.json            stage pointers, version summaries, promotion history
      v001/model.joblib     the fitted ForecastModel
      v001/meta.json        how it was trained and validated, monitoring baselines
      v001/reference.joblib training-period inputs, the reference for drift checks

Stages: ``champion`` serves forecasts; ``shadow`` is the previous champion, kept
predicting during the new champion's probation so a rollback is one pointer
swap; ``archived`` and ``rolled_back`` are retired versions kept for audit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from .modeling import ForecastModel
from .store import _json_default


class ModelRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._cache: dict[str, ForecastModel] = {}
        if not self._index_path.exists():
            self._write({"champion": None, "shadow": None, "next_id": 1, "versions": {}, "history": []})

    # ---- index -------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        with open(self._index_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, index: dict[str, Any]) -> None:
        tmp = self._index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=2, default=_json_default)
        os.replace(tmp, self._index_path)       # atomic: readers never see a half-written index

    @property
    def champion(self) -> str | None:
        return self._read()["champion"]

    @property
    def shadow(self) -> str | None:
        return self._read()["shadow"]

    def history(self) -> list[dict[str, Any]]:
        return self._read()["history"]

    def versions(self) -> list[dict[str, Any]]:
        index = self._read()
        return [{"version": v, **info} for v, info in sorted(index["versions"].items())]

    # ---- artefacts ---------------------------------------------------------
    def register(self, model: ForecastModel, meta: dict[str, Any], reference: pd.DataFrame, ts) -> str:
        index = self._read()
        version = f"v{index['next_id']:03d}"
        folder = self.root / version
        folder.mkdir(parents=True, exist_ok=False)
        joblib.dump(model, folder / "model.joblib", compress=3)
        joblib.dump(reference, folder / "reference.joblib", compress=3)
        meta = {"version": version, "registered_at": str(pd.Timestamp(ts)), **meta}
        with open(folder / "meta.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=_json_default)
        index["next_id"] += 1
        index["versions"][version] = {
            "stage": "candidate", "registered_at": meta["registered_at"],
            "kind": meta.get("kind"), "strategy": meta.get("strategy"), "trigger": meta.get("trigger"),
        }
        index["history"].append({"ts": meta["registered_at"], "action": "register", "version": version})
        self._write(index)
        self._cache[version] = model
        return version

    def load(self, version: str) -> ForecastModel:
        if version not in self._cache:
            self._cache[version] = joblib.load(self.root / version / "model.joblib")
        return self._cache[version]

    def meta(self, version: str) -> dict[str, Any]:
        with open(self.root / version / "meta.json", "r", encoding="utf-8") as fh:
            return json.load(fh)

    def update_meta(self, version: str, changes: dict[str, Any]) -> None:
        meta = {**self.meta(version), **changes}
        with open(self.root / version / "meta.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=_json_default)

    def reference(self, version: str) -> pd.DataFrame:
        return joblib.load(self.root / version / "reference.joblib")

    # ---- stage changes -----------------------------------------------------
    def promote(self, version: str, ts, reason: str) -> None:
        """Make ``version`` the champion; the previous champion becomes the shadow."""
        index = self._read()
        previous = index["champion"]
        if index["shadow"]:
            index["versions"][index["shadow"]]["stage"] = "archived"
        if previous:
            index["versions"][previous]["stage"] = "shadow"
        index["shadow"] = previous
        index["champion"] = version
        index["versions"][version]["stage"] = "champion"
        index["versions"][version]["promoted_at"] = str(pd.Timestamp(ts))
        index["history"].append({"ts": str(pd.Timestamp(ts)), "action": "promote", "version": version,
                                 "replaced": previous, "reason": reason})
        self._write(index)

    def retire_shadow(self, ts, reason: str) -> str | None:
        index = self._read()
        shadow = index["shadow"]
        if shadow:
            index["versions"][shadow]["stage"] = "archived"
            index["shadow"] = None
            index["history"].append({"ts": str(pd.Timestamp(ts)), "action": "retire_shadow",
                                     "version": shadow, "reason": reason})
            self._write(index)
        return shadow

    def rollback(self, ts, reason: str) -> str:
        """Restore the shadow as champion; the failed champion is marked rolled_back."""
        index = self._read()
        if not index["shadow"]:
            raise RuntimeError("no shadow model to roll back to")
        failed, restored = index["champion"], index["shadow"]
        index["versions"][failed]["stage"] = "rolled_back"
        index["versions"][restored]["stage"] = "champion"
        index["champion"], index["shadow"] = restored, None
        index["history"].append({"ts": str(pd.Timestamp(ts)), "action": "rollback", "version": restored,
                                 "replaced": failed, "reason": reason})
        self._write(index)
        return restored
