"""Process-level settings.

Only things needed *before* the database exists live here (paths, bind address,
secrets). Everything the user is expected to tune -- Ollama endpoint, poll
intervals, the thread hold-open window -- lives in the `settings` table and is
edited from the web UI. See app/settings_store.py.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RSSOPDS_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 8000

    # Optional HTTP basic auth. An empty user disables the check entirely.
    # The OPDS pair falls back to the web pair when left blank, so setting
    # just the web credentials protects both.
    web_user: str = ""
    web_password: str = ""
    opds_user: str = ""
    opds_password: str = ""
    # Escape hatch for readers that cannot send HTTP credentials: leaves the
    # OPDS catalogue open while the web UI stays behind a password.
    opds_public: bool = False

    # Public base URL, used to build absolute links in the OPDS catalog.
    # Leave blank to derive it from the incoming request.
    base_url: str = ""

    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rssopds.db"

    @property
    def epub_dir(self) -> Path:
        return self.data_dir / "epub"

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def cover_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def ebooks_dir(self) -> Path:
        # Not managed by the app at all -- the user drops files and folders
        # here directly (it's a bind mount on the host), and /opds/ebooks
        # just mirrors whatever tree it finds. No AI cleaning, no read
        # tracking, no covers: a dumb, browsable file share over OPDS.
        return self.data_dir / "ebooks"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.epub_dir, self.image_dir, self.cover_dir,
                 self.ebooks_dir):
            d.mkdir(parents=True, exist_ok=True)


config = Config()
