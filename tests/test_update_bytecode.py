"""Rapid git checkouts can have identical source sizes and integer mtimes."""
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest



class UpdateBytecode(unittest.TestCase):
    def test_installer_refreshes_same_second_cache_before_first_cli_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home/'.agent2telegram-src'
            (source/'.git').mkdir(parents=True)
            package = source/'agent2telegram'
            package.mkdir()
            init = package/'__init__.py'
            init.write_text('__version__ = "1.2.1"\n')
            (package/'__main__.py').write_text('from . import __version__; print(__version__)\n')
            py_compile.compile(str(init),doraise=True,invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
            original = init.stat()
            binary = home/'bin'
            binary.mkdir()
            git = binary/'git'
            git.write_text(f'#!{sys.executable}\nimport os\nfrom pathlib import Path\n'
                           f'p=Path({str(init)!r})\np.write_text(\'__version__ = "1.2.2"\\n\')\n'
                           f'os.utime(p,ns=({original.st_atime_ns},{original.st_mtime_ns}))\n')
            git.chmod(0o755)
            (binary/'tmux').write_text('#!/bin/sh\nexit 0\n')
            (binary/'tmux').chmod(0o755)
            env = {'HOME':str(home),'PATH':f'{binary}:{Path(sys.executable).parent}:{os.defpath}'}
            installer = Path(__file__).resolve().parents[1]/'install.sh'
            result = subprocess.run(['bash',str(installer),'--no-pip','--no-setup'],cwd=home,
                                    env=env,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            result = subprocess.run([str(binary/'agent2telegram'),'--version'],cwd=home,
                                    env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'1.2.2')
