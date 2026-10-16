"""Raw → Event/ignored: ledger и существующий pending фиксируются одним commit."""

import sqlite3
from hashlib import sha256
from uuid import UUID

from agent.telemetry.adapters.cowrie import CowrieAdapter, CowrieBinding, CowrieMappingError

from .models import CowrieCollectionLimits, CowrieJournalError, CowrieProgress, binding_scope
from .repository import CowrieJournalRepository


class CowrieRawConverter:
    """Повтор после ACK проверяет ledger, не воссоздавая удалённый pending."""

    version = "cowrie-jsonl/1"

    def __init__(self, repository: CowrieJournalRepository) -> None:
        self.repository = repository
        self.mapper = CowrieAdapter()

    def _checkpoint(self, stage: str) -> None:
        """Crash injection до/после фиксации общей транзакции."""

    def convert(self, binding: CowrieBinding, journal_id: UUID, offset: int) -> str:
        db = self.repository.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            blocked = self.repository.state(journal_id)[4]
            if blocked:
                raise CowrieJournalError(blocked)
            actual = db.execute(
                "SELECT g.scope,s.revision,s.port FROM cowrie_generations g JOIN cowrie_scopes s "
                "ON s.scope=g.scope WHERE g.journal_id=?",
                (str(journal_id),),
            ).fetchone()
            if actual != (binding_scope(binding), binding.profile_revision, binding.service_port):
                raise CowrieJournalError("BINDING_CONFLICT")
            row = db.execute(
                "SELECT raw FROM cowrie_raw WHERE journal_id=? AND offset=?",
                (str(journal_id), offset),
            ).fetchone()
            if row is None:
                raise CowrieJournalError("RAW_NOT_SAVED")
            raw = row[0]
            event = self.mapper.map_record(
                raw, binding=binding, journal_id=journal_id, offset=offset
            )
            payload = None if event is None else self.repository.spool.validated_payload(event)
            event_id = None if event is None else str(event.event_id)
            evidence = (
                sha256(raw).digest(),
                self.version,
                "ignored" if event is None else "event",
                event_id,
                payload,
            )
            prior = db.execute(
                "SELECT raw_digest,mapper_version,result,event_id,payload "
                "FROM cowrie_ledger WHERE journal_id=? AND offset=?",
                (str(journal_id), offset),
            ).fetchone()
            if prior is not None:
                if prior != evidence:
                    raise CowrieJournalError("RESULT_CONFLICT")
                return "repeated"
            if event is not None:
                used = db.execute(
                    "SELECT event_id FROM pending WHERE event_id=? UNION ALL "
                    "SELECT event_id FROM quarantine WHERE event_id=? UNION ALL "
                    "SELECT event_id FROM low_conversion_ledger WHERE event_id=? UNION ALL "
                    "SELECT event_id FROM cowrie_ledger WHERE event_id=? UNION ALL "
                    "SELECT json_extract(j.value,'$.event_id') FROM batch_snapshots b, "
                    "json_each(b.payload,'$.events') j WHERE json_extract(j.value,'$.event_id')=? "
                    "LIMIT 1",
                    (event_id,) * 5,
                ).fetchone()
                if used is not None:
                    raise CowrieJournalError("EVENT_ID_CONFLICT")
                assert payload is not None
                self.repository.spool.insert_pending(event.event_id, payload)
                self._checkpoint("pending_inserted")
            self.repository._usage(0 if payload is None else len(payload.encode("utf-8")))
            db.execute(
                "INSERT INTO cowrie_ledger VALUES (?,?,?,?,?,?,?)",
                (str(journal_id), offset, *evidence),
            )
            self._checkpoint("ledger_inserted")
            self._checkpoint("before_commit")
        self._checkpoint("after_commit")
        return "created"

    def step(
        self, binding: CowrieBinding, journal_id: UUID, limits: CowrieCollectionLimits | None = None
    ) -> CowrieProgress:
        limits = limits or CowrieCollectionLimits()
        count = size = 0
        try:
            blocked = self.repository.state(journal_id)[4]
            if blocked:
                return CowrieProgress("error", code=blocked)
            while True:
                row = self.repository.db.execute(
                    "SELECT r.offset,length(r.raw) FROM cowrie_raw r LEFT JOIN cowrie_ledger l "
                    "ON r.journal_id=l.journal_id AND r.offset=l.offset "
                    "WHERE r.journal_id=? AND l.offset IS NULL ORDER BY r.offset LIMIT 1",
                    (str(journal_id),),
                ).fetchone()
                if row is None:
                    return CowrieProgress("progress" if count else "idle", count, size)
                if count >= limits.max_records or size + row[1] > limits.max_bytes:
                    return CowrieProgress("limited", count, size, "STEP_LIMIT")
                self.convert(binding, journal_id, row[0])
                count += 1
                size += row[1]
        except (CowrieJournalError, CowrieMappingError) as error:
            if error.code != "STORAGE_LIMIT":
                self.repository.block(journal_id, error.code)
            return CowrieProgress(
                "limited" if error.code == "STORAGE_LIMIT" else "error", count, size, error.code
            )
        except sqlite3.Error:
            return CowrieProgress("retry", count, size, "DB_ERROR")
