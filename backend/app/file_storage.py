from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

try:
    from minio import Minio
except ImportError:  # pragma: no cover
    Minio = None

log = logging.getLogger(__name__)

# Ключ PDF-превью офисного документа: <ключ оригинала> + этот суффикс —
# превью хранится в бакете рядом с оригиналом
PREVIEW_SUFFIX = ".preview.pdf"


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    object_name: str
    uri: str


class MinioFileStorage:
    def __init__(
        self,
        endpoint: str | None,
        access_key: str | None,
        secret_key: str | None,
        bucket: str | None,
        secure: bool = False,
    ):
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.secure = secure
        self.last_error: str | None = None
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(Minio and self.endpoint and self.access_key and self.secret_key and self.bucket)

    def put_document(
        self,
        document_id: str,
        version_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> StoredObject | None:
        if not self.enabled:
            return None
        try:
            client = self._get_client()
            assert self.bucket is not None
            if not client.bucket_exists(self.bucket):
                client.make_bucket(self.bucket)
            object_name = f"documents/{document_id}/{version_id}/{_safe_name(filename)}"
            client.put_object(
                self.bucket,
                object_name,
                io.BytesIO(content),
                length=len(content),
                content_type=content_type,
            )
            stored = StoredObject(bucket=self.bucket, object_name=object_name, uri=f"s3://{self.bucket}/{object_name}")
            self.last_error = None
            return stored
        except Exception as exc:
            # Оригинал документа — часть провенанса: терять его молча нельзя,
            # инжест должен завершиться как failed с текстом ошибки
            self.last_error = str(exc)
            log.error("MinIO: файл документа %s не сохранён: %s", document_id, exc)
            raise

    def put_preview(self, object_name: str, content: bytes) -> bool:
        """Сохраняет PDF-превью офисного документа по точному ключу (рядом с оригиналом).

        Превью необязательно: любой сбой не пробрасывается (warning в лог,
        False) — инжест не должен завершаться ошибкой из-за недоступного MinIO.
        """
        if not self.enabled:
            return False
        try:
            client = self._get_client()
            assert self.bucket is not None
            if not client.bucket_exists(self.bucket):
                client.make_bucket(self.bucket)
            client.put_object(
                self.bucket,
                object_name,
                io.BytesIO(content),
                length=len(content),
                content_type="application/pdf",
            )
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("MinIO: PDF-превью %s не сохранено: %s", object_name, exc)
            return False

    def _read_object(self, client, object_name: str) -> bytes:
        """Читает объект целиком, всегда возвращая соединение в пул urllib3."""
        response = client.get_object(self.bucket, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def get_object(self, object_name: str) -> bytes | None:
        """Содержимое объекта по точному ключу (оригинал или превью).

        None — хранилище выключено, объекта нет или сбой MinIO (ошибка в лог,
        не пробрасывается: вызывающие пути отвечают 404/пропуском).
        """
        if not self.enabled:
            return None
        try:
            client = self._get_client()
            assert self.bucket is not None
            data = self._read_object(client, object_name)
            self.last_error = None
            return data
        except Exception as exc:
            self.last_error = str(exc)
            log.error("MinIO: объект %s не получен: %s", object_name, exc)
            return None

    def get_document(self, document_id: str) -> tuple[bytes, str] | None:
        """Оригинальный файл документа: (содержимое, имя объекта в бакете).

        None — хранилище выключено или файла нет; ошибки MinIO пробрасываются
        (клиент должен увидеть сбой, а не пустой ответ).
        """
        if not self.enabled:
            return None
        try:
            client = self._get_client()
            assert self.bucket is not None
            prefix = f"documents/{document_id}/"
            objects = [
                obj for obj in client.list_objects(self.bucket, prefix=prefix, recursive=True)
                # PDF-превью хранится под тем же префиксом — оригиналом не считается
                if not obj.object_name.endswith(PREVIEW_SUFFIX)
            ]
            if not objects:
                return None
            # У документа одна версия — под префиксом хранится один оригинал
            object_name = objects[0].object_name
            data = self._read_object(client, object_name)
            self.last_error = None
            return data, Path(object_name).name
        except Exception as exc:
            self.last_error = str(exc)
            log.error("MinIO: файл документа %s не получен: %s", document_id, exc)
            raise

    def delete_document(self, document_id: str) -> None:
        if not self.enabled:
            return
        try:
            client = self._get_client()
            assert self.bucket is not None
            prefix = f"documents/{document_id}/"
            for obj in list(client.list_objects(self.bucket, prefix=prefix, recursive=True)):
                client.remove_object(self.bucket, obj.object_name)
            self.last_error = None
        except Exception as exc:
            # Осиротевшие объекты копятся навсегда: клиент должен увидеть
            # ошибку и повторить удаление
            self.last_error = str(exc)
            log.error("MinIO: файлы документа %s не удалены: %s", document_id, exc)
            raise

    def _get_client(self):
        if self._client is None:
            self._client = Minio(
                self.endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
            )
        return self._client


def _safe_name(filename: str) -> str:
    name = Path(filename).name or "document.bin"
    return "".join(char if char.isalnum() or char in ".-_ " else "_" for char in name)[:180]
