import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xagent.interfaces.clients.desktop_launcher import (
    desktop_app_dir,
    desktop_dependencies_ready,
    desktop_setup_hint,
    packaged_desktop_app,
)


class DesktopLauncherTests(unittest.TestCase):
    def test_desktop_app_dir_points_at_repo_desktop_folder(self):
        desktop_dir = desktop_app_dir()
        self.assertTrue((desktop_dir / "package.json").is_file())
        self.assertTrue((desktop_dir / "electron" / "main.cjs").is_file())

    def test_desktop_setup_hint_mentions_release_or_build(self):
        hint = desktop_setup_hint()
        self.assertTrue("GitHub Releases" in hint or "npm run build" in hint)

    def test_packaged_desktop_app_honors_override_env(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            app_path = Path(handle.name)
        try:
            with patch.dict("os.environ", {"XAGENT_DESKTOP_APP": str(app_path)}, clear=False):
                self.assertEqual(packaged_desktop_app(), app_path.resolve())
        finally:
            app_path.unlink(missing_ok=True)

    def test_desktop_dependencies_ready_when_packaged_app_exists(self):
        app_path = Path(__file__).resolve()
        with patch(
            "xagent.interfaces.clients.desktop_launcher.packaged_desktop_app",
            return_value=app_path,
        ):
            self.assertTrue(desktop_dependencies_ready())


if __name__ == "__main__":
    unittest.main()
