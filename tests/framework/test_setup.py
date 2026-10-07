"""Setup regression checks without Docker, live credentials or a database."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("shop_setup", ROOT / "scripts/setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env.demo"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def configure(self):
        with contextlib.redirect_stdout(io.StringIO()):
            setup.configure(self.path, False, False)

    def local_config(self):
        return {"services": {
            "web": {"environment": {"SECRET_KEY": "django-secret", "JWT_SECRET": "jwt-secret",
                    "POSTGRES_PASSWORD": "db-password", "PUBLIC_URL": "http://localhost:8080",
                    "DEMO_OWNER_PASSWORD": "demo-password-long"}},
            "db": {"environment": {"POSTGRES_PASSWORD": "db-password"}},
            "nginx": {"ports": [{"host_ip": "127.0.0.1", "published": "8080"}]},
        }}

    def production_config(self):
        config = self.local_config()
        config["services"]["web"]["environment"].update(
            PUBLIC_URL="https://cafe.example.org", ALLOWED_HOSTS="cafe.example.org",
            DEBUG="false", OPENAI_API_KEY="test-only")
        config["services"]["nginx"].update(
            environment={"NGINX_SERVER_NAME": "cafe.example.org", "TLS_CERT_NAME": "cafe.example.org"},
            volumes=[{"source": self.directory.name, "target": "/etc/letsencrypt"}])
        return config

    def test_generates_distinct_private_secrets_and_preserves_file_on_rerun(self):
        self.configure()
        original = self.path.read_bytes()
        values = dict(line.split("=", 1) for line in original.decode().splitlines()
                      if line and not line.startswith("#") and "=" in line)
        self.assertEqual(len({values[key] for key in setup.SECRET_KEYS}), 3)
        self.assertNotIn("change-me", "".join(values[key] for key in setup.SECRET_KEYS))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        os.environ["OPENAI_API_KEY"] = "different-key"
        self.configure()
        self.assertEqual(self.path.read_bytes(), original)

    def test_short_password_does_not_leave_partial_configuration(self):
        os.environ["DEMO_OWNER_PASSWORD"] = "short"
        with self.assertRaisesRegex(ValueError, "12 characters"):
            self.configure()
        self.assertFalse(self.path.exists())

    def test_secret_with_interpolation_characters_remains_literal(self):
        os.environ["OPENAI_API_KEY"] = "test-$NOT_A_VARIABLE#literal"
        self.configure()
        self.assertIn("OPENAI_API_KEY='test-$NOT_A_VARIABLE#literal'", self.path.read_text())

    def test_environment_does_not_override_selected_file_or_leak_compose_profiles(self):
        self.configure()
        os.environ.update(POSTGRES_PASSWORD="unrelated", PUBLIC_URL="https://unrelated.example",
                          COMPOSE_FILE="other.yml", COMPOSE_PROFILES="production", APP_ENV_FILE=".env")
        env = setup.environment(self.path, "test-demo")
        self.assertNotIn("POSTGRES_PASSWORD", env)
        self.assertNotIn("PUBLIC_URL", env)
        self.assertNotIn("COMPOSE_FILE", env)
        self.assertNotIn("COMPOSE_PROFILES", env)
        self.assertEqual(env["APP_ENV_FILE"], str(self.path))
        self.assertEqual(env["COMPOSE_PROJECT_NAME"], "test-demo")
        self.assertEqual(env["APP_IMAGE"], "test-demo:local")

    def test_production_needs_explicit_domain_before_creating_config(self):
        with self.assertRaisesRegex(ValueError, "domain"):
            setup.configure(self.path, True, False)
        self.assertFalse(self.path.exists())

    def test_rejects_publicly_exposed_local_evaluation(self):
        config = self.local_config()
        config["services"]["nginx"]["ports"][0]["host_ip"] = "0.0.0.0"
        with self.assertRaisesRegex(ValueError, "HTTP_BIND"):
            setup.validate(config, False)

    def test_rejects_placeholder_secrets_before_starting(self):
        config = self.local_config()
        config["services"]["web"]["environment"]["SECRET_KEY"] = "change-me-secret"
        with self.assertRaisesRegex(ValueError, "SECRET_KEY"):
            setup.validate(config, False)

    def test_production_requires_certificates_and_disables_evaluation(self):
        config = self.production_config()
        with self.assertRaisesRegex(ValueError, "missing fullchain.pem"):
            setup.validate(config, True)
        cert_dir = Path(self.directory.name) / "live/cafe.example.org"
        cert_dir.mkdir(parents=True)
        for name in ("fullchain.pem", "privkey.pem"):
            (cert_dir / name).write_text("test fixture")
        setup.validate(config, True)
        config["services"]["web"]["environment"]["DJANGO_SETTINGS_MODULE"] = "evaluate.integration.settings"
        with self.assertRaisesRegex(ValueError, "evaluation disabled"):
            setup.validate(config, True)

    def test_compose_error_does_not_print_expanded_secrets(self):
        self.configure()
        stack = setup.Stack(self.path, "test-demo", False)
        with patch.object(stack, "run", return_value=subprocess.CompletedProcess([], 1, "", "private-secret")):
            with self.assertRaises(ValueError) as error:
                stack.configuration()
        self.assertNotIn("private-secret", str(error.exception))

    def test_failed_start_prevents_seed_and_smoke_commands(self):
        self.configure()
        stack = setup.Stack(self.path, "test-demo", False)
        with patch.object(stack, "run", side_effect=subprocess.CalledProcessError(1, ["docker"])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                setup.start_local(stack, self.local_config()["services"]["web"]["environment"])
        self.assertEqual(run.call_count, 1)

    def test_failed_migration_prevents_production_start(self):
        self.configure()
        stack = setup.Stack(self.path, "test-production", True)
        def command(*args, **kwargs):
            if args[0] == "run":
                raise subprocess.CalledProcessError(1, ["docker", "run"])
        with patch.object(stack, "run", side_effect=command), patch.object(setup.subprocess, "run") as launch:
            with self.assertRaises(subprocess.CalledProcessError):
                setup.deploy(stack)
        launch.assert_not_called()

    def test_demo_requires_explicit_model_call_opt_in_before_setup(self):
        with patch.object(setup, "configure") as configure, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit:
                setup.main(["demo"])
        self.assertEqual(exit.exception.code, 2)
        configure.assert_not_called()

    def test_failed_preflight_never_sends_live_chat(self):
        self.configure()
        stack = setup.Stack(self.path, "test-demo", False)
        with patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "{}", "")) as run:
            with patch.object(setup.time, "monotonic", side_effect=[0, 121]):
                with self.assertRaisesRegex(ValueError, "no live chat"):
                    setup.run_demo(stack, "evaluate/configs/smoke.json")
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("--allow-live-chat", run.call_args.args[0])

    def test_demo_waits_for_readiness_and_reuses_selected_stack_for_report(self):
        self.configure()
        stack = setup.Stack(self.path, "test-demo", False)
        responses = [subprocess.CompletedProcess([], 2, "{}", ""),
                     subprocess.CompletedProcess([], 0, "{}", ""),
                     subprocess.CompletedProcess([], 0)]
        with patch.object(setup.subprocess, "run", side_effect=responses) as run:
            with patch.object(setup.time, "sleep"):
                setup.run_demo(stack, "evaluate/configs/smoke.json")
        calls = run.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIn("preflight", calls[0].args[0])
        self.assertIn("preflight", calls[1].args[0])
        self.assertIn("--allow-live-chat", calls[2].args[0])
        self.assertIn("--report", calls[2].args[0])
        for call in calls:
            self.assertEqual(call.kwargs["env"]["APP_ENV_FILE"], str(self.path))
            self.assertEqual(call.kwargs["env"]["COMPOSE_PROJECT_NAME"], "test-demo")

    def test_production_migrates_before_gate_without_seeding_demo(self):
        self.configure()
        stack = setup.Stack(self.path, "test-production", True)
        operations = []
        def compose(*args, **kwargs):
            operations.append(args)
        def launch(args, **kwargs):
            operations.append(tuple(args))
            self.assertEqual(kwargs["env"]["COMPOSE_PROJECT_NAME"], "test-production")
        with patch.object(stack, "run", side_effect=compose):
            with patch.object(setup.subprocess, "run", side_effect=launch):
                with contextlib.redirect_stdout(io.StringIO()):
                    setup.deploy(stack)
        migration = next(i for i, command in enumerate(operations) if command[0] == "run")
        self.assertEqual(operations[migration], ("run", "--rm", "--no-deps", "init"))
        self.assertEqual(operations[migration - 1][0], "stop")
        self.assertIn("start_production.sh", operations[migration + 1][1])
        self.assertFalse(any("seed_cafe_demo" in command for command in operations))


if __name__ == "__main__":
    unittest.main()
