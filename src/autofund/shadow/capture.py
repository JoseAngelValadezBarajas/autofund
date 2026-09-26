"""F4 capture is independent of the Stage execution journal."""

import hashlib
import os
from pathlib import Path

from autofund.observer import parsing
from autofund.observer.errors import MarketDataInvalid
from autofund.observer.models import (
    FeeSource,
    Level,
    MarketLimits,
    OrderBookSnapshot,
    PublicTrade,
    ShadowFee,
)
from autofund.observer.source import MarketFrame, MarketNotice
from autofund.replay.serialization import canonical_json, fingerprint

from .config import ShadowConfig
from .session import ShadowSession


def restore_frame(value: object) -> MarketFrame | MarketNotice:
    row = parsing.obj(value)
    if "kind" in row:
        return MarketNotice(
            parsing.timestamp(row.get("observed_at")),
            parsing.text(row.get("kind")),
            parsing.text(row.get("severity")),
        )
    book = parsing.obj(row.get("depth"))
    snapshot = OrderBookSnapshot(
        parsing.text(book.get("book")),
        parsing.timestamp(book.get("timestamp")),
        parsing.integer(book.get("sequence")),
        tuple(
            Level(
                parsing.number(parsing.obj(v).get("price")),
                parsing.number(parsing.obj(v).get("amount")),
            )
            for v in parsing.array(book.get("bids"))
        ),
        tuple(
            Level(
                parsing.number(parsing.obj(v).get("price")),
                parsing.number(parsing.obj(v).get("amount")),
            )
            for v in parsing.array(book.get("asks"))
        ),
    )
    trades = tuple(
        PublicTrade(
            parsing.text(t.get("book")),
            parsing.integer(t.get("trade_id")),
            parsing.timestamp(t.get("timestamp")),
            parsing.number(t.get("price")),
            parsing.number(t.get("amount")),
            parsing.text(t.get("maker_side")),
        )
        for t in (parsing.obj(v) for v in parsing.array(row.get("trades")))
    )
    return MarketFrame(
        parsing.timestamp(row.get("observed_at")),
        trades,
        snapshot,
        parsing.integer(row.get("retry_count")),
    )


def restore_session(header: dict[str, object]) -> ShadowSession:
    limits_row, fee_row, config_row = (
        parsing.obj(header.get(k)) for k in ("limits", "fee", "config")
    )
    limits = MarketLimits(
        parsing.text(limits_row.get("book")),
        *(
            parsing.number(limits_row.get(k))
            for k in (
                "minimum_amount",
                "maximum_amount",
                "minimum_price",
                "maximum_price",
                "minimum_value",
                "maximum_value",
                "tick_size",
            )
        ),
    )
    config = ShadowConfig(
        initial_equity=parsing.number(config_row.get("initial_equity")),
        max_deployment=parsing.number(config_row.get("max_deployment")),
        single_order_cap=parsing.number(config_row.get("single_order_cap")),
        extra_slippage_bps=parsing.number(config_row.get("extra_slippage_bps")),
        max_spread_bps=parsing.number(config_row.get("max_spread_bps")),
        max_orderbook_age_seconds=parsing.integer(
            config_row.get("max_orderbook_age_seconds")
        ),
        closing_delay_seconds=parsing.integer(config_row.get("closing_delay_seconds")),
        poll_seconds=parsing.integer(config_row.get("poll_seconds")),
        interval=parsing.text(config_row.get("interval")),
    )
    fee = ShadowFee(
        parsing.number(fee_row.get("rate")),
        FeeSource(parsing.text(fee_row.get("source"))),
    )
    return ShadowSession(config, limits, fee, parsing.timestamp(header.get("start")))


class ShadowCapture:
    def __init__(
        self, directory: Path, session: ShadowSession, *, resume: bool = False
    ) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self._lock = (directory / ".writer.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._lock.seek(0)
                if not self._lock.read(1):
                    self._lock.write(b"0")
                    self._lock.flush()
                self._lock.seek(0)
                # See the note in exchanges/bitso/journal.py: both `attr-defined` (for the platform
                # without this module) and `unused-ignore` (for the platform with it) are required,
                # because mypy analyzes both branches of the platform test.
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined, unused-ignore]
            else:
                import fcntl

                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined, unused-ignore]
        except OSError:
            self._lock.close()
            raise MarketDataInvalid("shadow capture already in use") from None
        self.session = session
        self.market_hash = "0" * 64
        self.count = 0
        header = {
            "schema_version": "autofund.shadow.capture.v1",
            "environment": "PRODUCTION_READ_ONLY",
            "config": session.config,
            "limits": session.limits,
            "fee": session.fee,
            "start": session.start,
            "strategy_fingerprint": session.strategy.identity.fingerprint,
        }
        try:
            if not resume:
                for name in (
                    "session.jsonl",
                    "market.jsonl",
                    "shadow_journal.jsonl",
                    "manifest.json",
                    "report.json",
                ):
                    if (directory / name).exists():
                        raise MarketDataInvalid("capture exists; use explicit resume")
                self._append("session.jsonl", header)
                (directory / "market.jsonl").touch()
                (directory / "shadow_journal.jsonl").touch()
            else:
                old_header, frames, results = read_capture(
                    directory, verify_manifest=False
                )
                if canonical_json(old_header) != canonical_json(header):
                    raise MarketDataInvalid("resume configuration differs")
                replayed = restore_session(old_header)
                for index, frame in enumerate(frames):
                    replayed.process(frame)
                    if index < len(results):
                        if canonical_json(results[index]) != canonical_json(
                            self._journal_record(replayed)
                        ):
                            raise MarketDataInvalid("SHADOW_RECONSTRUCTION_HALT")
                    else:
                        self._append(
                            "shadow_journal.jsonl", self._journal_record(replayed)
                        )
                self.session = replayed
                self.count = len(frames)
                for frame in frames:
                    self.market_hash = fingerprint(
                        {"previous": self.market_hash, "frame": frame}
                    )
        except Exception:
            self.close()
            raise

    def _append(self, name: str, value: object) -> None:
        with (self.directory / name).open("ab") as stream:
            stream.write((canonical_json(value) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _journal_record(session: ShadowSession) -> dict[str, object]:
        result = session.result()
        return {
            "frame": session.frames,
            "candles": session.candles,
            "signals": session.signals,
            "executions": session.executions,
            "rejections": session.rejections,
            "ledger": session.wallet.ledger,
            "equity": session.equity_curve[-1:] or [],
            "result_fingerprint": result["result_fingerprint"],
        }

    def consume(self, frame: MarketFrame | MarketNotice) -> None:
        digest = fingerprint({"previous": self.market_hash, "frame": frame})
        self._append(
            "market.jsonl", {"sequence": self.count + 1, "frame": frame, "hash": digest}
        )
        self.count += 1
        self.market_hash = digest
        self.session.process(frame)
        self._append("shadow_journal.jsonl", self._journal_record(self.session))

    def finalize(self, reason: str) -> dict[str, object]:
        # Ctrl+C may land after durable input but before the derived record.
        # Rebuild that uncommitted tail rather than finalize inconsistent files.
        header, frames, records = read_capture(self.directory, verify_manifest=False)
        if len(records) < len(frames):
            recovered = restore_session(header)
            for index, frame in enumerate(frames):
                recovered.process(frame)
                expected = self._journal_record(recovered)
                if index < len(records):
                    if canonical_json(records[index]) != canonical_json(expected):
                        raise MarketDataInvalid("SHADOW_RECONSTRUCTION_HALT")
                else:
                    self._append("shadow_journal.jsonl", expected)
            self.session = recovered
        result = self.session.result()
        self._atomic("report.json", result)
        hashes = {
            name: hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
            for name in (
                "session.jsonl",
                "market.jsonl",
                "shadow_journal.jsonl",
                "report.json",
            )
        }
        manifest = {
            "schema_version": "autofund.shadow.manifest.v1",
            "stop_reason": reason,
            "frames": self.count,
            "result_fingerprint": result["result_fingerprint"],
            "files": hashes,
        }
        self._atomic("manifest.json", manifest)
        return result

    def _atomic(self, name: str, value: object) -> None:
        temporary = self.directory / (name + ".tmp")
        with temporary.open("wb") as output:
            output.write((canonical_json(value) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.directory / name)

    def close(self) -> None:
        self._lock.close()


def _lines(path: Path) -> list[dict[str, object]]:
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        raise MarketDataInvalid("truncated shadow journal; manual recovery required")
    return [parsing.decode(line) for line in data.splitlines()]


def read_capture(
    directory: Path, *, verify_manifest: bool = True
) -> tuple[
    dict[str, object], list[MarketFrame | MarketNotice], list[dict[str, object]]
]:
    if verify_manifest:
        manifest = parsing.decode((directory / "manifest.json").read_bytes())
        files = parsing.obj(manifest.get("files"))
        if set(files) != {
            "session.jsonl",
            "market.jsonl",
            "shadow_journal.jsonl",
            "report.json",
        }:
            raise MarketDataInvalid("unexpected capture file set")
        for name, digest in files.items():
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
                raise MarketDataInvalid("capture file integrity mismatch")
    headers = _lines(directory / "session.jsonl")
    if (
        len(headers) != 1
        or headers[0].get("schema_version") != "autofund.shadow.capture.v1"
        or headers[0].get("environment") != "PRODUCTION_READ_ONLY"
    ):
        raise MarketDataInvalid("invalid capture header")
    frames = []
    previous = "0" * 64
    for index, record in enumerate(_lines(directory / "market.jsonl"), 1):
        frame = restore_frame(record.get("frame"))
        digest = fingerprint({"previous": previous, "frame": frame})
        if record.get("sequence") != index or record.get("hash") != digest:
            raise MarketDataInvalid("market capture chain mismatch")
        previous = digest
        frames.append(frame)
    results = _lines(directory / "shadow_journal.jsonl")
    if len(results) > len(frames) or (verify_manifest and len(results) != len(frames)):
        raise MarketDataInvalid("input/journal counts differ")
    if verify_manifest:
        report = parsing.decode((directory / "report.json").read_bytes())
        if manifest.get("frames") != len(frames) or manifest.get(
            "result_fingerprint"
        ) != report.get("result_fingerprint"):
            raise MarketDataInvalid("manifest counts or result identity differ")
    return headers[0], frames, results


def replay(directory: Path) -> dict[str, object]:
    header, frames, journal = read_capture(directory)
    session = restore_session(header)
    for frame, recorded in zip(frames, journal, strict=True):
        session.process(frame)
        if canonical_json(ShadowCapture._journal_record(session)) != canonical_json(
            recorded
        ):
            raise MarketDataInvalid("shadow journal parity failure")
    result = session.result()
    stored = parsing.decode((directory / "report.json").read_bytes())
    if canonical_json(result) != canonical_json(stored):
        raise MarketDataInvalid("shadow result parity failure")
    return result
