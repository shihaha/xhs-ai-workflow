"""SQLite database lifecycle for durable local workbench facts."""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Base class for all persisted workbench records."""


class Database:
    """Own the local SQLite engine and initialize its durable schema."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        event.listen(self.engine, "connect", _configure_sqlite)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    def initialize(self) -> None:
        """Create schema without creating any business records."""
        from backend.app.models.jobs import JobArtifactRecord, JobLogRecord, JobRecord

        _ = (JobArtifactRecord, JobLogRecord, JobRecord)
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.sessions()

    def close(self) -> None:
        """Release pooled SQLite handles before an app instance is replaced."""
        self.engine.dispose()


def _configure_sqlite(connection: object, _: object) -> None:
    cursor = connection.cursor()  # type: ignore[union-attr]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
