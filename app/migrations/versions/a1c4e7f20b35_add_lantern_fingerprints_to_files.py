"""Add the SHA-256 fingerprint of each file, for Lantern.

Revision ID: a1c4e7f20b35
Revises: d8e7f6a5b4c3
"""

from alembic import op
import sqlalchemy as sa


revision = 'a1c4e7f20b35'
down_revision = 'd8e7f6a5b4c3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('files') as batch_op:
        batch_op.add_column(sa.Column('sha256', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('mtime_ns', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('hash_priority', sa.Integer(), nullable=False, server_default='0'))
        batch_op.create_index('idx_files_sha256', ['sha256'])


def downgrade():
    with op.batch_alter_table('files') as batch_op:
        batch_op.drop_index('idx_files_sha256')
        batch_op.drop_column('hash_priority')
        batch_op.drop_column('mtime_ns')
        batch_op.drop_column('sha256')
