"""make learned and reminder exclusive

Revision ID: 0003_make_learned_and_reminder_exclusive
Revises: 0002_add_reminders
Create Date: 2026-06-15
"""

from datetime import UTC, datetime, timedelta

from alembic import op
import sqlalchemy as sa


revision = "0003_make_learned_and_reminder_exclusive"
down_revision = "0002_add_reminders"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inserted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=40)
    remind_at = inserted_at + timedelta(minutes=30)

    bind.execute(
        sa.text(
            """
            UPDATE reminder
            SET
                inserted_at = :inserted_at,
                remind_at = :remind_at,
                progression_model = 'default',
                progression_index = 0,
                progression_label = '30m'
            WHERE EXISTS (
                SELECT 1
                FROM word
                WHERE word.user_id = reminder.user_id
                  AND word.collection_number = reminder.collection_number
                  AND lower(word.name) = reminder.name
                  AND word.learned = 1
            )
            """
        ),
        {"inserted_at": inserted_at, "remind_at": remind_at},
    )
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
    bind.execute(sa.text("UPDATE word SET learned = 0 WHERE learned = 1"))


def downgrade():
    # The original learned/reminder overlap cannot be reconstructed safely.
    pass
