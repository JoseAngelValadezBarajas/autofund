"""Versioned JSONL bundle. Raw and normalized content have separate files."""

import hashlib
import json
from collections.abc import AsyncGenerator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from autofund.replay.serialization import canonical_json, fingerprint
from autofund.replay.strategy import SimpleMeanReversionV0

from .errors import CaptureIntegrityError
from .models import LiveCandleUpdate, MarketInfo, Severity, SourceItem, SourceNotice
from .session import LiveSessionResult, normalized_fingerprint

SCHEMA = "autofund.capture.v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CaptureIntegrityError("expected capture object")
    return cast(dict[str, object], value)


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise CaptureIntegrityError("expected capture string")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(_str(value))


def _load_lines(path: Path) -> list[dict[str, object]]:
    try:
        return [
            _dict(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
    except (OSError, ValueError) as exc:
        raise CaptureIntegrityError("missing, truncated or malformed capture") from exc


class RawCaptureWriter:
    def __init__(
        self,
        path: Path,
        *,
        source: str,
        info: MarketInfo,
        interval: str,
        session_id: str,
        started_at: datetime,
        metadata_raw: str | None,
        strategy: SimpleMeanReversionV0,
        max_invalid_messages: int,
    ) -> None:
        self.path = path
        self.raw_path = path.with_suffix(".raw.jsonl")
        self.manifest_path = path.with_suffix(".manifest.json")
        if len({self.path, self.raw_path, self.manifest_path}) != 3 or any(
            p.exists() for p in (self.path, self.raw_path, self.manifest_path)
        ):
            raise CaptureIntegrityError(
                "capture bundle already exists or paths collide"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._normal = path.open("x", encoding="utf-8", newline="\n")
        self._raw = self.raw_path.open("x", encoding="utf-8", newline="\n")
        self._count = self._raw_count = 0
        self._closed = False
        header = {
            "type": "header",
            "schema_version": SCHEMA,
            "source": source,
            "info": info,
            "interval": interval,
            "session_id": session_id,
            "started_at": started_at,
            "strategy": strategy.identity,
            "max_invalid_messages": max_invalid_messages,
        }
        self._write(self._normal, header)
        self._write(
            self._raw,
            {"type": "header", "schema_version": SCHEMA, "metadata_raw": metadata_raw},
        )

    @staticmethod
    def _write(stream: object, value: object) -> None:
        from typing import TextIO

        target = cast(TextIO, stream)
        target.write(canonical_json(value) + "\n")
        target.flush()

    def append(self, item: SourceItem) -> None:
        if self._closed:
            raise CaptureIntegrityError("writer is closed")
        self._count += 1
        if item.raw_message is not None:
            self._raw_count += 1
            self._write(
                self._raw,
                {
                    "type": "raw",
                    "item_id": self._count,
                    "received_at": item.received_at,
                    "payload_text": item.raw_message,
                },
            )
        self._write(
            self._normal,
            {
                "type": "item",
                "item_id": self._count,
                "received_at": item.received_at,
                "event": item.event,
                "notice": item.notice,
            },
        )

    def finish(self, result: LiveSessionResult) -> None:
        if self._closed:
            return
        self._write(self._normal, {"type": "end", "item_count": self._count})
        self._write(self._raw, {"type": "end", "raw_count": self._raw_count})
        self._normal.close()
        self._raw.close()
        self._closed = True
        digests = {
            "normalized_file_sha256": _sha(self.path),
            "raw_file_sha256": _sha(self.raw_path),
        }
        manifest = {
            "schema_version": SCHEMA,
            **digests,
            "capture_file_fingerprint": fingerprint(digests),
            "report": result.report(),
        }
        self.manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")


class RecordedMarketDataSource:
    """Validates the whole bundle before emitting anything. No network dependency."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        raw_path = self.path.with_suffix(".raw.jsonl")
        try:
            manifest = _dict(
                json.loads(
                    self.path.with_suffix(".manifest.json").read_text(encoding="utf-8")
                )
            )
            if manifest.get("schema_version") != SCHEMA:
                raise CaptureIntegrityError("unsupported capture schema")
            digests = {
                "normalized_file_sha256": _sha(self.path),
                "raw_file_sha256": _sha(raw_path),
            }
            if any(
                manifest.get(key) != value for key, value in digests.items()
            ) or manifest.get("capture_file_fingerprint") != fingerprint(digests):
                raise CaptureIntegrityError("capture file fingerprint mismatch")
            rows, raws = _load_lines(self.path), _load_lines(raw_path)
            header = rows[0]
            if (
                header.get("type") != "header"
                or header.get("schema_version") != SCHEMA
                or raws[0].get("schema_version") != SCHEMA
            ):
                raise CaptureIntegrityError("invalid capture header")
            self.source_name = _str(header["source"])
            self.interval = _str(header["interval"])
            self.session_id = _str(header["session_id"])
            self.started_at = _time(header["started_at"])
            self.expected_report = _dict(manifest["report"])
            self.metadata_raw = cast(str | None, raws[0].get("metadata_raw"))
            info = _dict(header["info"])
            names = (
                "price_tick_size",
                "quantity_step_size",
                "minimum_quantity",
                "maximum_quantity",
                "minimum_notional",
                "maximum_notional",
            )
            self.info = MarketInfo(
                _str(info["symbol"]),
                _str(info["base_asset"]),
                _str(info["quote_asset"]),
                _str(info["status"]),
                *(
                    Decimal(_str(info[key])) if info.get(key) is not None else None
                    for key in names
                ),
            )
            identity = _dict(header["strategy"])
            params = dict(cast(list[tuple[str, object]], identity["parameters"]))
            self.strategy = SimpleMeanReversionV0(
                cast(int, params["window"]),
                Decimal(_str(params["entry_threshold"])),
                Decimal(_str(params["allocation_fraction"])),
            )
            if canonical_json(self.strategy.identity) != canonical_json(identity):
                raise CaptureIntegrityError("unsupported shadow strategy identity")
            self.max_invalid_messages = cast(int, header["max_invalid_messages"])
            if rows[-1] != {"type": "end", "item_count": len(rows) - 2} or raws[-1] != {
                "type": "end",
                "raw_count": len(raws) - 2,
            }:
                raise CaptureIntegrityError("capture footer/count mismatch")
            raw_by_id = {row["item_id"]: row for row in raws[1:-1]}
            if len(raw_by_id) != len(raws) - 2:
                raise CaptureIntegrityError("duplicate raw item identifier")
            items = []
            for index, row in enumerate(rows[1:-1], 1):
                if row.get("type") != "item" or row.get("item_id") != index:
                    raise CaptureIntegrityError("capture sequence mismatch")
                event = None
                if row["event"] is not None:
                    value = _dict(row["event"])
                    event = LiveCandleUpdate(
                        _str(value["market"]),
                        _str(value["interval"]),
                        _time(value["open_time"]),
                        _time(value["close_time"]),
                        Decimal(_str(value["open"])),
                        Decimal(_str(value["high"])),
                        Decimal(_str(value["low"])),
                        Decimal(_str(value["close"])),
                        Decimal(_str(value["volume"])),
                        cast(bool, value["is_closed"]),
                        _time(value["source_event_time"]),
                    )
                notice = None
                if row["notice"] is not None:
                    value = _dict(row["notice"])
                    notice = SourceNotice(
                        _str(value["kind"]),
                        Severity(_str(value["severity"])),
                        _str(value["reason"]),
                    )
                raw = raw_by_id.pop(index, None)
                items.append(
                    SourceItem(
                        _time(row["received_at"]),
                        event,
                        _str(raw["payload_text"]) if raw else None,
                        notice,
                    )
                )
            if raw_by_id:
                raise CaptureIntegrityError("orphan raw message")
            self._items = tuple(items)
            events = tuple(item.event for item in items if item.event is not None)
            if normalized_fingerprint(
                self.info.market, self.interval, events
            ) != self.expected_report.get("normalized_session_fingerprint"):
                raise CaptureIntegrityError("normalized session fingerprint mismatch")
            if len(events) != self.expected_report.get("normalized_updates") or len(
                raws
            ) - 2 != self.expected_report.get("raw_messages"):
                raise CaptureIntegrityError("manifest count mismatch")
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise CaptureIntegrityError("invalid capture bundle") from exc

    async def get_market_info(self) -> MarketInfo:
        return self.info

    async def stream_candles(self) -> AsyncGenerator[SourceItem, None]:
        for item in self._items:
            yield item
