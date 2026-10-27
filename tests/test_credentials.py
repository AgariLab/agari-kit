import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agari_kit.cli import load_token


class CredentialTests(unittest.TestCase):
    def test_environment_and_private_file_without_shell_evaluation(self):
        with patch.dict("os.environ", {"RIICHI_BOT_TOKEN": "environment-token"}):
            self.assertEqual(load_token(None), "environment-token")
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / ".riichi-token"
                path.write_text("literal-$token\n")
                self.assertEqual(load_token(path), "literal-$token")

    def test_errors_never_include_token_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".riichi-token"
            for content in (b"secret token", b"secret\xfftoken", b""):
                path.write_bytes(content)
                with self.assertRaises(ValueError) as caught:
                    load_token(path)
                self.assertNotIn("secret", str(caught.exception))
