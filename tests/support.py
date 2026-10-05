import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pilot-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def copy_fixture(self, name, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / name, dest)
        return dest
