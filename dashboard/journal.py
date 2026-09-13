"""Trade journal.

Every completed round trip an engine records is mirrored into the dashboard's
journal automatically, with the facts filled in (market, strategy, prices, PnL,
duration). A human then adds what a machine cannot: why the trade was taken, why
it was closed, a rating, notes, tags and optional screenshots.

The auto-import is idempotent — ``(mode, trade_id)`` is unique — so a page
refresh or a dashboard restart never duplicates an entry, and it never overwrites
the notes a user has already written.
"""
from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from typing import Any

from dashboard.db import DashboardDB, EngineReader
from polybot.utils.time_utils import now_s

log = logging.getLogger("polybot.dashboard.journal")

#: Fields a user may edit. Everything else is engine-owned and read-only.
EDITABLE_FIELDS = ("entry_reason", "exit_reason", "confidence", "notes", "rating", "tags")

_MAX_SHOT_BYTES = 2 * 1024 * 1024   # 2 MB per screenshot
_ALLOWED_IMAGE_PREFIXES = ("data:image/png;base64,", "data:image/jpeg;base64,",
                           "data:image/webp;base64,")


class TradeJournal:
    """Auto-imports completed trades and stores the human annotations."""

    def __init__(self, db: DashboardDB, uploads_dir: Path) -> None:
        self._db = db
        self._uploads = uploads_dir
        self._uploads.mkdir(parents=True, exist_ok=True)

    # ======================================================================
    # Import
    # ======================================================================
    def sync(self, mode: str, reader: EngineReader, *, limit: int = 500) -> int:
        """Import any completed trades that are not journalled yet."""
        row = self._db.query_one(
            "SELECT MAX(trade_id) AS last FROM journal WHERE mode = ?", (mode,))
        last_id = int(row["last"] or 0) if row else 0
        trades = reader.query(
            "SELECT id, market_id, market_label, side, strategy, qty, entry_price, exit_price, "
            "entry_ts, exit_ts, duration_s, pnl, entry_reason, exit_reason, confidence "
            "FROM trades WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit))
        if not trades:
            return 0
        self._db.executemany(
            "INSERT OR IGNORE INTO journal (mode, trade_id, ts, market_id, market_label, side, "
            "strategy, qty, entry_price, exit_price, entry_ts, exit_ts, duration_s, pnl, "
            "entry_reason, exit_reason, confidence, auto) "
            "VALUES (:mode, :trade_id, :ts, :market_id, :market_label, :side, :strategy, :qty, "
            ":entry_price, :exit_price, :entry_ts, :exit_ts, :duration_s, :pnl, :entry_reason, "
            ":exit_reason, :confidence, 1)",
            [{
                "mode": mode, "trade_id": trade["id"], "ts": now_s(),
                "market_id": trade["market_id"], "market_label": trade["market_label"],
                "side": trade["side"], "strategy": trade["strategy"], "qty": trade["qty"],
                "entry_price": trade["entry_price"], "exit_price": trade["exit_price"],
                "entry_ts": trade["entry_ts"], "exit_ts": trade["exit_ts"],
                "duration_s": trade["duration_s"], "pnl": trade["pnl"],
                "entry_reason": trade["entry_reason"], "exit_reason": trade["exit_reason"],
                "confidence": trade["confidence"],
            } for trade in trades])
        log.debug("journal imported trades", extra={"mode": mode, "count": len(trades)})
        return len(trades)

    # ======================================================================
    # Read
    # ======================================================================
    def entries(self, mode: str, *, limit: int = 200, offset: int = 0,
                only_annotated: bool = False, search: str = "") -> list[dict[str, Any]]:
        clauses = ["mode = ?"]
        params: list[Any] = [mode]
        if only_annotated:
            clauses.append("(notes IS NOT NULL AND notes != '')")
        if search:
            clauses.append("(COALESCE(notes,'') LIKE ? OR COALESCE(market_label,'') LIKE ? "
                           "OR COALESCE(tags,'') LIKE ? OR COALESCE(strategy,'') LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        params.extend([max(1, min(1000, limit)), max(0, offset)])
        return self._db.query(
            f"SELECT * FROM journal WHERE {' AND '.join(clauses)} "
            f"ORDER BY exit_ts DESC LIMIT ? OFFSET ?", params)

    def entry(self, entry_id: int) -> dict[str, Any] | None:
        return self._db.query_one("SELECT * FROM journal WHERE id = ?", (entry_id,))

    def stats(self, mode: str) -> dict[str, Any]:
        row = self._db.query_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN notes IS NOT NULL AND notes != '' THEN 1 ELSE 0 END) AS annotated, "
            "AVG(rating) AS avg_rating, "
            "SUM(CASE WHEN entry_shot IS NOT NULL OR exit_shot IS NOT NULL THEN 1 ELSE 0 END) "
            "AS with_screenshots FROM journal WHERE mode = ?", (mode,))
        return {
            "total": int(row["total"] or 0) if row else 0,
            "annotated": int(row["annotated"] or 0) if row else 0,
            "with_screenshots": int(row["with_screenshots"] or 0) if row else 0,
            "average_rating": round(float(row["avg_rating"]), 2)
            if row and row["avg_rating"] is not None else None,
        }

    # ======================================================================
    # Write
    # ======================================================================
    def annotate(self, entry_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply user annotations to one journal entry."""
        fields = {k: v for k, v in updates.items() if k in EDITABLE_FIELDS}
        if "rating" in fields and fields["rating"] is not None:
            rating = int(fields["rating"])
            if not 1 <= rating <= 5:
                raise ValueError("rating must be between 1 and 5")
            fields["rating"] = rating
        if "confidence" in fields and fields["confidence"] is not None:
            confidence = float(fields["confidence"])
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("confidence must be between 0 and 1")
            fields["confidence"] = confidence
        for key in ("notes", "entry_reason", "exit_reason", "tags"):
            if key in fields and fields[key] is not None:
                fields[key] = str(fields[key])[:4000]
        if not fields:
            raise ValueError("no editable fields supplied")

        assignments = ", ".join(f"{key} = :{key}" for key in fields)
        self._db.execute(f"UPDATE journal SET {assignments}, auto = 0 WHERE id = :id",
                         {**fields, "id": entry_id})
        entry = self.entry(entry_id)
        if entry is None:
            raise ValueError(f"journal entry {entry_id} not found")
        return entry

    def attach_screenshot(self, entry_id: int, which: str, data_url: str) -> dict[str, Any]:
        """Store a base64 data-URL screenshot on disk and link it to the entry."""
        if which not in ("entry_shot", "exit_shot"):
            raise ValueError("screenshot slot must be entry_shot or exit_shot")
        if not data_url.startswith(_ALLOWED_IMAGE_PREFIXES):
            raise ValueError("screenshot must be a PNG, JPEG or WebP data URL")
        header, _, payload = data_url.partition(",")
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("screenshot is not valid base64") from exc
        if len(raw) > _MAX_SHOT_BYTES:
            raise ValueError(f"screenshot exceeds {_MAX_SHOT_BYTES // (1024 * 1024)} MB")

        extension = {"png": "png", "jpeg": "jpg", "webp": "webp"}[header.split("/")[1].split(";")[0]]
        filename = f"journal-{entry_id}-{which}.{extension}"
        path = self._uploads / filename
        path.write_bytes(raw)
        self._db.execute(f"UPDATE journal SET {which} = ? WHERE id = ?", (filename, entry_id))
        entry = self.entry(entry_id)
        if entry is None:
            raise ValueError(f"journal entry {entry_id} not found")
        return entry

    def screenshot_path(self, filename: str) -> Path | None:
        """Resolve a stored screenshot, refusing anything outside the uploads dir."""
        candidate = (self._uploads / Path(filename).name).resolve()
        if candidate.parent != self._uploads.resolve() or not candidate.is_file():
            return None
        return candidate

    def delete_screenshot(self, entry_id: int, which: str) -> None:
        if which not in ("entry_shot", "exit_shot"):
            raise ValueError("screenshot slot must be entry_shot or exit_shot")
        entry = self.entry(entry_id)
        if entry and entry.get(which):
            path = self.screenshot_path(str(entry[which]))
            if path is not None:
                path.unlink(missing_ok=True)
        self._db.execute(f"UPDATE journal SET {which} = NULL WHERE id = ?", (entry_id,))
