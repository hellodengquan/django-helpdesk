import errno
import os
import logging
import tempfile
import uuid
from contextlib import contextmanager

from django.core.files.storage import FileSystemStorage
from django.core.files import locks
from django.core.files.move import file_move_safe

logger = logging.getLogger("helpdesk")


class RemoteStorageError(Exception):
    """Base exception for remote storage errors."""
    pass


class RemoteStorageUnreachable(RemoteStorageError):
    """Raised when remote storage backend is unreachable."""
    pass


class MultipartUploadTracker:
    """
    Tracks multipart uploads for remote storage backends,
    enabling cleanup of incomplete uploads on failure.
    """

    def __init__(self):
        self._active_uploads = {}

    def start_upload(self, storage_name, upload_id, file_path=None):
        """Register a new multipart upload."""
        key = (storage_name, upload_id)
        self._active_uploads[key] = {
            'file_path': file_path,
            'parts': [],
            'started_at': None,
        }
        logger.info("Started tracking multipart upload %s for %s", upload_id, storage_name)
        return key

    def add_part(self, key, part_number, part_info):
        """Track an uploaded part."""
        if key in self._active_uploads:
            self._active_uploads[key]['parts'].append({
                'part_number': part_number,
                'info': part_info,
            })

    def complete_upload(self, key):
        """Mark upload as complete and stop tracking."""
        if key in self._active_uploads:
            upload_id = key[1]
            del self._active_uploads[key]
            logger.info("Completed multipart upload %s", upload_id)

    def abort_upload(self, key):
        """Mark upload as aborted and stop tracking."""
        if key in self._active_uploads:
            upload_id = key[1]
            del self._active_uploads[key]
            logger.info("Aborted multipart upload %s", upload_id)

    def get_pending_uploads(self, storage_name=None):
        """Get all pending uploads, optionally filtered by storage name."""
        return [
            {'upload_id': k[1], 'info': v}
            for k, v in self._active_uploads.items()
            if storage_name is None or k[0] == storage_name
        ]

    def clear_all(self):
        """Clear all tracked uploads (for testing/cleanup)."""
        self._active_uploads.clear()


_multipart_tracker = MultipartUploadTracker()


def get_multipart_tracker():
    """Get the global multipart upload tracker."""
    return _multipart_tracker


class RemoteStorageMixin:
    """
    Mixin class that provides common functionality for remote storage backends,
    including connection health checks, local temp file cleanup, and
    multipart upload tracking for orphaned part recovery.
    """

    REMOTE_ERRORS = (
        ConnectionError,
        TimeoutError,
        OSError,
        RemoteStorageError,
    )

    def __init__(self, *args, **kwargs):
        self._connection_test_interval = 60
        self._last_connection_check = 0
        super().__init__(*args, **kwargs)

    def is_reachable(self):
        """Check if the remote storage backend is reachable."""
        try:
            self._check_connection()
            return True
        except Exception:
            return False

    def _check_connection(self):
        """
        Override this method in subclasses to implement connection checking.
        Should raise RemoteStorageUnreachable if connection fails.
        """
        pass

    def _cleanup_local_temp_file(self, file_path):
        """Clean up a local temporary file if it exists."""
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
                logger.info("Cleaned up local temp file '%s' after remote storage failure", file_path)
            except OSError as e:
                logger.warning("Failed to clean up local temp file '%s': %s", file_path, str(e))

    def _abort_multipart_upload(self, upload_id):
        """
        Override this method in subclasses to abort a multipart upload
        and clean up any already-uploaded parts on the remote storage.
        """
        tracker = get_multipart_tracker()
        key = (self.__class__.__name__, upload_id)
        tracker.abort_upload(key)
        logger.info("Aborted multipart upload %s", upload_id)

    @contextmanager
    def _upload_operation(self, name, content):
        """
        Context manager for upload operations that ensures cleanup
        of local temp files and remote multipart parts on failure.
        """
        temp_file_path = None
        upload_id = None
        tracker = get_multipart_tracker()

        try:
            if hasattr(content, 'temporary_file_path'):
                temp_file_path = content.temporary_file_path()

            yield {
                'temp_file_path': temp_file_path,
                'tracker': tracker,
                'register_upload': lambda uid: tracker.start_upload(
                    self.__class__.__name__, uid, temp_file_path
                ),
                'add_part': lambda key, num, info: tracker.add_part(key, num, info),
                'complete_upload': lambda key: tracker.complete_upload(key),
            }
        except self.REMOTE_ERRORS as e:
            logger.error("Remote storage operation failed for '%s': %s", name, str(e))

            if upload_id:
                try:
                    self._abort_multipart_upload(upload_id)
                except Exception as abort_e:
                    logger.warning("Failed to abort multipart upload %s: %s", upload_id, str(abort_e))

            if temp_file_path:
                self._cleanup_local_temp_file(temp_file_path)

            pending = tracker.get_pending_uploads(self.__class__.__name__)
            for pending_upload in pending:
                try:
                    self._abort_multipart_upload(pending_upload['upload_id'])
                except Exception as abort_e:
                    logger.warning(
                        "Failed to abort pending multipart upload %s: %s",
                        pending_upload['upload_id'], str(abort_e)
                    )

            raise
        except Exception:
            if temp_file_path:
                self._cleanup_local_temp_file(temp_file_path)
            raise

    def _save(self, name, content):
        """
        Wrap the save operation with our upload context manager
        to ensure cleanup on remote storage failures.
        """
        with self._upload_operation(name, content) as ctx:
            return super()._save(name, content)


class SafeFileSystemStorage(RemoteStorageMixin, FileSystemStorage):
    """
    A FileSystemStorage subclass that ensures partial files are cleaned up
    when saving fails due to disk full (ENOSPC), permission errors, or
    any other I/O exception.

    Django's default FileSystemStorage._save() can leave partial files on
    disk if writing the content fails mid-stream (e.g., disk full). This
    class wraps the file-writing section in a try/finally so that any
    partially-written file is removed when an exception occurs.
    """

    def _save(self, name, content):
        full_path = self.path(name)

        directory = os.path.dirname(full_path)
        try:
            if self.directory_permissions_mode is not None:
                from django.core.files.storage import safe_makedirs
                safe_makedirs(directory, self.directory_permissions_mode, exist_ok=True)
            else:
                os.makedirs(directory, exist_ok=True)
        except FileExistsError:
            raise FileExistsError("%s exists and is not a directory." % directory)
        except OSError as e:
            logger.warning(
                "Failed to create directory '%s' for attachment: %s",
                directory,
                str(e),
            )
            raise

        while True:
            created_paths_to_cleanup = []
            try:
                if hasattr(content, "temporary_file_path"):
                    tmp_path = content.temporary_file_path()
                    file_move_safe(
                        tmp_path,
                        full_path,
                        allow_overwrite=self._allow_overwrite,
                    )
                else:
                    open_flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_BINARY", 0)
                    )
                    if self.OS_OPEN_FLAGS != open_flags:
                        open_flags = self.OS_OPEN_FLAGS
                    elif self._allow_overwrite:
                        open_flags = open_flags & ~os.O_EXCL | os.O_TRUNC
                    fd = os.open(full_path, open_flags, 0o666)
                    created_paths_to_cleanup.append(full_path)
                    _file = None
                    try:
                        locks.lock(fd, locks.LOCK_EX)
                        for chunk in content.chunks():
                            if _file is None:
                                mode = "wb" if isinstance(chunk, bytes) else "wt"
                                _file = os.fdopen(fd, mode)
                            _file.write(chunk)
                    finally:
                        try:
                            locks.unlock(fd)
                        except Exception:
                            pass
                        if _file is not None:
                            try:
                                _file.close()
                            except Exception:
                                pass
                        else:
                            try:
                                os.close(fd)
                            except Exception:
                                pass
            except FileExistsError:
                name = self.get_available_name(name)
                full_path = self.path(name)
                continue
            except Exception:
                for p in created_paths_to_cleanup:
                    try:
                        if os.path.exists(p):
                            os.unlink(p)
                            logger.info(
                                "Cleaned up partial file '%s' after save failure",
                                p,
                            )
                    except OSError as cleanup_e:
                        logger.warning(
                            "Failed to clean up partial file '%s': %s",
                            p,
                            str(cleanup_e),
                        )
                raise
            else:
                break

        if self.file_permissions_mode is not None:
            try:
                os.chmod(full_path, self.file_permissions_mode)
            except OSError as e:
                logger.warning(
                    "Failed to chmod saved file '%s': %s",
                    full_path,
                    str(e),
                )

        name = os.path.relpath(full_path, self.location)
        try:
            self._ensure_location_group_id(full_path)
        except Exception as e:
            logger.warning(
                "Failed to ensure group id for '%s': %s",
                full_path,
                str(e),
            )
        return str(name).replace("\\", "/")


class MockRemoteStorage(RemoteStorageMixin, FileSystemStorage):
    """
    A mock remote storage backend for testing purposes.
    Simulates remote storage behavior including:
    - Configurable connectivity failures
    - Multipart upload simulation
    - Network timeouts
    """

    def __init__(self, *args, **kwargs):
        self._is_reachable = True
        self._fail_next_save = False
        self._fail_mode = None
        self._upload_counter = 0
        self._simulated_latency = 0
        super().__init__(*args, **kwargs)

    def set_unreachable(self, unreachable=True):
        """Set whether the storage should simulate being unreachable."""
        self._is_reachable = not unreachable

    def set_fail_next_save(self, fail=True, mode='connection'):
        """
        Make the next save operation fail with the specified error mode.
        Modes: 'connection', 'timeout', 'partial_upload'
        """
        self._fail_next_save = fail
        self._fail_mode = mode

    def _check_connection(self):
        """Override to simulate connection checking."""
        if not self._is_reachable:
            raise RemoteStorageUnreachable("Mock remote storage is unreachable")

    def _simulate_multipart_upload(self, name, content):
        """
        Simulate a multipart upload that can fail mid-way,
        leaving orphan parts that need cleanup.
        """
        upload_id = f"mock-upload-{uuid.uuid4().hex}"
        tracker = get_multipart_tracker()
        key = tracker.start_upload(self.__class__.__name__, upload_id)

        chunk_size = 1024 * 1024
        chunks = []
        data = content.read() if hasattr(content, 'read') else content

        for i in range(0, len(data), chunk_size):
            chunks.append(data[i:i + chunk_size])

        for part_num, chunk in enumerate(chunks, 1):
            if self._fail_mode == 'partial_upload' and part_num > len(chunks) // 2:
                raise ConnectionError("Network error during multipart upload")

            part_path = f"{name}.part{part_num}"
            full_path = self.path(part_path)
            directory = os.path.dirname(full_path)
            os.makedirs(directory, exist_ok=True)
            with open(full_path, 'wb') as f:
                f.write(chunk)

            tracker.add_part(key, part_num, {'path': part_path, 'size': len(chunk)})

        self._complete_multipart_upload(name, key, chunks)
        return name

    def _complete_multipart_upload(self, name, key, chunks):
        """Simulate completing a multipart upload by combining parts."""
        full_path = self.path(name)
        directory = os.path.dirname(full_path)
        os.makedirs(directory, exist_ok=True)

        with open(full_path, 'wb') as outfile:
            for chunk in chunks:
                outfile.write(chunk)

        tracker = get_multipart_tracker()
        tracker.complete_upload(key)

    def _abort_multipart_upload(self, upload_id):
        """Override to clean up simulated multipart parts."""
        tracker = get_multipart_tracker()
        key = (self.__class__.__name__, upload_id)

        if key in tracker._active_uploads:
            upload_info = tracker._active_uploads[key]
            for part in upload_info.get('parts', []):
                part_path = self.path(part['info']['path'])
                if os.path.exists(part_path):
                    try:
                        os.unlink(part_path)
                        logger.info("Cleaned up remote part '%s'", part_path)
                    except OSError as e:
                        logger.warning("Failed to clean up remote part '%s': %s", part_path, str(e))

        super()._abort_multipart_upload(upload_id)

    def _save(self, name, content):
        """
        Override _save to simulate remote storage behavior.
        Can fail with various error modes for testing.
        """
        with self._upload_operation(name, content) as ctx:
            if self._fail_next_save:
                self._fail_next_save = False

                if self._fail_mode == 'connection':
                    raise ConnectionError("Connection refused to mock remote storage")
                elif self._fail_mode == 'timeout':
                    raise TimeoutError("Connection timeout to mock remote storage")
                elif self._fail_mode == 'partial_upload':
                    return self._simulate_multipart_upload(name, content)

            if not self._is_reachable:
                raise RemoteStorageUnreachable("Mock remote storage is unreachable")

            return super()._save(name, content)


def cleanup_all_pending_multipart_uploads():
    """
    Clean up all pending multipart uploads across all storage backends.
    This should be called periodically or during shutdown.
    """
    tracker = get_multipart_tracker()
    pending = tracker.get_pending_uploads()

    for upload_info in pending:
        upload_id = upload_info['upload_id']
        try:
            storage_class = upload_info['info'].get('storage_class')
            if storage_class and hasattr(storage_class, '_abort_multipart_upload'):
                storage = storage_class()
                storage._abort_multipart_upload(upload_id)
            else:
                tracker.abort_upload(('GlobalCleanup', upload_id))
        except Exception as e:
            logger.warning("Failed to clean up multipart upload %s: %s", upload_id, str(e))

    return len(pending)
