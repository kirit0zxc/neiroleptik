"""Логическое удаление назначения без потери событий и истории команд."""

import sqlalchemy as sa
from alembic import op

revision = "d94b1c5e2f01"
down_revision = "8c4bb233af01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Старые назначения остаются активными; deleted_at пуст по умолчанию."""
    op.add_column("deployments", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Возвращаем прежнюю схему только при отсутствии удалённых назначений."""
    # После удаления порты могли быть заняты новым назначением: нельзя оживлять старое.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM deployments WHERE deleted_at IS NOT NULL) "
        "THEN RAISE EXCEPTION 'Cannot downgrade with deleted deployments'; END IF; END $$"
    )
    op.drop_column("deployments", "deleted_at")
