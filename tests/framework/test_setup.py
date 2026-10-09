"""Setup regression checks without Docker, live credentials or a database."""
import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

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
        self.log_path = patch.object(setup, "LOG_PATH", None)
        self.log_path.start()
        self.addCleanup(self.log_path.stop)

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
                setup.deploy(stack, self.production_config())
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
        with patch.object(stack, "run", side_effect=compose), patch.object(setup, "verify_https") as verify:
            with patch.object(setup.subprocess, "run", side_effect=launch):
                with contextlib.redirect_stdout(io.StringIO()):
                    setup.deploy(stack, self.production_config())
        verify.assert_called_once()
        migration = next(i for i, command in enumerate(operations) if command[0] == "run")
        self.assertEqual(operations[migration], ("run", "--rm", "--no-deps", "init"))
        self.assertEqual(operations[migration - 1][0], "stop")
        self.assertEqual(operations[migration + 1], ("rm", "-f", "-s", "nginx"))
        self.assertIn("start_production.sh", operations[migration + 2][1])
        self.assertFalse(any("seed_cafe_demo" in command for command in operations))

    def test_https_failure_never_reports_deployment_success(self):
        self.configure()
        stack = setup.Stack(self.path, "test-production", True)
        output = io.StringIO()
        with patch.object(stack, "run"), patch.object(setup.subprocess, "run"), \
                patch.object(setup, "verify_https", side_effect=ValueError("Local HTTPS failed")), \
                contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, "Local HTTPS"):
                setup.deploy(stack, self.production_config())
        self.assertNotIn("Production HTTPS is ready", output.getvalue())
        self.assertNotIn("createsuperuser", output.getvalue())

    def test_read_only_check_logs_without_deploying_or_exposing_secrets(self):
        self.configure()
        original = self.path.read_bytes()
        log = Path(self.directory.name) / "setup.log"
        with patch.object(setup, "check_docker"), \
                patch.object(setup.Stack, "configuration", return_value=self.local_config()), \
                patch.object(setup, "check_ports"), patch.object(setup, "start_local") as start:
            self.assertEqual(setup.main(["local", "--check", "--env-file", str(self.path),
                                         "--log-file", str(log)]), 0)
        start.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn("Preflight finished", log.read_text())
        self.assertNotIn("django-secret", log.read_text())
        self.assertNotIn("db-password", log.read_text())

    def test_preflight_failure_never_builds_or_stops_services(self):
        self.configure()
        with patch.object(setup, "check_docker"), \
                patch.object(setup.Stack, "configuration", return_value=self.production_config()), \
                patch.object(setup, "preflight", side_effect=ValueError("Port conflict")), \
                patch.object(setup, "deploy") as deploy:
            with self.assertRaisesRegex(ValueError, "Port conflict"):
                setup.main(["production", "--env-file", str(self.path), "--no-input",
                            "--log-file", str(Path(self.directory.name) / "setup.log")])
        deploy.assert_not_called()

    def test_environment_clears_https_overrides_even_when_absent_from_existing_file(self):
        self.configure()
        self.path.write_text("SECRET_KEY='existing'\n")
        os.environ.update(HTTPS_BIND="127.0.0.1", HTTPS_PORT="18443")
        env = setup.environment(self.path, "test")
        self.assertNotIn("HTTPS_BIND", env)
        self.assertNotIn("HTTPS_PORT", env)

    def test_dns_failure_has_actionable_message_before_deployment(self):
        config = self.production_config()
        with patch.object(setup, "validate", return_value=config["services"]["web"]["environment"]), \
                patch.object(setup.shutil, "which", return_value="tool"), \
                patch.object(setup, "check_certificate"), patch.object(setup, "check_ports"), \
                patch.object(setup.socket, "getaddrinfo", side_effect=setup.socket.gaierror()):
            with self.assertRaisesRegex(ValueError, "Point its A record to this VPS"):
                setup.preflight(Mock(), config, True)

    def test_check_without_configuration_does_not_create_credentials(self):
        with self.assertRaisesRegex(ValueError, "--configure-only"):
            setup.main(["production", "--check", "--env-file", str(self.path),
                        "--log-file", str(Path(self.directory.name) / "setup.log")])
        self.assertFalse(self.path.exists())

    def test_cli_failure_is_recorded_without_exposing_environment_contents(self):
        log = Path(self.directory.name) / "setup.log"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/setup.py"), "production",
                                 "--check", "--env-file", str(self.path), "--log-file", str(log)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Setup stopped", log.read_text())
        self.assertIn("--configure-only", result.stderr)

    def test_no_input_skips_interactive_account_creation_after_deployment(self):
        self.configure()
        config = self.production_config()
        with patch.object(setup, "check_docker"), \
                patch.object(setup.Stack, "configuration", return_value=config), \
                patch.object(setup, "preflight", return_value=config["services"]["web"]["environment"]), \
                patch.object(setup, "deploy") as deploy, patch.object(setup, "operator_account") as operator, \
                patch.object(setup.sys.stdin, "isatty", return_value=True):
            setup.main(["production", "--no-input", "--env-file", str(self.path),
                        "--log-file", str(Path(self.directory.name) / "setup.log")])
        deploy.assert_called_once()
        operator.assert_called_once_with(deploy.call_args.args[0], interactive=False)


class OperatorAccountTests(unittest.TestCase):
    def setUp(self):
        self.stack = Mock(path=Path("/srv/my deployment/.env.production"),
                          env={"APP_IMAGE": "custom-production:local"},
                          command=["docker", "compose", "--project-name", "custom-production",
                                   "--env-file", "/srv/my deployment/.env.production",
                                   "-f", "docker-compose.yml", "-f", "docker-compose.tls.yml"])
        self.missing = subprocess.CompletedProcess([], 0, "Startup output\nSETUP_OPERATOR_EXISTS=0\n", "")
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_existing_administrator_skips_creation_on_rerun(self):
        self.stack.run.return_value = subprocess.CompletedProcess([], 0, "SETUP_OPERATOR_EXISTS=1\n", "")
        with patch("builtins.input") as prompt:
            setup.operator_account(self.stack, interactive=True)
        prompt.assert_not_called()
        self.assertEqual(self.stack.run.call_count, 1)
        self.assertIn("already exists", self.output.getvalue())

    def test_accept_runs_django_wizard_on_selected_stack_without_capturing_credentials(self):
        self.stack.run.side_effect = [self.missing, subprocess.CompletedProcess([], 0)]
        with patch("builtins.input", return_value=""):
            setup.operator_account(self.stack, interactive=True)
        self.stack.run.assert_called_with("exec", "web", "python", "manage.py", "createsuperuser", check=False)
        self.assertNotIn("capture", self.stack.run.call_args.kwargs)
        self.assertIn("Administrator account created", self.output.getvalue())

    def test_decline_keeps_site_running_and_prints_correct_manual_command(self):
        self.stack.run.return_value = self.missing
        with patch("builtins.input", return_value="n"):
            setup.operator_account(self.stack, interactive=True)
        self.assertEqual(self.stack.run.call_count, 1)
        self.assertIn("HTTPS services remain running", self.output.getvalue())
        command = self.output.getvalue().splitlines()[-1]
        parts = setup.shlex.split(command)
        self.assertIn("APP_ENV_FILE=/srv/my deployment/.env.production", parts)
        self.assertIn("APP_IMAGE=custom-production:local", parts)
        self.assertIn("custom-production", parts)
        self.assertEqual(parts[-5:], ["exec", "web", "python", "manage.py", "createsuperuser"])

    def test_noninteractive_run_prints_command_without_prompting(self):
        self.stack.run.return_value = self.missing
        with patch("builtins.input") as prompt:
            setup.operator_account(self.stack, interactive=False)
        prompt.assert_not_called()
        self.assertEqual(self.stack.run.call_count, 1)
        self.assertIn("createsuperuser", self.output.getvalue())

    def test_failed_creation_does_not_claim_account_created(self):
        self.stack.run.side_effect = [self.missing, subprocess.CompletedProcess([], 1)]
        with patch("builtins.input", return_value="yes"):
            setup.operator_account(self.stack, interactive=True)
        self.assertIn("did not complete", self.output.getvalue())
        self.assertNotIn("[OK] Administrator account created", self.output.getvalue())

    def test_cancelled_wizard_keeps_services_running_and_offers_retry(self):
        self.stack.run.side_effect = [self.missing, KeyboardInterrupt]
        with patch("builtins.input", return_value="y"):
            setup.operator_account(self.stack, interactive=True)
        self.assertIn("cancelled", self.output.getvalue())
        self.assertIn("createsuperuser", self.output.getvalue())

    def test_failed_account_check_does_not_disclose_raw_output_or_prompt(self):
        self.stack.run.return_value = subprocess.CompletedProcess([], 1, "private-output", "private-error")
        with patch("builtins.input") as prompt:
            setup.operator_account(self.stack, interactive=True)
        prompt.assert_not_called()
        self.assertIn("Could not check", self.output.getvalue())
        self.assertIn("createsuperuser", self.output.getvalue())
        self.assertNotIn("private-", self.output.getvalue())


class PortChecks(unittest.TestCase):
    def setUp(self):
        self.stack = Mock(env={"COMPOSE_PROJECT_NAME": "production"})
        self.config = {"services": {"nginx": {"ports": [
            {"host_ip": "0.0.0.0", "published": "443", "target": 443}]}}}
        self.owner = {"name": "/production-nginx-1", "labels": {
            "com.docker.compose.project": "production", "com.docker.compose.service": "nginx"},
            "ports": {"443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}]}}

    def test_existing_deployment_owns_its_ports_on_upgrade(self):
        with patch.object(setup, "docker_containers", return_value=[self.owner]), \
                patch.object(setup.socket, "socket") as probe:
            setup.check_ports(self.stack, self.config)
        probe.assert_not_called()

    def test_changing_an_overlapping_own_bind_requires_releasing_the_old_listener(self):
        for existing, requested in (("127.0.0.1", "0.0.0.0"), ("0.0.0.0", "127.0.0.1"),
                                    ("127.0.0.1", "::")):
            with self.subTest(existing=existing, requested=requested):
                self.owner["ports"]["443/tcp"][0]["HostIp"] = existing
                self.config["services"]["nginx"]["ports"][0]["host_ip"] = requested
                with patch.object(setup, "docker_containers", return_value=[self.owner]), \
                        patch.object(setup.socket, "socket") as probe:
                    with self.assertRaisesRegex(ValueError, "Before changing the bind address") as error:
                        setup.check_ports(self.stack, self.config)
                probe.assert_not_called()
                self.assertIn("docker stop production-nginx-1", str(error.exception))
                self.assertIn("rerun setup to check the requested address", str(error.exception))

    def test_matching_own_bind_is_allowed_with_an_additional_ipv6_mapping(self):
        self.owner["ports"]["443/tcp"].insert(0, {"HostIp": "::", "HostPort": "443"})
        with patch.object(setup, "docker_containers", return_value=[self.owner]), \
                patch.object(setup.socket, "socket") as probe:
            setup.check_ports(self.stack, self.config)
        probe.assert_not_called()

    def test_new_non_overlapping_address_is_checked_for_host_conflicts(self):
        self.owner["ports"]["443/tcp"][0]["HostIp"] = "127.0.0.1"
        self.config["services"]["nginx"]["ports"][0]["host_ip"] = "127.0.0.2"
        with patch.object(setup, "docker_containers", return_value=[self.owner]), \
                patch.object(setup.socket, "socket") as probe:
            probe.return_value.__enter__.return_value.bind.side_effect = OSError(errno.EADDRINUSE, "used")
            with self.assertRaisesRegex(ValueError, "Cannot bind 127.0.0.2:443"):
                setup.check_ports(self.stack, self.config)
        probe.return_value.__enter__.return_value.bind.assert_called_once_with(("127.0.0.2", 443))

    def test_other_container_conflict_is_found_even_without_host_socket(self):
        self.owner["labels"]["com.docker.compose.project"] = "old-test"
        with patch.object(setup, "docker_containers", return_value=[self.owner]):
            with self.assertRaisesRegex(ValueError, "already published by"):
                setup.check_ports(self.stack, self.config)

    def test_host_listener_conflict_has_actionable_message(self):
        with patch.object(setup, "docker_containers", return_value=[]), \
                patch.object(setup.socket, "socket") as probe:
            probe.return_value.__enter__.return_value.bind.side_effect = OSError(errno.EADDRINUSE, "used")
            with self.assertRaisesRegex(ValueError, "sudo ss -ltnp"):
                setup.check_ports(self.stack, self.config)

    def test_other_services_in_same_project_are_not_exempt(self):
        self.owner["labels"]["com.docker.compose.service"] = "old-proxy"
        with patch.object(setup, "docker_containers", return_value=[self.owner]):
            with self.assertRaisesRegex(ValueError, "already published by"):
                setup.check_ports(self.stack, self.config)


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required for certificate checks")
class CertificateChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cert_dir = Path(cls.directory.name) / "live/cafe.example.org"
        cls.cert_dir.mkdir(parents=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(cls.cert_dir / "privkey.pem"),
                        "-out", str(cls.cert_dir / "fullchain.pem"), "-days", "2",
                        "-subj", "/CN=cafe.example.org", "-addext", "subjectAltName=DNS:cafe.example.org"],
                       check=True, capture_output=True)

    def setUp(self):
        self.config = {"services": {
            "web": {"environment": {"PUBLIC_URL": "https://cafe.example.org"}},
            "nginx": {"environment": {"TLS_CERT_NAME": "cafe.example.org"},
                      "volumes": [{"source": self.directory.name, "target": "/etc/letsencrypt"}]}}}

    def test_valid_certificate(self):
        setup.check_certificate(self.config)

    def test_expired_certificate_is_rejected_before_start(self):
        with patch.object(setup.time, "time", return_value=setup.time.time() + 10 * 86400):
            with self.assertRaisesRegex(ValueError, "expired"):
                setup.check_certificate(self.config)

    def test_future_certificate_is_rejected(self):
        with patch.object(setup.time, "time", return_value=setup.time.time() - 10 * 86400):
            with self.assertRaisesRegex(ValueError, "not valid yet"):
                setup.check_certificate(self.config)

    def test_wrong_hostname_is_rejected(self):
        self.config["services"]["web"]["environment"]["PUBLIC_URL"] = "https://other.example.org"
        with self.assertRaisesRegex(ValueError, "does not cover"):
            setup.check_certificate(self.config)

    def test_hostname_check_requires_explicit_match_even_with_zero_exit_status(self):
        # OpenSSL 3.0 prints a mismatch but exits zero for x509 -checkhost.
        run = subprocess.run
        cases = [
            (0, "Hostname cafe.example.org does NOT match certificate\n", False),
            (0, "", False),
            (0, "Hostname other.example.org does match certificate\n", False),
            (1, "Hostname cafe.example.org does match certificate\n", False),
            (0, "Hostname cafe.example.org does match certificate\n", True),
        ]
        for code, output, accepted in cases:
            def openssl(command, **kwargs):
                if "-checkhost" in command:
                    return subprocess.CompletedProcess(command, code, output, "")
                return run(command, **kwargs)

            with self.subTest(code=code, output=output), patch.object(setup.subprocess, "run", side_effect=openssl):
                if accepted:
                    setup.check_certificate(self.config)
                else:
                    with self.assertRaisesRegex(ValueError, "does not cover"):
                        setup.check_certificate(self.config)

    def test_mismatched_private_key_is_rejected(self):
        original = (self.cert_dir / "privkey.pem").read_bytes()
        try:
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-out",
                            str(self.cert_dir / "privkey.pem"), "-pkeyopt", "rsa_keygen_bits:2048"],
                           check=True, capture_output=True)
            with self.assertRaisesRegex(ValueError, "matching private key"):
                setup.check_certificate(self.config)
        finally:
            (self.cert_dir / "privkey.pem").write_bytes(original)


class HTTPSChecks(unittest.TestCase):
    def setUp(self):
        self.config = {"services": {
            "web": {"environment": {"PUBLIC_URL": "https://cafe.example.org"}},
            "nginx": {"ports": [{"host_ip": "127.0.0.1", "published": "18443", "target": 443}]}}}
        self.stack = Mock(command=["docker", "compose", "--project-name", "test"])

    def test_local_check_uses_published_port_and_domain_for_certificate_verification(self):
        with patch.object(setup, "https_probe", return_value=None) as probe:
            setup.verify_https(self.stack, self.config)
        self.assertEqual(probe.call_args_list[0].args,
                         ("https://cafe.example.org:18443/health", "cafe.example.org:18443:127.0.0.1"))
        self.assertEqual(probe.call_args_list[1].args, ("https://cafe.example.org/health",))

    def test_local_failure_does_not_probe_public_dns(self):
        with patch.object(setup, "https_probe", return_value="connection failed") as probe:
            with self.assertRaisesRegex(ValueError, "Local HTTPS check failed"):
                setup.verify_https(self.stack, self.config)
        self.assertEqual(probe.call_count, 1)

    def test_public_failure_distinguishes_dns_and_firewall_from_app_health(self):
        with patch.object(setup, "https_probe", side_effect=[None, "timeout"]):
            with self.assertRaisesRegex(ValueError, "Local HTTPS works, but the public URL failed"):
                setup.verify_https(self.stack, self.config)

    def test_tls_failure_is_not_bypassed_and_raw_response_is_not_disclosed(self):
        with patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 60, "private-response", "private-diagnostic")) as run:
            error = setup.https_probe("https://cafe.example.org/health")
        self.assertIn("60", error)
        self.assertNotIn("private", error)
        self.assertNotIn("--insecure", run.call_args.args[0])
        self.assertEqual(run.call_count, 1)

    def test_connection_retry_and_health_response(self):
        with patch.object(setup.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 7, "", "refused"),
                subprocess.CompletedProcess([], 0, json.dumps({"status": "ok"}), "")]) as run, \
                patch.object(setup.time, "sleep"):
            self.assertIsNone(setup.https_probe("https://cafe.example.org/health"))
        self.assertEqual(run.call_count, 2)

    def test_wrong_application_response_fails(self):
        with patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "<html>other site</html>", "")):
            self.assertEqual(setup.https_probe("https://cafe.example.org/health"), "unexpected /health response")


if __name__ == "__main__":
    unittest.main()
