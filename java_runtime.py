"""Resolve and actually probe a Java 17+ runtime without shell activation."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def java_environment(home=None):
    """Explicit homes fail closed; discovery skips broken inherited runtimes."""
    candidates = [Path(home).expanduser()/'bin/java'] if home else []
    if not home:
        if os.environ.get('JAVA_HOME'):
            candidates.append(Path(os.environ['JAVA_HOME'])/'bin/java')
        # macOS's Java launcher can select an obsolete Intel applet plugin.
        if sys.platform == 'darwin':
            try:
                found = subprocess.check_output(['/usr/libexec/java_home', '-v', '17+'],
                                                text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
                if found:
                    candidates.append(Path(found)/'bin/java')
            except (OSError, subprocess.SubprocessError):
                pass
        if shutil.which('brew'):
            try:
                prefix = subprocess.check_output(['brew', '--prefix', 'openjdk@17'], text=True,
                                                stderr=subprocess.DEVNULL, timeout=10).strip()
                candidates.append(Path(prefix)/'libexec/openjdk.jdk/Contents/Home/bin/java')
            except (OSError, subprocess.SubprocessError):
                pass
        if shutil.which('java'):
            candidates.append(Path(shutil.which('java')))
    for binary in dict.fromkeys(candidates):
        try:
            result = subprocess.run([str(binary), '-XshowSettings:properties', '-version'],
                                    capture_output=True, text=True, timeout=10)
            match = re.search(r'(?:openjdk|java) version "(\d+)', result.stderr + result.stdout)
            if result.returncode == 0 and match and int(match[1]) >= 17:
                resolved = binary.resolve()
                actual_home = re.search(r'^\s*java.home\s*=\s*(.+)$', result.stderr + result.stdout, re.M)
                selected = (Path(home).expanduser().absolute() if home else
                            Path(actual_home[1].strip()) if actual_home else resolved.parent.parent)
                env = os.environ.copy()
                env['JAVA_HOME'] = str(selected)
                env['PATH'] = str(selected/'bin') + os.pathsep + env.get('PATH', os.defpath)
                return env
        except (OSError, subprocess.SubprocessError):
            pass
    raise RuntimeError('No runnable Java 17+; configure java_home with a compatible JDK')
