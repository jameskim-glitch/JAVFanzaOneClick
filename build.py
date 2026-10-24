"""Build a fresh allowlisted Windows release, including dependency notices."""
import ast
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import ssl
import subprocess
import sys
import time
import uuid
import zipfile

from installer import PAYLOAD_FILES

ROOT = Path(__file__).resolve().parent
DEPENDENCIES = {'Pillow': '12.3.0', 'pyinstaller': '6.22.0'}


def version():
    for node in ast.parse((ROOT / 'jav_fanza_oneclick.py').read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError('Missing version')


def linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def directory(path, *, fresh=False):
    path = Path(path)
    if not path.resolve().is_relative_to(ROOT) or path.resolve() == ROOT:
        raise RuntimeError('Output must remain inside this checkout')
    for item in [path, *path.parents]:
        if item == ROOT:
            break
        if linked(item):
            raise RuntimeError('Linked build directory is not supported')
    path.mkdir(parents=True, exist_ok=not fresh)
    return path


def validate_runtime():
    if os.name != 'nt' or platform.python_implementation() != 'CPython' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('Use official Windows x64 CPython for release builds.')
    if sys.version_info[:3] != (3, 13, 17) or sys.prefix == sys.base_prefix:
        raise RuntimeError('Create a fresh venv using official CPython 3.13.17.')
    if os.environ.get('PYTHONPATH') or os.environ.get('PYTHONHOME'):
        raise RuntimeError('Unset PYTHONPATH/PYTHONHOME for release builds.')
    source = Path(ssl.__file__).read_text(encoding='utf-8')
    if 'codex-rejected-cas' in source or 'sitecustomize' in sys.modules or 'usercustomize' in sys.modules:
        raise RuntimeError('Customized runtime detected; use a clean official Python installation.')
    if (Path(sys.base_prefix) / 'Lib' / 'codex-rejected-cas.pem').exists():
        raise RuntimeError('Managed runtime is not a public release build environment.')
    for name, expected in DEPENDENCIES.items():
        if metadata.version(name) != expected:
            raise RuntimeError(f'Expected {name}=={expected}')


def package_license(name, suffix):
    dist = metadata.distribution(name)
    matches = [f for f in dist.files or () if str(f).replace('\\', '/').endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f'Missing or ambiguous license for {name}')
    return Path(dist.locate_file(matches[0])).read_text(encoding='utf-8')


def notices():
    sections = [
        ('CPython 3.13.17 and bundled Windows components', (Path(sys.base_prefix) / 'LICENSE.txt').read_text(encoding='utf-8')),
        ('Pillow 12.3.0 and bundled image codecs', package_license('Pillow', '.dist-info/licenses/LICENSE')),
        ('PyInstaller 6.22.0 bootloader and runtime hooks', package_license('pyinstaller', '.dist-info/licenses/COPYING.txt')),
    ]
    text = (
        'JAV FANZA OneClick - third-party notices\n\n'
        'This software is based in part on the work of the Independent JPEG Group.\n'
        'Portions of this software are copyright The FreeType Project (www.freetype.org). All rights reserved.\n'
        'FreeType is used under the FreeType License option, not the GPL option.\n'
        'These notices do not grant a license to the project itself or downloaded media.\n'
        'Microsoft runtime components are for Microsoft platforms only; the Windows binary\n'
        'redistribution conditions in the CPython notice below apply to those components.\n'
    )
    for title, body in sections:
        if len(body.strip()) < 500:
            raise RuntimeError(f'Incomplete notice: {title}')
        text += '\n\n===== ' + title + ' =====\n\n' + body
    return text


def manifest_paths(payload):
    result = []
    for name in PAYLOAD_FILES:
        if Path(name).name != name or name in ('.', '..'):
            raise RuntimeError('Payload names must be flat filenames')
        path = payload / name
        if linked(path) or not path.is_file() or path.resolve().parent != payload.resolve():
            raise RuntimeError(f'Missing or linked payload file: {name}')
        result.append(path)
    return result


def package_zip(payload, destination):
    paths = manifest_paths(payload)
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, 'JAVFanzaOneClick/' + path.name)


def installer_data_args(payload):
    args = []
    for path in manifest_paths(payload):
        args.extend(['--add-data', str(path) + ';payload/JAVFanzaOneClick'])
    return args


def run(*args):
    subprocess.run([sys.executable, '-I', '-m', 'PyInstaller', *map(str, args)], cwd=ROOT, check=True)


def main():
    validate_runtime()
    release = version()
    run_id = time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
    output = directory(ROOT / 'dist' / release / run_id, fresh=True)
    scratch = directory(ROOT / '.build' / (release + '-' + run_id), fresh=True)
    payload = directory(output / 'JAVFanzaOneClick', fresh=True)
    notice_file = payload / 'THIRD_PARTY_NOTICES.txt'
    notice_file.write_text(notices(), encoding='utf-8')
    info_file = payload / 'BUILD_INFO.json'
    info_file.write_text(json.dumps({
        'version': release, 'python': platform.python_version(), 'platform': 'Windows x64',
        'openssl': ssl.OPENSSL_VERSION, 'dependencies': DEPENDENCIES, 'code_signed': False,
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    run('--onefile', '--noconfirm', '--exclude-module', 'numpy', '--exclude-module', 'tkinter', '--name', 'jav_fanza_oneclick',
        '--distpath', payload, '--workpath', directory(scratch / 'backend'),
        '--specpath', directory(scratch / 'spec'), '--paths', ROOT,
        '--add-data', str(notice_file) + ';.', '--add-data', str(info_file) + ';.', ROOT / 'jav_fanza_oneclick.py')
    for name in ('jav_fanza_oneclick.js', 'JAVFanzaOneClick.yml', 'README_KO.md', 'README.md', 'THIRD_PARTY.md'):
        shutil.copy2(ROOT / name, payload / name)
    run('--onefile', '--windowed', '--noconfirm', '--name', f'JAVFanzaOneClick_Setup_{release}_Windows_x64',
        '--distpath', output, '--workpath', directory(scratch / 'installer'),
        '--specpath', directory(scratch / 'installer-spec'),
        '--add-data', str(notice_file) + ';.', *installer_data_args(payload), ROOT / 'installer.py')
    archive = output / f'JAVFanzaOneClick_{release}_Windows_x64.zip'
    package_zip(payload, archive)
    setup = output / f'JAVFanzaOneClick_Setup_{release}_Windows_x64.exe'
    hashes = [f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}' for p in (setup, archive)]
    (output / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n', encoding='utf-8')
    print(f'Built {output}')


if __name__ == '__main__':
    main()
