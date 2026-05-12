"""rename nav→market_price + add unit_nav to etf_daily_snapshot

Revision ID: f9a4c1e8b203
Revises: 297272847a56
Create Date: 2026-05-12 00:48:00

Linda hardline #1 (msg=ed1d62dc + feng-lu msg=440b79ea Wind alt-tool
finding): the original `nav` column was populated from
`fund_data:get_fund_quote` MATCH — that's the secondary-market
intraday last trade price, NOT the basis NAV. Renaming to
`market_price` removes the misleading label. A separate `unit_nav`
column holds the actual basis NAV per share (`最新单位净值` from
`analytics_data:get_financial_data`).

Both columns stay nullable — the same `shares_status` + `missing_reason`
contract Linda locked still applies when either tool can't return a
value.

For SQLite (dev + tests) we use `batch_alter_table` because plain
`ALTER COLUMN RENAME` is a Postgres-only operation in Alembic <
2.0; batch mode emits a portable recreate-table flow that works on
both backends.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f9a4c1e8b203"
down_revision: Union[str, None] = "297272847a56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("etf_daily_snapshot") as batch_op:
        batch_op.alter_column(
            "nav",
            new_column_name="market_price",
            existing_type=sa.Float(),
            existing_nullable=True,
        )
        batch_op.add_column(
            sa.Column("unit_nav", sa.Float(), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("etf_daily_snapshot") as batch_op:
        batch_op.drop_column("unit_nav")
        batch_op.alter_column(
            "market_price",
            new_column_name="nav",
            existing_type=sa.Float(),
            existing_nullable=True,
        )
