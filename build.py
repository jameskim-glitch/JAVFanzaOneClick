"""Build only the explicit public plugin payload; never copy user configuration."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def version():
    for node in ast.parse((ROOT / 'jav_fanza_oneclick.py').read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError('Missing version')


def directory(path):
    path = Path(path)
    if not path.resolve().is_relative_to(ROOT):
        raise RuntimeError('Output must remain inside this checkout')
    for item in [path, *path.parents]:
        if item == ROOT:
            break
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise RuntimeError('Linked build directory is not supported')
    path.mkdir(parents=True, exist_ok=True)
    return path


def run(*args):
    subprocess.run([sys.executable, '-m', 'PyInstaller', *map(str, args)], cwd=ROOT, check=True)


def main():
    if os.name != 'nt':
        raise SystemExit('Build the Windows installer on Windows.')
    release = version()
    output = directory(ROOT / 'dist' / release)
    scratch = directory(ROOT / '.build' / release)
    payload = directory(output / 'JAVFanzaOneClick')
    ssl_extra = []
    # Some managed runtimes require this trust-policy data file at startup.
    # Do not change TLS verification or ship any user certificates/settings.
    policy = Path(sys.base_prefix) / 'Lib' / 'codex-rejected-cas.pem'
    if policy.is_file():
        ssl_extra = ['--add-data', str(policy) + ';.']
    run('--onefile', '--noconfirm', '--exclude-module', 'numpy', '--name', 'jav_fanza_oneclick',
        '--distpath', payload, '--workpath', directory(scratch / 'backend'),
        '--specpath', directory(scratch / 'spec'), '--paths', ROOT,
        *ssl_extra, ROOT / 'jav_fanza_oneclick.py')
    for name in ('jav_fanza_oneclick.js', 'JAVFanzaOneClick.yml', 'README_KO.md'):
        shutil.copy2(ROOT / name, payload / name)
    run('--onefile', '--windowed', '--noconfirm', '--name', f'JAVFanzaOneClick_Setup_{release}_Windows_x64',
        '--distpath', output, '--workpath', directory(scratch / 'installer'),
        '--specpath', directory(scratch / 'installer-spec'),
        '--add-data', str(payload) + ';payload\\JAVFanzaOneClick', ROOT / 'installer.py')
    shutil.make_archive(str(output / f'JAVFanzaOneClick_{release}_Windows_x64'), 'zip', output, 'JAVFanzaOneClick')
    print(f'Built {output}')


if __name__ == '__main__':
    main()
