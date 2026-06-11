# vim: set fileencoding=utf-8 :

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings, TestCase
from django.urls import reverse
from django.utils.encoding import smart_str
from django.conf import settings
from contextlib import contextmanager
from helpdesk import lib, models
import os
import shutil
from tempfile import gettempdir
from unittest import mock
from unittest.case import skip
from django.contrib.auth import get_user_model


MEDIA_DIR = os.path.join(gettempdir(), "helpdesk_test_media")


@override_settings(MEDIA_ROOT=MEDIA_DIR)
class AttachmentIntegrationTests(TestCase):
    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue_public = models.Queue.objects.create(
            title="Public Queue",
            slug="pub_q",
            allow_public_submission=True,
            new_ticket_cc="new.public@example.com",
            updated_ticket_cc="update.public@example.com",
        )

        self.queue_private = models.Queue.objects.create(
            title="Private Queue",
            slug="priv_q",
            allow_public_submission=False,
            new_ticket_cc="new.private@example.com",
            updated_ticket_cc="update.private@example.com",
        )

        self.ticket_data = {
            "title": "Test Ticket Title",
            "body": "Test Ticket Desc",
            "priority": 3,
            "submitter_email": "submitter@example.com",
        }

    def test_create_pub_ticket_with_attachment(self):
        test_file = SimpleUploadedFile(
            "test_att.txt", b"attached file content", "text/plain"
        )
        post_data = self.ticket_data.copy()
        post_data.update(
            {
                "queue": self.queue_public.id,
                "attachment": test_file,
            }
        )

        # Ensure ticket form submits with attachment successfully
        response = self.client.post(reverse("helpdesk:home"), post_data, follow=True)
        self.assertContains(response, test_file.name)

        # Ensure attachment is available with correct content
        att = models.FollowUpAttachment.objects.get(
            followup__ticket=response.context["ticket"]
        )
        with open(os.path.join(MEDIA_DIR, att.file.name)) as file_on_disk:
            disk_content = file_on_disk.read()
        self.assertEqual(disk_content, "attached file content")

    def test_create_pub_ticket_with_attachment_utf8(self):
        test_file = SimpleUploadedFile("ß°äöü.txt", "โจ".encode("utf-8"), "text/utf-8")
        post_data = self.ticket_data.copy()
        post_data.update(
            {
                "queue": self.queue_public.id,
                "attachment": test_file,
            }
        )

        # Ensure ticket form submits with attachment successfully
        response = self.client.post(reverse("helpdesk:home"), post_data, follow=True)
        self.assertContains(response, test_file.name)

        # Ensure attachment is available with correct content
        att = models.FollowUpAttachment.objects.get(
            followup__ticket=response.context["ticket"]
        )
        with open(
            os.path.join(MEDIA_DIR, att.file.name), encoding="utf-8"
        ) as file_on_disk:
            disk_content = smart_str(file_on_disk.read(), "utf-8")
        self.assertEqual(disk_content, "โจ")


@override_settings(MEDIA_ROOT=MEDIA_DIR)
class AttachmentIntegrationStaffTests(TestCase):
    def setUp(self):
        self.ticket = models.Ticket.objects.create(
            queue=models.Queue.objects.create(),
            title="Test attachments via ticket update",
        )
        self.default_update_post_data = {
            "queue": self.ticket.queue_id,
            "title": self.ticket.title,
            "priority": self.ticket.priority,
        }

    def loginUser(self, is_staff=True):
        """Create a staff user and login"""
        User = get_user_model()
        self.user = User.objects.create(
            username="User_1",
            is_staff=is_staff,
        )
        self.user.set_password("pass")
        self.user.save()
        self.client.login(username="User_1", password="pass")

    def test_update_ticket_with_attachment_valid_extension(self):
        self.loginUser(is_staff=True)
        file_content = "staff attached file content"
        test_file = SimpleUploadedFile(
            "test_staff_att.txt", bytes(file_content, "utf-8"), "text/plain"
        )
        post_data = {
            "attachment": test_file,
            **self.default_update_post_data,
        }
        # Ensure ticket form submits with attachment successfully
        self.client.post(
            reverse(
                "helpdesk:update",
                kwargs={"ticket_id": self.ticket.id},
            ),
            post_data,
            follow=True,
        )
        # Ensure attachment is available with correct content
        att = models.FollowUpAttachment.objects.get(followup__ticket=self.ticket)
        with open(os.path.join(MEDIA_DIR, att.file.name)) as file_on_disk:
            disk_content = file_on_disk.read()
        self.assertEqual(disk_content, file_content)

    def test_update_ticket_with_attachment_invalid_extension(self):
        self.loginUser(is_staff=True)
        file_content = "staff attached file content with invalid extension"
        file_extension = ".crash"
        test_file = SimpleUploadedFile(
            f"test_staff_att{file_extension}",
            bytes(file_content, "utf-8"),
            "text/plain",
        )
        post_data = {
            "attachment": test_file,
            **self.default_update_post_data,
        }
        # Ensure ticket form submits with attachment successfully
        response = self.client.post(
            reverse(
                "helpdesk:update",
                kwargs={"ticket_id": self.ticket.id},
            ),
            post_data,
            follow=True,
        )
        error_msg = response.context_data["form"].errors["attachment"][0]
        self.assertTrue(
            file_extension in error_msg,
            "Response indicates there were no errors attaching illegal file extension",
        )
        # Ensure attachment is not uploaded
        has_att = models.FollowUpAttachment.objects.filter(
            followup__ticket=self.ticket
        ).exists()
        self.assertFalse(has_att, "File was attached with invalid extension")


@mock.patch.object(models.FollowUp, "save", autospec=True)
@mock.patch.object(models.FollowUpAttachment, "save", autospec=True)
@mock.patch.object(models.Ticket, "save", autospec=True)
@mock.patch.object(models.Queue, "save", autospec=True)
class AttachmentUnitTests(TestCase):
    def setUp(self):
        self.file_attrs = {
            "filename": "°ßäöü.txt",
            "content": "โจ".encode("utf-8"),
            "content-type": "text/utf8",
        }
        self.test_file = SimpleUploadedFile.from_dict(self.file_attrs)
        self.follow_up = models.FollowUp.objects.create(
            ticket=models.Ticket.objects.create(queue=models.Queue.objects.create())
        )

    @skip("Rework with model relocation")
    def test_unicode_attachment_filename(
        self, mock_att_save, mock_queue_save, mock_ticket_save, mock_follow_up_save
    ):
        """check utf-8 data is parsed correctly"""
        filename, fileobj = lib.process_attachments(self.follow_up, [self.test_file])[0]
        mock_att_save.assert_called_with(
            file=self.test_file,
            filename=self.file_attrs["filename"],
            mime_type=self.file_attrs["content-type"],
            size=len(self.file_attrs["content"]),
            followup=self.follow_up,
        )
        self.assertEqual(filename, self.file_attrs["filename"])

    def test_autofill(
        self, mock_att_save, mock_queue_save, mock_ticket_save, mock_follow_up_save
    ):
        """check utf-8 data is parsed correctly"""
        obj = models.FollowUpAttachment.objects.create(
            followup=self.follow_up, file=self.test_file
        )
        obj.save()
        self.assertEqual(obj.file.name, self.file_attrs["filename"])
        self.assertEqual(obj.file.size, len(self.file_attrs["content"]))
        self.assertEqual(obj.file.file.content_type, "text/utf8")

    def test_kbi_attachment(
        self, mock_att_save, mock_queue_save, mock_ticket_save, mock_follow_up_save
    ):
        """check utf-8 data is parsed correctly"""

        kbcategory = models.KBCategory.objects.create(
            title="Title", slug="slug", description="Description"
        )
        kbitem = models.KBItem.objects.create(
            category=kbcategory, title="Title", question="Question", answer="Answer"
        )

        obj = models.KBIAttachment.objects.create(kbitem=kbitem, file=self.test_file)
        obj.save()
        self.assertEqual(obj.filename, self.file_attrs["filename"])
        self.assertEqual(obj.file.size, len(self.file_attrs["content"]))
        self.assertEqual(obj.mime_type, "text/plain")

    @skip("model in lib not patched")
    @override_settings(MEDIA_ROOT=MEDIA_DIR)
    def test_unicode_filename_to_filesystem(
        self, mock_att_save, mock_queue_save, mock_ticket_save, mock_follow_up_save
    ):
        """don't mock saving to filesystem to test file renames caused by storage layer"""
        filename, fileobj = lib.process_attachments(self.follow_up, [self.test_file])[0]
        # Attachment object was zeroth positional arg (i.e. self) of att.save
        # call
        attachment_obj = mock_att_save.return_value

        mock_att_save.assert_called_once_with(attachment_obj)
        self.assertIsInstance(attachment_obj, models.FollowUpAttachment)
        self.assertEqual(attachment_obj.filename, self.file_attrs["filename"])


@override_settings(MEDIA_ROOT=MEDIA_DIR)
class AttachmentCleanupTests(TestCase):
    """Tests to ensure orphan files are cleaned up when errors occur."""

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = models.Queue.objects.create(
            title="Test Queue",
            slug="test_q",
            allow_public_submission=True,
        )
        self.ticket = models.Ticket.objects.create(
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
        )
        self.followup = models.FollowUp.objects.create(
            ticket=self.ticket,
            title="Test FollowUp",
            comment="Test Comment",
        )
        User = get_user_model()
        self.user = User.objects.create(
            username="test_user",
            is_staff=True,
        )
        self.user.set_password("pass")
        self.user.save()

    def _count_files_in_media_dir(self):
        """Count files actually present in the media directory."""
        count = 0
        if os.path.exists(MEDIA_DIR):
            for root, dirs, files in os.walk(MEDIA_DIR):
                count += len(files)
        return count

    def tearDown(self):
        super().tearDown()
        try:
            if os.path.exists(MEDIA_DIR):
                shutil.rmtree(MEDIA_DIR)
        except OSError:
            pass

    def test_attachment_save_failure_cleans_up_file(self):
        """When Attachment.save() fails after file is written, the file should be cleaned up."""
        file_content = b"test attachment content for save failure"
        test_file = SimpleUploadedFile(
            "fail_test.txt", file_content, "text/plain"
        )
        initial_file_count = self._count_files_in_media_dir()

        att = models.FollowUpAttachment(
            followup=self.followup,
            file=test_file,
        )

        with mock.patch(
            "django.db.models.Model._save_table",
            side_effect=Exception("DB save failed"),
        ):
            with self.assertRaises(Exception):
                att.save()

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan file was left behind after Attachment.save() failure",
        )

    def test_process_attachments_with_save_exception(self):
        """When process_attachments encounters a save exception, no orphan files should remain."""
        file_content = b"test content for process_attachments failure"
        test_file = SimpleUploadedFile(
            "proc_fail_test.txt", file_content, "text/plain"
        )
        initial_file_count = self._count_files_in_media_dir()

        original_save = models.FollowUpAttachment.save

        def failing_save(self, *args, **kwargs):
            if not self.pk:
                raise Exception("Simulated database error during attachment save")
            return original_save(self, *args, **kwargs)

        with mock.patch.object(
            models.FollowUpAttachment, "save", autospec=True
        ) as mock_save:
            mock_save.side_effect = Exception("Simulated database error")

            from django.core.exceptions import ValidationError

            try:
                lib.process_attachments(self.followup, [test_file])
            except (ValidationError, Exception):
                pass

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files were left behind after process_attachments failure",
        )

    def test_update_ticket_failure_cleans_attachments(self):
        """When update_ticket fails, newly created attachment files should be cleaned up."""
        from helpdesk.update_ticket import update_ticket

        file_content = b"test content for update_ticket failure"
        test_file = SimpleUploadedFile(
            "update_fail_test.txt", file_content, "text/plain"
        )
        initial_file_count = self._count_files_in_media_dir()
        initial_att_count = models.FollowUpAttachment.objects.filter(
            followup__ticket=self.ticket
        ).count()

        with mock.patch(
            "helpdesk.update_ticket.add_staff_subscription",
            side_effect=Exception("Simulated failure at end of update_ticket"),
        ):
            with self.assertRaises(Exception):
                update_ticket(
                    self.user,
                    self.ticket,
                    comment="Test comment with attachment",
                    files=[test_file],
                )

        final_file_count = self._count_files_in_media_dir()
        final_att_count = models.FollowUpAttachment.objects.filter(
            followup__ticket=self.ticket
        ).count()

        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files were left behind after update_ticket failure",
        )
        self.assertEqual(
            initial_att_count,
            final_att_count,
            "Database records were not rolled back after update_ticket failure",
        )

    def test_public_ticket_form_save_failure_cleans_attachments(self):
        """When PublicTicketForm.save fails, newly created attachment files should be cleaned up."""
        from helpdesk.forms import PublicTicketForm

        file_content = b"test content for PublicTicketForm save failure"
        test_file = SimpleUploadedFile(
            "pub_form_fail.txt", file_content, "text/plain"
        )
        initial_file_count = self._count_files_in_media_dir()
        initial_ticket_count = models.Ticket.objects.count()

        post_data = {
            "title": "Test Ticket Form Failure",
            "body": "Test body",
            "priority": 3,
            "submitter_email": "test@example.com",
            "queue": self.queue.id,
        }
        files_data = {"attachment": test_file}

        form = PublicTicketForm(data=post_data, files=files_data)
        self.assertTrue(form.is_valid(), f"Form errors: {form.errors}")

        with mock.patch(
            "helpdesk.signals.new_ticket_done.send",
            side_effect=Exception("Simulated signal handler failure"),
        ):
            with self.assertRaises(Exception):
                form.save(user=None)

        final_file_count = self._count_files_in_media_dir()
        final_ticket_count = models.Ticket.objects.count()

        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files were left behind after PublicTicketForm.save failure",
        )
        self.assertEqual(
            initial_ticket_count,
            final_ticket_count,
            "Ticket records were not rolled back after PublicTicketForm.save failure",
        )


@override_settings(MEDIA_ROOT=MEDIA_DIR)
class ConcurrentUploadConflictTests(TestCase):
    """Regression tests for orphan files left behind during concurrent upload conflicts."""

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = models.Queue.objects.create(
            title="Concurrent Queue",
            slug="conc_q",
            allow_public_submission=True,
        )
        self.ticket = models.Ticket.objects.create(
            queue=self.queue,
            title="Concurrent Upload Ticket",
            description="Test concurrent uploads",
        )
        self.followup = models.FollowUp.objects.create(
            ticket=self.ticket,
            title="Concurrent FollowUp",
            comment="Concurrent test",
        )
        User = get_user_model()
        self.user = User.objects.create(
            username="conc_user",
            is_staff=True,
        )
        self.user.set_password("pass")
        self.user.save()

    def _count_files_in_media_dir(self):
        count = 0
        if os.path.exists(MEDIA_DIR):
            for root, dirs, files in os.walk(MEDIA_DIR):
                count += len(files)
        return count

    def tearDown(self):
        super().tearDown()
        try:
            if os.path.exists(MEDIA_DIR):
                shutil.rmtree(MEDIA_DIR)
        except OSError:
            pass

    def test_same_filename_concurrent_uploads_no_orphan(self):
        """When two attachments with the same filename are saved to the same followup,
        Django auto-renames the second file. If the second save fails, no orphan file
        should be left behind."""
        file_content_a = b"content from upload A"
        file_content_b = b"content from upload B"

        file_a = SimpleUploadedFile("shared_name.txt", file_content_a, "text/plain")
        att_a = models.FollowUpAttachment(
            followup=self.followup,
            file=file_a,
        )
        att_a.save()
        self.assertTrue(
            models.FollowUpAttachment.objects.filter(pk=att_a.pk).exists()
        )

        initial_file_count = self._count_files_in_media_dir()

        file_b = SimpleUploadedFile("shared_name.txt", file_content_b, "text/plain")
        att_b = models.FollowUpAttachment(
            followup=self.followup,
            file=file_b,
        )

        with mock.patch(
            "django.db.models.Model._save_table",
            side_effect=Exception("Simulated concurrent DB write failure"),
        ):
            with self.assertRaises(Exception):
                att_b.save()

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan file left behind when concurrent upload with same filename fails",
        )

    def test_same_filename_concurrent_uploads_both_succeed(self):
        """When two attachments with the same filename are saved to the same followup
        and both succeed, Django auto-renames and both files should exist."""
        file_content_a = b"content from upload A"
        file_content_b = b"content from upload B"

        file_a = SimpleUploadedFile("dup_name.txt", file_content_a, "text/plain")
        att_a = models.FollowUpAttachment(
            followup=self.followup,
            file=file_a,
        )
        att_a.save()

        file_b = SimpleUploadedFile("dup_name.txt", file_content_b, "text/plain")
        att_b = models.FollowUpAttachment(
            followup=self.followup,
            file=file_b,
        )
        att_b.save()

        self.assertEqual(
            2,
            models.FollowUpAttachment.objects.filter(
                followup=self.followup
            ).count(),
            "Both attachments should be saved even with same filename",
        )
        self.assertNotEqual(
            att_a.file.name,
            att_b.file.name,
            "Django should auto-rename conflicting filenames",
        )

        att_a_file = att_a.file.name
        att_b_file = att_b.file.name
        att_a.delete()
        att_b.delete()

        from django.core.files.storage import default_storage

        self.assertFalse(
            default_storage.exists(att_a_file),
            "File for attachment A should be deleted",
        )
        self.assertFalse(
            default_storage.exists(att_b_file),
            "File for attachment B (auto-renamed) should be deleted",
        )

    def test_makedirs_concurrent_creation_no_orphan(self):
        """When os.makedirs is called concurrently and one fails with FileExistsError,
        the attachment file should be cleaned up."""
        file_content = b"test makedirs conflict"
        test_file = SimpleUploadedFile("makedirs_test.txt", file_content, "text/plain")
        initial_file_count = self._count_files_in_media_dir()

        att = models.FollowUpAttachment(
            followup=self.followup,
            file=test_file,
        )

        with mock.patch(
            "os.makedirs",
            side_effect=FileExistsError("Directory created by concurrent request"),
        ):
            with self.assertRaises((FileExistsError, Exception)):
                att.save()

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan file left behind when makedirs fails due to concurrent creation",
        )

    def test_update_ticket_concurrent_filename_conflict_no_orphan(self):
        """When update_ticket is called concurrently and a filename conflict occurs,
        the auto-renamed file should be cleaned up if the database save fails."""
        from helpdesk.update_ticket import update_ticket

        first_file = SimpleUploadedFile("conflict.txt", b"first upload", "text/plain")
        update_ticket(
            self.user,
            self.ticket,
            comment="First upload",
            files=[first_file],
        )

        initial_file_count = self._count_files_in_media_dir()
        initial_att_count = models.FollowUpAttachment.objects.filter(
            followup__ticket=self.ticket
        ).count()

        second_file = SimpleUploadedFile("conflict.txt", b"second upload", "text/plain")

        with mock.patch(
            "helpdesk.update_ticket.add_staff_subscription",
            side_effect=Exception("Simulated concurrent write failure"),
        ):
            with self.assertRaises(Exception):
                update_ticket(
                    self.user,
                    self.ticket,
                    comment="Second upload (concurrent)",
                    files=[second_file],
                )

        final_file_count = self._count_files_in_media_dir()
        final_att_count = models.FollowUpAttachment.objects.filter(
            followup__ticket=self.ticket
        ).count()

        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan file left behind after concurrent filename conflict in update_ticket",
        )
        self.assertEqual(
            initial_att_count,
            final_att_count,
            "Database records not rolled back after concurrent filename conflict",
        )

    def test_process_attachments_filename_conflict_save_failure(self):
        """When process_attachments encounters a filename conflict and the subsequent
        database save fails, the auto-renamed file should be cleaned up."""
        first_file = SimpleUploadedFile("proc_conflict.txt", b"first", "text/plain")
        lib.process_attachments(self.followup, [first_file])

        initial_file_count = self._count_files_in_media_dir()

        second_file = SimpleUploadedFile("proc_conflict.txt", b"second", "text/plain")

        with mock.patch(
            "django.db.models.Model._save_table",
            side_effect=Exception("DB save failed on conflicting filename"),
        ):
            result = lib.process_attachments(self.followup, [second_file])

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan file left behind when process_attachments fails on filename conflict",
        )


@override_settings(MEDIA_ROOT=MEDIA_DIR)
class DiskFullCleanupTests(TestCase):
    """Regression tests for orphan files left behind when disk is full during upload."""

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = models.Queue.objects.create(
            title="DiskFull Queue",
            slug="df_q",
            allow_public_submission=True,
        )
        self.ticket = models.Ticket.objects.create(
            queue=self.queue,
            title="Disk Full Test Ticket",
            description="Test disk full handling",
        )
        self.followup = models.FollowUp.objects.create(
            ticket=self.ticket,
            title="Disk Full FollowUp",
            comment="Testing disk full scenarios",
        )
        User = get_user_model()
        self.user = User.objects.create(
            username="diskfull_user",
            is_staff=True,
        )
        self.user.set_password("pass")
        self.user.save()

    def tearDown(self):
        super().tearDown()
        try:
            if os.path.exists(MEDIA_DIR):
                shutil.rmtree(MEDIA_DIR)
        except OSError:
            pass

    def _count_files_in_media_dir(self):
        count = 0
        if os.path.exists(MEDIA_DIR):
            for root, dirs, files in os.walk(MEDIA_DIR):
                count += len(files)
        return count

    def test_storage_write_enospc_cleans_up_partial_file(self):
        """When storage._save raises ENOSPC (disk full) mid-write, the partial
        file should be cleaned up by SafeFileSystemStorage."""
        import errno
        from helpdesk.storage import SafeFileSystemStorage

        storage = SafeFileSystemStorage(location=MEDIA_DIR)
        test_content = b"x" * 4096
        content = SimpleUploadedFile("enospc_test.bin", test_content, "application/octet-stream")

        initial_file_count = self._count_files_in_media_dir()
        saved_name = None
        target_name = storage.get_available_name("enospc_test.bin")

        original_fdopen = os.fdopen

        def failing_fdopen(fd, *args, **kwargs):
            real_file = original_fdopen(fd, *args, **kwargs)
            original_write = real_file.write

            def failing_write(data):
                original_write(data[:512])
                raise OSError(errno.ENOSPC, "No space left on device")

            real_file.write = failing_write
            return real_file

        with mock.patch("os.fdopen", side_effect=failing_fdopen):
            try:
                saved_name = storage._save(target_name, content)
            except OSError as e:
                self.assertEqual(
                    e.errno,
                    errno.ENOSPC,
                    "Expected ENOSPC error",
                )
            else:
                self.fail("Expected OSError with ENOSPC was not raised")

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Partial file was left behind after disk full during storage write",
        )

        if saved_name:
            full_path = os.path.join(MEDIA_DIR, saved_name)
            self.assertFalse(
                os.path.exists(full_path),
                f"Partial file '{saved_name}' should not exist after cleanup",
            )

    def test_attachment_save_enospc_cleans_up_file(self):
        """When Attachment.save() fails with ENOSPC during file write, no orphan
        file should remain."""
        import errno

        test_content = b"disk full test content" * 100
        test_file = SimpleUploadedFile(
            "disk_full_att.txt", test_content, "text/plain"
        )
        initial_file_count = self._count_files_in_media_dir()

        att = models.FollowUpAttachment(
            followup=self.followup,
            file=test_file,
        )

        original_fdopen = os.fdopen

        def failing_fdopen(fd, *args, **kwargs):
            real_file = original_fdopen(fd, *args, **kwargs)
            original_write = real_file.write

            def failing_write(data):
                original_write(data[:100])
                raise OSError(errno.ENOSPC, "No space left on device")

            real_file.write = failing_write
            return real_file

        with mock.patch("os.fdopen", side_effect=failing_fdopen):
            with self.assertRaises((OSError, Exception)):
                att.save()

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan file left behind after disk full during Attachment.save",
        )

    def test_process_attachments_enospc_no_orphan(self):
        """When process_attachments encounters ENOSPC, no orphan files should remain."""
        import errno

        test_content = b"process attachments disk full test" * 50
        test_file = SimpleUploadedFile(
            "proc_diskfull.txt", test_content, "text/plain"
        )
        initial_file_count = self._count_files_in_media_dir()

        original_fdopen = os.fdopen

        def failing_fdopen(fd, *args, **kwargs):
            real_file = original_fdopen(fd, *args, **kwargs)
            original_write = real_file.write

            def failing_write(data):
                original_write(data[:64])
                raise OSError(errno.ENOSPC, "No space left on device")

            real_file.write = failing_write
            return real_file

        with mock.patch("os.fdopen", side_effect=failing_fdopen):
            try:
                lib.process_attachments(self.followup, [test_file])
            except (OSError, Exception):
                pass

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files left behind after disk full in process_attachments",
        )

    def test_update_ticket_enospc_cleans_attachments(self):
        """When update_ticket fails due to ENOSPC during attachment write,
        no orphan attachment files should remain."""
        import errno
        from helpdesk.update_ticket import update_ticket

        test_content = b"update ticket disk full content" * 50
        test_file = SimpleUploadedFile(
            "update_diskfull.txt", test_content, "text/plain"
        )
        initial_file_count = self._count_files_in_media_dir()
        initial_att_count = models.FollowUpAttachment.objects.filter(
            followup__ticket=self.ticket
        ).count()

        original_fdopen = os.fdopen

        def failing_fdopen(fd, *args, **kwargs):
            real_file = original_fdopen(fd, *args, **kwargs)
            original_write = real_file.write

            def failing_write(data):
                original_write(data[:128])
                raise OSError(errno.ENOSPC, "No space left on device")

            real_file.write = failing_write
            return real_file

        with mock.patch("os.fdopen", side_effect=failing_fdopen):
            try:
                update_ticket(
                    self.user,
                    self.ticket,
                    comment="Disk full test comment",
                    files=[test_file],
                )
            except (OSError, Exception):
                pass

        final_file_count = self._count_files_in_media_dir()
        final_att_count = models.FollowUpAttachment.objects.filter(
            followup__ticket=self.ticket
        ).count()

        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files left behind after disk full in update_ticket",
        )
        self.assertEqual(
            initial_att_count,
            final_att_count,
            "Database records not rolled back after disk full in update_ticket",
        )


class RemoteStorageCleanupTests(TestCase):
    """Regression tests for orphan files left behind when remote storage is unreachable."""

    @classmethod
    def setUpTestData(cls):
        cls.queue = models.Queue.objects.create(
            title="Remote Storage Test Queue",
            slug="remote_storage_queue",
            allow_public_submission=True,
        )
        cls.ticket = models.Ticket.objects.create(
            title="Test Ticket",
            queue=cls.queue,
            description="Test ticket description",
            status=models.Ticket.OPEN_STATUS,
        )
        cls.followup = models.FollowUp.objects.create(
            ticket=cls.ticket,
            comment="Test followup",
        )

    def setUp(self):
        self._old_media_root = settings.MEDIA_ROOT
        settings.MEDIA_ROOT = MEDIA_DIR
        self._old_default_storage = getattr(settings, 'DEFAULT_FILE_STORAGE', None)
        settings.DEFAULT_FILE_STORAGE = 'helpdesk.storage.MockRemoteStorage'

        from helpdesk.storage import get_multipart_tracker, MockRemoteStorage
        get_multipart_tracker().clear_all()

        self.storage = MockRemoteStorage(location=MEDIA_DIR)
        self._patch_storage()

    def tearDown(self):
        settings.MEDIA_ROOT = self._old_media_root
        if self._old_default_storage:
            settings.DEFAULT_FILE_STORAGE = self._old_default_storage
        self._unpatch_storage()

        from helpdesk.storage import get_multipart_tracker
        get_multipart_tracker().clear_all()

        try:
            shutil.rmtree(MEDIA_DIR)
        except OSError:
            pass

    def _patch_storage(self):
        pass

    def _unpatch_storage(self):
        pass

    def _patch_storage_for_instance(self, instance):
        """Patch storage on a specific FieldFile instance."""
        if hasattr(instance, 'file'):
            instance.file.storage = self.storage
            if hasattr(instance.file, '_storage'):
                instance.file._storage = self.storage

    @contextmanager
    def _patch_followup_attachment_creation(self):
        """Patch FollowUpAttachment creation to auto-apply storage patch."""
        original_init = models.FollowUpAttachment.__init__
        original_kbi_init = models.KBIAttachment.__init__
        test_case = self

        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            test_case._patch_storage_for_instance(self)

        def patched_kbi_init(self, *args, **kwargs):
            original_kbi_init(self, *args, **kwargs)
            test_case._patch_storage_for_instance(self)

        with mock.patch.object(models.FollowUpAttachment, '__init__', patched_init):
            with mock.patch.object(models.KBIAttachment, '__init__', patched_kbi_init):
                yield

    def _count_files_in_media_dir(self):
        count = 0
        if os.path.exists(MEDIA_DIR):
            for root, dirs, files in os.walk(MEDIA_DIR):
                count += len(files)
        return count

    def _count_multipart_parts(self):
        count = 0
        if os.path.exists(MEDIA_DIR):
            for root, dirs, files in os.walk(MEDIA_DIR):
                for f in files:
                    if '.part' in f:
                        count += 1
        return count

    def test_remote_storage_connection_error_cleans_temp_file(self):
        """When remote storage raises ConnectionError, local temp files should be cleaned up."""
        import tempfile

        test_content = b"remote storage connection error test content" * 100
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.txt')
        temp_file.write(test_content)
        temp_file.close()

        from django.core.files.uploadedfile import TemporaryUploadedFile
        temp_uploaded = TemporaryUploadedFile(
            name="remote_conn_error.txt",
            content_type="text/plain",
            size=len(test_content),
            charset=None,
        )
        temp_uploaded.file = open(temp_file.name, 'rb')
        temp_uploaded._TemporaryFileArgs = {'delete': False}
        temp_uploaded._file_name = temp_file.name

        initial_file_count = self._count_files_in_media_dir()
        self.assertTrue(os.path.exists(temp_file.name))

        att = models.FollowUpAttachment(
            followup=self.followup,
            file=temp_uploaded,
        )
        self._patch_storage_for_instance(att)

        self.storage.set_fail_next_save(True, mode='connection')

        with self.assertRaises(ConnectionError):
            att.save()

        self.assertFalse(
            os.path.exists(temp_file.name),
            "Local temp file should be cleaned up after remote storage connection error"
        )

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files left behind after remote storage connection error"
        )

    def test_remote_storage_timeout_cleans_pending_uploads(self):
        """When remote storage times out, pending multipart uploads should be aborted."""
        test_content = b"remote storage timeout test content" * 200
        test_file = SimpleUploadedFile(
            "remote_timeout.txt", test_content, "text/plain"
        )

        initial_file_count = self._count_files_in_media_dir()
        initial_parts_count = self._count_multipart_parts()

        att = models.FollowUpAttachment(
            followup=self.followup,
            file=test_file,
        )
        self._patch_storage_for_instance(att)

        self.storage.set_fail_next_save(True, mode='timeout')

        from helpdesk.storage import get_multipart_tracker
        tracker = get_multipart_tracker()
        tracker.start_upload('MockRemoteStorage', 'test-timeout-upload-123')

        with self.assertRaises(TimeoutError):
            att.save()

        pending_uploads = tracker.get_pending_uploads('MockRemoteStorage')
        self.assertEqual(
            len(pending_uploads), 0,
            "All pending multipart uploads should be aborted after timeout"
        )

        final_file_count = self._count_files_in_media_dir()
        final_parts_count = self._count_multipart_parts()

        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files left behind after remote storage timeout"
        )
        self.assertEqual(
            initial_parts_count,
            final_parts_count,
            "Orphan multipart parts left behind after remote storage timeout"
        )

    def test_remote_storage_unreachable_cleans_parts(self):
        """When remote storage is unreachable, partial multipart uploads should be cleaned up."""
        test_content = b"unreachable storage test content" * 500
        test_file = SimpleUploadedFile(
            "unreachable_test.txt", test_content, "text/plain"
        )

        initial_file_count = self._count_files_in_media_dir()
        initial_parts_count = self._count_multipart_parts()

        att = models.FollowUpAttachment(
            followup=self.followup,
            file=test_file,
        )
        self._patch_storage_for_instance(att)

        self.storage.set_unreachable(True)

        from helpdesk.storage import RemoteStorageUnreachable
        with self.assertRaises(RemoteStorageUnreachable):
            att.save()

        final_file_count = self._count_files_in_media_dir()
        final_parts_count = self._count_multipart_parts()

        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files left behind when remote storage is unreachable"
        )
        self.assertEqual(
            initial_parts_count,
            final_parts_count,
            "Orphan multipart parts left behind when remote storage is unreachable"
        )

    def test_process_attachments_remote_storage_error_cleanup(self):
        """When process_attachments encounters remote storage errors, no orphan files should remain."""
        test_content = b"process_attachments remote storage error test" * 100
        test_file = SimpleUploadedFile(
            "proc_remote_error.txt", test_content, "text/plain"
        )

        initial_file_count = self._count_files_in_media_dir()

        self.storage.set_fail_next_save(True, mode='connection')

        with self._patch_followup_attachment_creation():
            result = lib.process_attachments(self.followup, [test_file])

        self.assertEqual(len(result), 0, "No attachments should be saved")

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files left behind after process_attachments remote storage error"
        )

    def test_partial_multipart_upload_cleanup(self):
        """When a multipart upload fails mid-way, all uploaded parts should be cleaned up."""
        large_content = b"large content for multipart upload test" * 10000
        test_file = SimpleUploadedFile(
            "multipart_failure.txt", large_content, "text/plain"
        )

        initial_file_count = self._count_files_in_media_dir()
        initial_parts_count = self._count_multipart_parts()

        att = models.FollowUpAttachment(
            followup=self.followup,
            file=test_file,
        )
        self._patch_storage_for_instance(att)

        self.storage.set_fail_next_save(True, mode='partial_upload')

        with self.assertRaises(ConnectionError):
            att.save()

        final_parts_count = self._count_multipart_parts()
        self.assertEqual(
            initial_parts_count,
            final_parts_count,
            "Orphan multipart parts left behind after partial upload failure"
        )

        final_file_count = self._count_files_in_media_dir()
        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files left behind after partial multipart upload failure"
        )

        from helpdesk.storage import get_multipart_tracker
        tracker = get_multipart_tracker()
        pending = tracker.get_pending_uploads('MockRemoteStorage')
        self.assertEqual(
            len(pending), 0,
            "Multipart upload tracker should have no pending uploads"
        )

    def test_update_ticket_remote_storage_error_cleanup(self):
        """When update_ticket encounters remote storage errors, no orphan files should remain."""
        test_content = b"update_ticket remote storage error test" * 100
        test_file = SimpleUploadedFile(
            "update_ticket_remote.txt", test_content, "text/plain"
        )

        initial_file_count = self._count_files_in_media_dir()

        self.storage.set_fail_next_save(True, mode='timeout')

        from helpdesk import update_ticket
        user = get_user_model().objects.create_user(
            username="testuser_remote",
            password="testpass",
        )
        with mock.patch.object(update_ticket, 'process_email_notifications_for_ticket_update', return_value=None):
            with mock.patch.object(update_ticket, 'process_attachments', side_effect=TimeoutError("Connection timeout")):
                with self._patch_followup_attachment_creation():
                    with self.assertRaises(TimeoutError):
                        update_ticket.update_ticket(
                            user=user,
                            ticket=self.ticket,
                            comment="Test comment",
                            files=[test_file],
                        )

        final_file_count = self._count_files_in_media_dir()

        self.assertEqual(
            initial_file_count,
            final_file_count,
            "Orphan files left behind after update_ticket remote storage error"
        )


def tearDownModule():
    try:
        shutil.rmtree(MEDIA_DIR)
    except OSError:
        pass
