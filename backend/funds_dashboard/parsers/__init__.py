"""Wind-response parsers.

Each parser consumes a `WindResult` (the `columns + rows` envelope
the Wind CLI returns inside its triple-nested JSON) and produces
SQLAlchemy model instances ready to persist. The contract is:

* `"INVALID"` / `"MISSING"` / `"NOT_APPLICABLE"` / `None` / `""`
  marker values from Wind **must** propagate as enum status fields
  on the derived row — never coerce to 0 (Linda + Vera + Nova hard
  rule, see `qa/consistency_checks.md` §1).
* Numeric fields that the marker logic rejects land as SQL NULL,
  not zero. The `*_status` enum is the only place a downstream
  consumer learns "this value isn't usable".
* Parsers are pure — no I/O, no DB session, no Wind call. The
  scheduler wires Wind → parser → audit-helper → derived table.

This split (parse-then-persist) is what makes `INVALID-doesnt-leak-to-0`
testable without a Wind subprocess or a live DB.
"""

__all__ = ["etf_snapshot"]
