import io
import os
import tempfile
import shutil
from unittest.mock import patch, MagicMock

from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings

from helpdesk.management.commands.reset_demo_data import Command as ResetCommand
from helpdesk.models import Queue, Ticket


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_FIXTURE = os.path.join(PROJECT_ROOT, "demodesk", "fixtures", "demo.json")


class ResetDemoDataEnvironmentSafetyTestCase(TestCase):
    """Test the environment safety guards of reset_demo_data command."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._temp_media_root = tempfile.mkdtemp()
        cls._media_patcher = override_settings(
            MEDIA_ROOT=cls._temp_media_root,
        )
        cls._media_patcher.enable()
        cls._demo_env_patcher = patch.dict(os.environ, {"HELPDESK_DEMO_MODE": "1"})
        cls._demo_env_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._demo_env_patcher.stop()
        cls._media_patcher.disable()
        if os.path.exists(cls._temp_media_root):
            shutil.rmtree(cls._temp_media_root)
        super().tearDownClass()

    def _call_with_fixture(self, *args, **kwargs):
        call_args = list(args) + ["--fixture", DEMO_FIXTURE]
        return call_command("reset_demo_data", *call_args, **kwargs)

    def _simulate_production_env(self):
        """
        Create override_settings context that makes the environment appear as
        production (DEBUG=False, production-like DB name, different SECRET_KEY).
        Also explicitly unsets HELPDESK_DEMO_MODE for this context.
        """
        demo_unset = patch.dict(os.environ, {}, clear=True)

        class CombinedPatcher:
            def __init__(self, settings_patcher, env_patcher):
                self.settings_patcher = settings_patcher
                self.env_patcher = env_patcher
                self._saved_django_settings = os.environ.get(
                    "DJANGO_SETTINGS_MODULE", ""
                )

            def __enter__(self):
                self._saved_django_settings = os.environ.get(
                    "DJANGO_SETTINGS_MODULE", ""
                )
                self.settings_patcher.__enter__()
                result = self.env_patcher.__enter__()
                if self._saved_django_settings:
                    os.environ["DJANGO_SETTINGS_MODULE"] = self._saved_django_settings
                return result

            def __exit__(self, exc_type, exc_val, exc_tb):
                result = self.env_patcher.__exit__(exc_type, exc_val, exc_tb)
                self.settings_patcher.__exit__(exc_type, exc_val, exc_tb)
                return result

        settings_ctx = override_settings(
            DEBUG=False,
            SECRET_KEY="a-real-production-secret-key-that-is-not-the-demo-one",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": "production_db.sqlite3",
                }
            },
        )
        return CombinedPatcher(settings_ctx, demo_unset)

    def test_non_demo_environment_blocked_without_force(self):
        """
        Verify that in a non-demo/production environment, the command is
        immediately rejected with a CommandError if --force is not provided.
        """
        with self._simulate_production_env():
            with self.assertRaises(CommandError) as cm:
                self._call_with_fixture("--noinput", verbosity=0)

            error_msg = str(cm.exception)
            self.assertIn(
                "demo/debug environments",
                error_msg.lower(),
                "Error should mention demo/debug environment restriction",
            )
            self.assertIn(
                "production",
                error_msg.lower(),
                "Error should mention production environment",
            )
            self.assertIn(
                "--force",
                error_msg,
                "Error should mention --force flag as bypass",
            )

    def test_non_demo_environment_blocked_without_force_has_data_safety_warning(self):
        """
        Verify that the production block error message warns about data deletion.
        """
        with self._simulate_production_env():
            with self.assertRaises(CommandError) as cm:
                self._call_with_fixture("--noinput", verbosity=0)

            error_msg = str(cm.exception)
            self.assertIn(
                "PERMANENTLY DELETE",
                error_msg,
                "Error should warn about permanent data deletion",
            )
            self.assertIn(
                "tickets",
                error_msg.lower(),
                "Error should mention tickets as affected data",
            )
            self.assertIn(
                "queues",
                error_msg.lower(),
                "Error should mention queues as affected data",
            )

    def test_non_demo_with_force_first_confirmation_wrong_phrase_cancels(self):
        """
        Verify that with --force in production, giving the wrong first
        confirmation phrase correctly cancels the operation.
        """
        wrong_inputs = [
            "wrong phrase",
            "",
            "i understand i am deleting all production data",
            "I UNDERSTAND",
        ]

        for wrong in wrong_inputs:
            with self.subTest(wrong_phrase=wrong):
                with self._simulate_production_env():
                    with patch("builtins.input", return_value=wrong):
                        stdout = io.StringIO()
                        try:
                            self._call_with_fixture(
                                "--force",
                                verbosity=0,
                                stdout=stdout,
                            )
                        except SystemExit:
                            pass

                        output = stdout.getvalue()
                        self.assertIn(
                            "cancelled",
                            output.lower(),
                            f"Wrong phrase '{wrong}' should cancel operation",
                        )

                        self.assertEqual(
                            Ticket.objects.count(),
                            0,
                            "No data should have been modified after cancellation",
                        )

    def test_non_demo_with_force_second_confirmation_wrong_cancels(self):
        """
        Verify that with --force in production, passing first confirmation
        but failing second confirmation cancels correctly.
        """
        first_correct = "I UNDERSTAND I AM DELETING ALL PRODUCTION DATA"
        wrong_second = [
            "no",
            "",
            "destroy",
            "destroy everything",
        ]

        for wrong in wrong_second:
            with self.subTest(wrong_second=wrong):
                with self._simulate_production_env():
                    inputs = iter([first_correct, wrong])
                    with patch("builtins.input", lambda _="": next(inputs)):
                        stdout = io.StringIO()
                        stderr = io.StringIO()
                        try:
                            self._call_with_fixture(
                                "--force",
                                verbosity=0,
                                stdout=stdout,
                                stderr=stderr,
                            )
                        except SystemExit:
                            pass

                        output = stdout.getvalue()
                        self.assertIn(
                            "cancelled",
                            output.lower(),
                            f"Wrong 2nd confirm '{wrong}' should cancel operation",
                        )
                        self.assertEqual(
                            Ticket.objects.count(),
                            0,
                            "No data should have been modified",
                        )

    def test_non_demo_with_force_correct_confirmations_succeeds(self):
        """
        Verify that with --force in production, passing BOTH confirmations
        correctly allows the operation to proceed.
        """
        first_correct = "I UNDERSTAND I AM DELETING ALL PRODUCTION DATA"
        second_correct = "DESTROY EVERYTHING"

        Queue.objects.create(title="Prod Queue", slug="prodq")
        Ticket.objects.create(
            title="Real Production Ticket",
            queue=Queue.objects.get(slug="prodq"),
        )
        self.assertGreater(Ticket.objects.count(), 0, "Precondition: data exists")

        with self._simulate_production_env():
            inputs = iter([first_correct, second_correct])
            with patch("builtins.input", lambda _="": next(inputs)):
                stdout = io.StringIO()
                stderr = io.StringIO()
                self._call_with_fixture(
                    "--force",
                    verbosity=0,
                    stdout=stdout,
                    stderr=stderr,
                )

                output = stdout.getvalue()
                self.assertIn(
                    "completed successfully",
                    output.lower(),
                    "Reset should succeed with both correct confirmations",
                )

        expected_titles = {
            "Some django-helpdesk Problem",
            "Something else",
            "Something with an attachment",
        }
        actual_titles = set(Ticket.objects.values_list("title", flat=True))
        self.assertEqual(
            actual_titles,
            expected_titles,
            "Production data should be replaced with demo data",
        )
        self.assertFalse(
            Ticket.objects.filter(title="Real Production Ticket").exists(),
            "Production ticket should be removed",
        )

    def test_non_demo_with_force_shows_warning_banner(self):
        """
        Verify that the production --force warning banner is displayed.
        """
        first_correct = "I UNDERSTAND I AM DELETING ALL PRODUCTION DATA"
        second_correct = "DESTROY EVERYTHING"

        with self._simulate_production_env():
            inputs = iter([first_correct, second_correct])
            with patch("builtins.input", lambda _="": next(inputs)):
                stdout = io.StringIO()
                stderr = io.StringIO()
                self._call_with_fixture(
                    "--force",
                    verbosity=0,
                    stdout=stdout,
                    stderr=stderr,
                )

                err_output = stderr.getvalue()
                self.assertIn(
                    "WARNING",
                    err_output,
                    "Warning banner should be displayed",
                )
                self.assertIn(
                    "PERMANENTLY DESTROY",
                    err_output,
                    "Warning should mention permanent destruction",
                )
                self.assertIn(
                    "CANNOT BE UNDONE",
                    err_output,
                    "Warning should mention irreversibility",
                )
                self.assertIn(
                    "backup",
                    err_output.lower(),
                    "Warning should mention backup",
                )

    def test_demo_environment_allows_reset(self):
        """
        Verify that in a demo environment (DEBUG=True), the command proceeds
        normally after a single 'yes' confirmation.
        """
        Queue.objects.create(title="Extra Demo Queue", slug="xdq")
        Ticket.objects.create(
            title="Extra Ticket That Should Be Cleared",
            queue=Queue.objects.get(slug="xdq"),
        )
        self.assertGreater(Queue.objects.count(), 0, "Precondition: queues exist")

        with patch("builtins.input", return_value="yes"):
            stdout = io.StringIO()
            self._call_with_fixture(verbosity=0, stdout=stdout)

            output = stdout.getvalue()
            self.assertIn(
                "completed successfully",
                output.lower(),
                "Reset should succeed in demo env with yes confirmation",
            )

        expected_slugs = {"DH", "SP"}
        actual_slugs = set(Queue.objects.values_list("slug", flat=True))
        self.assertEqual(
            actual_slugs,
            expected_slugs,
            "Only demo queues should exist after reset",
        )

    def test_demo_environment_noinput_works(self):
        """
        Verify that in a demo environment, --noinput skips the confirmation
        prompt and executes normally.
        """
        Queue.objects.create(title="Temp Q", slug="tmpq")

        stdout = io.StringIO()
        self._call_with_fixture("--noinput", verbosity=0, stdout=stdout)

        output = stdout.getvalue()
        self.assertIn(
            "completed successfully",
            output.lower(),
            "--noinput should work in demo environment",
        )
        self.assertEqual(
            Queue.objects.count(),
            2,
            "Data should be reset to demo state",
        )

    def test_demo_environment_cancels_on_no(self):
        """
        Verify that in a demo environment, answering 'no' to the confirmation
        cancels the operation without modifying any data.
        """
        test_queue = Queue.objects.create(title="Persisted Queue", slug="persist")

        with patch("builtins.input", return_value="no"):
            stdout = io.StringIO()
            self._call_with_fixture(verbosity=0, stdout=stdout)

            output = stdout.getvalue()
            self.assertIn(
                "cancelled",
                output.lower(),
                "Answering no should cancel operation",
            )

        self.assertTrue(
            Queue.objects.filter(slug="persist").exists(),
            "Data should remain unchanged after cancellation",
        )
        self.assertEqual(
            Queue.objects.first().pk,
            test_queue.pk,
            "Original queue should still be present",
        )

    def test_noinput_ignored_in_production_with_force(self):
        """
        Verify that --noinput does NOT skip confirmations in production
        environments even with --force. The user must still pass both
        confirmation prompts manually.
        """
        with self._simulate_production_env():
            inputs = iter(
                [
                    "I UNDERSTAND I AM DELETING ALL PRODUCTION DATA",
                    "DESTROY EVERYTHING",
                ]
            )
            mock_input = MagicMock(side_effect=lambda _="": next(inputs))
            with patch("builtins.input", mock_input):
                stdout = io.StringIO()
                stderr = io.StringIO()
                self._call_with_fixture(
                    "--force",
                    "--noinput",
                    verbosity=0,
                    stdout=stdout,
                    stderr=stderr,
                )

                self.assertGreaterEqual(
                    mock_input.call_count,
                    2,
                    "--noinput should NOT skip force confirmations in production",
                )

    def test_is_demo_environment_detects_debug_true(self):
        """Verify _is_demo_environment returns True when DEBUG=True."""
        with override_settings(DEBUG=True):
            cmd = ResetCommand()
            self.assertTrue(
                cmd._is_demo_environment(),
                "DEBUG=True should be detected as demo environment",
            )

    def test_is_demo_environment_detects_production(self):
        """Verify _is_demo_environment returns False in production-like setup."""
        with self._simulate_production_env():
            cmd = ResetCommand()
            self.assertFalse(
                cmd._is_demo_environment(),
                "Should NOT be detected as demo env in production-like config",
            )

    def test_is_demo_environment_detects_env_variable(self):
        """Verify HELPDESK_DEMO_MODE env var triggers demo detection."""
        with self._simulate_production_env():
            with patch.dict(os.environ, {"HELPDESK_DEMO_MODE": "true"}):
                cmd = ResetCommand()
                self.assertTrue(
                    cmd._is_demo_environment(),
                    "HELPDESK_DEMO_MODE=true should be detected as demo environment",
                )

    def test_is_demo_environment_detects_demo_database_name(self):
        """Verify database name containing 'demo' triggers demo detection."""
        with override_settings(
            DEBUG=False,
            SECRET_KEY="not-demo-key",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": "/data/demo_project/db.sqlite3",
                }
            },
        ):
            cmd = ResetCommand()
            self.assertTrue(
                cmd._is_demo_environment(),
                "DB name containing 'demo' should be detected as demo environment",
            )

    def test_is_demo_environment_detects_demo_secret_key(self):
        """Verify the demo project's default SECRET_KEY triggers demo detection."""
        demo_secret = "_crkn1+fnzu5$vns_-d+^ayiq%z4k*s!!ag0!mfy36(y!vrazd"
        with override_settings(
            DEBUG=False,
            SECRET_KEY=demo_secret,
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": "some_prod_db.sqlite3",
                }
            },
        ):
            cmd = ResetCommand()
            self.assertTrue(
                cmd._is_demo_environment(),
                "Demo default SECRET_KEY should be detected as demo environment",
            )

    def test_force_in_production_with_correct_confirmations_and_keep_users(self):
        """
        Verify that --force works correctly in production alongside other
        flags like --keep-users, respecting both the safety checks and the
        data preservation options.
        """
        from django.contrib.auth import get_user_model

        User = get_user_model()

        first_correct = "I UNDERSTAND I AM DELETING ALL PRODUCTION DATA"
        second_correct = "DESTROY EVERYTHING"

        with self._simulate_production_env():
            production_user = User.objects.create_user(
                username="production_user",
                password="testpass",
                email="prod@example.com",
            )
            user_pk = production_user.pk
            self.assertIsNotNone(user_pk)

            inputs = iter([first_correct, second_correct])
            with patch("builtins.input", lambda _="": next(inputs)):
                stdout = io.StringIO()
                stderr = io.StringIO()
                self._call_with_fixture(
                    "--force",
                    "--keep-users",
                    verbosity=0,
                    stdout=stdout,
                    stderr=stderr,
                )

                all_users = list(User.objects.values_list("username", flat=True))

                self.assertTrue(
                    User.objects.filter(pk=user_pk).exists(),
                    f"--keep-users should preserve users even with --force. "
                    f"Existing users: {all_users}",
                )
                self.assertTrue(
                    User.objects.filter(username="production_user").exists(),
                    f"production_user should exist. Existing users: {all_users}",
                )
                self.assertTrue(
                    User.objects.filter(username="admin").exists(),
                    f"Admin from fixture should also be present. "
                    f"Existing users: {all_users}",
                )
