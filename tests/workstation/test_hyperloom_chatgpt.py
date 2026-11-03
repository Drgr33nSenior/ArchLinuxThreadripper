"""Credential-free tests for the explicit Codex subscription transport."""

import asyncio
from pathlib import Path
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import hyperloom_chatgpt as chatgpt


class ChatGPTTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "login"
        self.home.mkdir(mode=0o700)
        (self.home / "config.toml").write_text(chatgpt.CONFIG)
        self.source = {chatgpt.AUTH_MODE_ENV: "chatgpt", chatgpt.AUTH_HOME_ENV: str(self.home),
                       "HYPERLOOM_BENCHMARK_BACKEND": "workstation_owner"}

    def test_mode_is_explicit_and_has_no_api_fallback(self):
        self.assertFalse(chatgpt.enabled({}))
        self.assertTrue(chatgpt.enabled(self.source))
        for key in chatgpt.CONFLICTS:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "cannot mix"):
                chatgpt.environment(dict(self.source, **{key: "fixture-conflict"}))
        with self.assertRaises(ValueError):
            chatgpt.environment(dict(self.source, HYPERLOOM_BENCHMARK_BACKEND="magpie"))

    def test_home_is_private_and_outside_all_writable_roots(self):
        self.assertEqual(chatgpt.auth_home(self.source), self.home)
        for root in (self.home, self.root, self.home / "nested"):
            with self.assertRaisesRegex(ValueError, "outside"):
                chatgpt.auth_home(self.source, writable_roots=(root,))
        self.home.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private"):
            chatgpt.auth_home(self.source)

    def test_changed_or_linked_config_and_linked_home_refuse(self):
        config = self.home / "config.toml"
        config.write_text(chatgpt.CONFIG + "# changed\n")
        with self.assertRaisesRegex(ValueError, "configuration"):
            chatgpt.auth_home(self.source)
        config.unlink()
        target = self.root / "config.toml"; target.write_text(chatgpt.CONFIG)
        config.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "configuration"):
            chatgpt.auth_home(self.source)
        link = self.root / "linked-home"; link.symlink_to(self.home)
        with self.assertRaises(ValueError):
            chatgpt.auth_home(dict(self.source, **{chatgpt.AUTH_HOME_ENV: str(link)}))

    def test_persistent_home_never_reads_auth_or_deletes_the_login(self):
        cwd = self.root / "scratch"; cwd.mkdir()
        # The unreadable sentinel is not a credential. No auth-store read is needed.
        auth = self.home / "auth.json"; auth.write_text("fixture-unreadable")
        original = Path.read_text
        def read(path, *args, **kwargs):
            self.assertNotEqual(path.name, "auth.json", "adapter must never read the credential store")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", read):
            with chatgpt.persistent_home(source=self.source, cwd=cwd, writable_roots=(cwd,)) as path:
                self.assertEqual(path, self.home)
        self.assertTrue(auth.exists())
        self.assertTrue(self.home.is_dir())

    def fake_sdk(self, kind="chatgpt"):
        observed = {}
        class Client:
            def __init__(self, config): observed["config"] = config
            async def __aenter__(self): return self
            async def __aexit__(self, *args): observed["closed"] = True
            async def account(self):
                return SimpleNamespace(account=SimpleNamespace(root=SimpleNamespace(type=kind,
                                        email="fixture-not-returned", id="fixture-not-returned")))
            async def models(self):
                observed["models_called"] = True
                return SimpleNamespace(data=[SimpleNamespace(model="fixture-model", is_default=True)])
        return SimpleNamespace(CodexConfig=lambda **kw: kw, AsyncCodex=Client), observed

    def test_only_authentication_method_and_model_ids_are_exported(self):
        sdk, observed = self.fake_sdk()
        result = asyncio.run(chatgpt.account_catalog(source=self.source, sdk=sdk))
        self.assertEqual(result, {"authentication": "chatgpt",
                                  "models": [{"model": "fixture-model", "is_default": True}]})
        self.assertTrue(observed["closed"])
        self.assertEqual(observed["config"]["env"]["CODEX_HOME"], str(self.home))
        self.assertEqual(observed["config"]["config_overrides"], chatgpt.OVERRIDES)

    def test_api_or_absent_account_refuses_before_catalog(self):
        for kind in ("apiKey", None):
            sdk, observed = self.fake_sdk(kind)
            with self.assertRaisesRegex(ValueError, "sign-in"):
                asyncio.run(chatgpt.account_catalog(source=self.source, sdk=sdk))
            self.assertNotIn("models_called", observed)
            self.assertTrue(observed["closed"])

    def test_model_resolution_preserves_explicit_choice_and_never_falls_back(self):
        args = SimpleNamespace(codex_model="fixture-model", specialist_model=None, critic_protocol="auto")
        catalog = {"models": [{"model": "fixture-model", "is_default": True}]}
        with mock.patch.object(chatgpt, "account_catalog", new=mock.AsyncMock(return_value=catalog)):
            chatgpt.resolve_models(args)
            self.assertEqual(args.claude_model, "fixture-model")
            args.specialist_model = "missing"
            with self.assertRaisesRegex(ValueError, "unavailable"):
                chatgpt.resolve_models(args)
            args.specialist_model = None; args.critic_protocol = "anthropic"
            with self.assertRaisesRegex(ValueError, "Anthropic"):
                chatgpt.resolve_models(args)

    def test_permission_paths_use_atomic_tables_for_cli_override_parser(self):
        workspace = self.root / "workspace.with.dots"
        workspace.mkdir()
        options = chatgpt.permission_overrides(self.source, "workspace-write", (workspace,))
        tables = {}
        for option in options:
            key, value = option.split("=", 1)
            # Codex splits the override key on dots before parsing its value.
            # Path keys must therefore live inside a TOML table value.
            self.assertNotIn('"', key)
            tables[key] = tomllib.loads("value=" + value)["value"]
        self.assertEqual(tables["permissions.hyperloom_write.filesystem"], {str(self.home): "deny"})
        self.assertEqual(tables["permissions.hyperloom_write.workspace_roots"], {str(workspace): True})
        self.assertFalse(tables["permissions.hyperloom_write.network.enabled"])


if __name__ == "__main__":
    unittest.main()
