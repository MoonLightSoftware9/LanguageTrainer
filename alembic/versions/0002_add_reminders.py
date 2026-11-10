"""add reminders

Revision ID: 0002_add_reminders
Revises: 0001_users_collections_words
Create Date: 2026-06-15
"""

from datetime import UTC, datetime, timedelta

from alembic import op
import sqlalchemy as sa


revision = "0002_add_reminders"
down_revision = "0001_users_collections_words"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reminder",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("collection_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("inserted_at", sa.DateTime(), nullable=False),
        sa.Column("remind_at", sa.DateTime(), nullable=False),
        sa.Column("progression_model", sa.String(), nullable=False, server_default="default"),
        sa.Column("progression_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progression_label", sa.String(), nullable=False, server_default="30m"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "collection_number",
            "name",
            name="uq_reminder_user_collection_name",
        ),
    )
    op.create_index("ix_reminder_collection_number", "reminder", ["collection_number"])
    op.create_index("ix_reminder_inserted_at", "reminder", ["inserted_at"])
    op.create_index("ix_reminder_name", "reminder", ["name"])
    op.create_index("ix_reminder_progression_index", "reminder", ["progression_index"])
    op.create_index("ix_reminder_progression_label", "reminder", ["progression_label"])
    op.create_index("ix_reminder_progression_model", "reminder", ["progression_model"])
    op.create_index("ix_reminder_remind_at", "reminder", ["remind_at"])
    op.create_index("ix_reminder_user_id", "reminder", ["user_id"])

    bind = op.get_bind()
    inserted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=40)
    remind_at = inserted_at + timedelta(minutes=30)
    bind.execute(
        sa.text(
            """
            INSERT OR IGNORE INTO reminder (
                user_id,
                collection_number,
                name,
                inserted_at,
                remind_at,
                progression_model,
                progression_index,
                progression_label
            )
            SELECT
                user_id,
                collection_number,
                lower(name),
                :inserted_at,
                :remind_at,
                'default',
                0,
                '30m'
            FROM word
            WHERE learned = 1
            """
        ),
        {"inserted_at": inserted_at, "remind_at": remind_at},
    )


def downgrade():
    op.drop_index("ix_reminder_user_id", table_name="reminder")
    op.drop_index("ix_reminder_remind_at", table_name="reminder")
    op.drop_index("ix_reminder_progression_model", table_name="reminder")
    op.drop_index("ix_reminder_progression_label", table_name="reminder")
    op.drop_index("ix_reminder_progression_index", table_name="reminder")
    op.drop_index("ix_reminder_name", table_name="reminder")
    op.drop_index("ix_reminder_inserted_at", table_name="reminder")
    op.drop_index("ix_reminder_collection_number", table_name="reminder")
    op.drop_table("reminder")
