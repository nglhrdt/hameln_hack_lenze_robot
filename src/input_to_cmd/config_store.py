from pathlib import Path
from threading import Lock
from typing import Annotated, Literal, TypeAlias

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, UrlConstraints, model_validator
from tinydb import TinyDB


IframeUrl: TypeAlias = Annotated[AnyHttpUrl, UrlConstraints(max_length=2048)]


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_type: Literal["kasten", "raeder"]
    iframe_url: IframeUrl

    @model_validator(mode="after")
    def reject_url_credentials(self) -> "Configuration":
        if self.iframe_url.username is not None or self.iframe_url.password is not None:
            raise ValueError("The iframe URL must not contain credentials")
        return self


class ConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_type: Literal["kasten", "raeder"] | None = None
    iframe_url: IframeUrl | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "ConfigurationUpdate":
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("Provide at least one non-null configuration value")
        if self.iframe_url is not None and (
            self.iframe_url.username is not None or self.iframe_url.password is not None
        ):
            raise ValueError("The iframe URL must not contain credentials")
        return self


DEFAULT_CONFIGURATION = Configuration.model_validate({
    "device_type": "kasten", "iframe_url": "http://192.168.5.203:5000",
})


class ConfigurationStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def read(self) -> Configuration:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with TinyDB(self.path) as db:
                document = db.get(doc_id=1)
                if document is None:
                    if len(db):
                        raise ValueError("Configuration document has an unexpected ID")
                    return DEFAULT_CONFIGURATION.model_copy()
                return Configuration.model_validate(document)

    def update(self, changes: ConfigurationUpdate) -> Configuration:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with TinyDB(self.path) as db:
                document = db.get(doc_id=1)
                if document is None and len(db):
                    raise ValueError("Configuration document has an unexpected ID")
                current = (
                    DEFAULT_CONFIGURATION if document is None
                    else Configuration.model_validate(document)
                )
                merged = current.model_dump(mode="json")
                merged.update(changes.model_dump(mode="json", exclude_unset=True))
                updated = Configuration.model_validate(merged)
                if document is None:
                    db.insert(updated.model_dump(mode="json"))
                else:
                    db.update(updated.model_dump(mode="json"), doc_ids=[1])
                return updated
