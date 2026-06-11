import errno
import os
import logging

from django.core.files.storage import FileSystemStorage
from django.core.files import locks
from django.core.files.move import file_move_safe

logger = logging.getLogger("helpdesk")


class SafeFileSystemStorage(FileSystemStorage):
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
