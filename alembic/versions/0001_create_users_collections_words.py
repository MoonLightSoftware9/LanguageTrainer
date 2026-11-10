"""create users collections words

Revision ID: 0001_users_collections_words
Revises:
Create Date: 2026-06-11
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_users_collections_words"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("default_collection_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("default_lang", sa.String(), nullable=False, server_default="EN"),
        sa.Column("all_lang", sa.String(), nullable=False, server_default="EN,CY,RU"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_default_lang", "user", ["default_lang"])
    op.create_index("ix_user_name", "user", ["name"])

    op.create_table(
        "collection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("lang", sa.String(), nullable=False, server_default="EN"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "number", name="uq_collection_user_number"),
    )
    op.create_index("ix_collection_lang", "collection", ["lang"])
    op.create_index("ix_collection_number", "collection", ["number"])
    op.create_index("ix_collection_user_id", "collection", ["user_id"])

    op.create_table(
        "word",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("collection_number", sa.Integer(), nullable=False),
        sa.Column("learned", sa.Boolean(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="default"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_word_collection_number", "word", ["collection_number"])
    op.create_index("ix_word_learned", "word", ["learned"])
    op.create_index("ix_word_name", "word", ["name"])
    op.create_index("ix_word_status", "word", ["status"])
    op.create_index("ix_word_user_id", "word", ["user_id"])

    op.create_table(
        "meaning",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("collection_number", sa.Integer(), nullable=False),
        sa.Column("learned", sa.Boolean(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="meanings"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meaning_collection_number", "meaning", ["collection_number"])
    op.create_index("ix_meaning_learned", "meaning", ["learned"])
    op.create_index("ix_meaning_name", "meaning", ["name"])
    op.create_index("ix_meaning_status", "meaning", ["status"])
    op.create_index("ix_meaning_user_id", "meaning", ["user_id"])

    op.create_table(
        "stopword",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_stop_word_user_name"),
    )
    op.create_index("ix_stopword_name", "stopword", ["name"])
    op.create_index("ix_stopword_user_id", "stopword", ["user_id"])


def downgrade():
    op.drop_index("ix_stopword_user_id", table_name="stopword")
    op.drop_index("ix_stopword_name", table_name="stopword")
    op.drop_table("stopword")

    op.drop_index("ix_meaning_user_id", table_name="meaning")
    op.drop_index("ix_meaning_status", table_name="meaning")
    op.drop_index("ix_meaning_name", table_name="meaning")
    op.drop_index("ix_meaning_learned", table_name="meaning")
    op.drop_index("ix_meaning_collection_number", table_name="meaning")
    op.drop_table("meaning")

    op.drop_index("ix_word_user_id", table_name="word")
    op.drop_index("ix_word_status", table_name="word")
    op.drop_index("ix_word_name", table_name="word")
    op.drop_index("ix_word_learned", table_name="word")
    op.drop_index("ix_word_collection_number", table_name="word")
    op.drop_table("word")

    op.drop_index("ix_collection_user_id", table_name="collection")
    op.drop_index("ix_collection_lang", table_name="collection")
    op.drop_index("ix_collection_number", table_name="collection")
    op.drop_table("collection")

    op.drop_index("ix_user_name", table_name="user")
    op.drop_index("ix_user_default_lang", table_name="user")
    op.drop_table("user")
