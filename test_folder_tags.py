import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import folder_tags as ft


class Client:
    def __init__(self):
        self.roots = [r'D:\library\온천']
        self.tags = [{'id': '1', 'name': '온천', 'ignore_auto_tag': False},
                     {'id': '2', 'name': '학교물', 'ignore_auto_tag': False}]
        self.rows = {'1': {'id': '1', 'files': [{'path': r'D:\library\온천\학교물\video.mp4'}], 'tags': [{'id': '99'}]}}
        self.updates, self.backups = [], 0
        self.backup_fail = self.write_fail = self.concurrent = self.change_path = False
        self.scene_reads = 0

    def graphql(self, query, variables=None, **kwargs):
        variables = variables or {}
        if 'OneClickFolderRoots' in query:
            return {'configuration': {'general': {'stashes': [{'path': p} for p in self.roots]}}}
        if 'OneClickFolderTags' in query:
            page = variables['page']
            return {'findTags': {'count': len(self.tags), 'tags': copy.deepcopy(self.tags[(page-1)*100:page*100])}}
        if 'OneClickFolderScenes' in query:
            page = variables['page']
            return {'findScenes': {'count': len(self.rows), 'scenes': copy.deepcopy(list(self.rows.values())[(page-1)*100:page*100])}}
        if 'OneClickFolderScene' in query:
            self.scene_reads += 1
            if self.change_path and self.scene_reads == 2:
                self.rows['1']['files'] = [{'path': r'D:\other\movie.mp4'}]
            return {'findScene': copy.deepcopy(self.rows.get(variables['id']))}
        if 'OneClickMaintenanceBackup' in query:
            if self.backup_fail:
                raise RuntimeError('backup failed')
            self.backups += 1
            return {'backupDatabase': None}
        if 'OneClickFolderTagAdd' in query:
            if self.write_fail:
                raise RuntimeError('connection lost')
            value = variables['input']
            assert set(value) == {'ids', 'tag_ids'}
            assert value['tag_ids']['mode'] == 'ADD'
            self.updates.append(copy.deepcopy(value))
            row = self.rows[value['ids'][0]]
            if self.concurrent:
                row['tags'].append({'id': '100'})
            existing = {t['id'] for t in row['tags']}
            row['tags'].extend({'id': i} for i in value['tag_ids']['ids'] if i not in existing)
            return {'bulkSceneUpdate': [copy.deepcopy(row)]}
        raise AssertionError(query)


class FolderTests(unittest.TestCase):
    def test_windows_root_and_subfolders_not_filename_or_parent(self):
        scene = {'files': [{'path': r'd:\unrelated\ROOT\child\movie.mp4'}]}
        self.assertEqual(ft.folder_names(scene, [r'D:\unrelated\root']), {'root', 'child'})

    def test_path_boundaries(self):
        for value in [r'D:\library2\온천\a.mp4', r'D:\library\..\온천\a.mp4', r'library\온천\a.mp4', 'D:온천/a.mp4']:
            with self.subTest(value=value):
                self.assertFalse(ft.folder_names({'files': [{'path': value}]}, [r'D:\library']))

    def test_posix_and_unc(self):
        self.assertEqual(ft.folder_names({'files': [{'path': '/library/온천/a.mp4'}]}, ['/library']), {'library', '온천'})
        self.assertEqual(ft.folder_names({'files': [{'path': r'\\host\share\온천\a.mp4'}]}, [r'\\host\share\온천']), {'온천'})

    def test_multiple_files(self):
        scene = {'files': [{'path': r'D:\온천\a.mp4'}, {'path': r'D:\학교물\b.mp4'}]}
        self.assertEqual(ft.folder_names(scene, [r'D:\온천', r'D:\학교물']), {'온천', '학교물'})

    def test_exact_names_and_normalization(self):
        self.assertEqual(ft.key('학교물'), ft.key('학교물'))
        self.assertNotEqual(ft.key('학교 물'), ft.key('학교물'))
        client = Client()
        client.tags[0]['name'] = '다른이름'
        client.tags[0]['aliases'] = ['온천']
        self.assertEqual(ft.plan(client)['items'][0]['tags'], [{'id': '2', 'name': '학교물'}])

    def test_ignore_and_collision(self):
        client = Client()
        client.tags[0]['ignore_auto_tag'] = True
        client.tags.append({'id': '3', 'name': '학교물', 'ignore_auto_tag': False})
        self.assertEqual(ft.plan(client)['pending'], 0)
        self.assertEqual(ft.context(client)['ambiguous_names'], ['학교물'])

    def test_pagination(self):
        client = Client()
        client.tags += [{'id': str(i), 'name': str(i), 'ignore_auto_tag': False} for i in range(3, 205)]
        client.rows.update({str(i): dict(copy.deepcopy(client.rows['1']), id=str(i)) for i in range(2, 205)})
        self.assertEqual(ft.plan(client)['pending'], 204)

    def test_dry_run_no_writes(self):
        with tempfile.TemporaryDirectory() as root:
            client = Client()
            self.assertEqual(ft.run(client, root, True)['pending'], 1)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(client.updates, [])

    def test_add_only_preserves_concurrent_tags_and_idempotence(self):
        with tempfile.TemporaryDirectory() as root:
            client = Client()
            client.concurrent = True
            first = ft.run(client, root)
            self.assertTrue(first['ok'])
            self.assertEqual(first['added'], 2)
            self.assertEqual({t['id'] for t in client.rows['1']['tags']}, {'1', '2', '99', '100'})
            self.assertEqual(client.backups, 1)
            self.assertEqual(ft.run(client, root)['updated'], 0)
            self.assertEqual(client.backups, 1)

    def test_backup_failure_stops_before_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            client = Client()
            client.backup_fail = True
            self.assertFalse(ft.run(client, root)['ok'])
            self.assertEqual(client.updates, [])

    def test_uncertain_write_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as root:
            client = Client()
            client.write_fail = True
            client.rows['2'] = dict(copy.deepcopy(client.rows['1']), id='2')
            result = ft.run(client, root)
            self.assertFalse(result['ok'])
            self.assertEqual(len(result['results']), 1)
            records = [json.loads(p.read_text(encoding='utf-8')) for p in (Path(root)/'folder_tags').glob('*.json')]
            self.assertTrue(any(r.get('status') == 'needs_check' for r in records))

    def test_changed_path_not_tagged(self):
        with tempfile.TemporaryDirectory() as root:
            client = Client()
            client.change_path = True
            self.assertEqual(ft.apply_one(client, root, '1')['status'], 'changed')
            self.assertEqual(client.updates, [])

    def test_hook_no_backup_gone_and_invalid_id(self):
        with tempfile.TemporaryDirectory() as root:
            client = Client()
            self.assertEqual(ft.apply_one(client, root, '1')['status'], 'updated')
            self.assertEqual(client.backups, 0)
            self.assertEqual(ft.apply_one(client, root, '404')['status'], 'gone')
            with self.assertRaises(ValueError):
                ft.apply_one(client, root, '../wrong')

    def test_inconsistent_pagination_rejected(self):
        client = Client()
        original = client.graphql
        def changed(query, variables=None, **kwargs):
            value = original(query, variables, **kwargs)
            if 'OneClickFolderScenes' in query:
                value['findScenes']['count'] = 2 if variables['page'] == 1 else 3
            return value
        with patch.object(client, 'graphql', side_effect=changed):
            with self.assertRaises(ValueError):
                ft.plan(client)


class ProtocolTests(unittest.TestCase):
    def test_independent_hook_and_bulk_modes(self):
        # The server implements only folder queries: metadata/translation calls fail.
        client = Client()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                try:
                    result = {'data': client.graphql(request['query'], request.get('variables'))}
                except Exception as exc:
                    result = {'errors': [{'message': str(exc)}]}
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        command = [os.environ['ONECLICK_BACKEND_EXE']] if os.environ.get('ONECLICK_BACKEND_EXE') else [sys.executable, str(Path(__file__).with_name('jav_fanza_oneclick.py'))]
        with tempfile.TemporaryDirectory() as root:
            def operation(mode, **args):
                request = {'args': {'mode': mode, **args}, 'server_connection': {'Endpoint': f'http://127.0.0.1:{server.server_port}/graphql', 'PluginDir': root}}
                proc = subprocess.run(command, input=json.dumps(request), text=True, encoding='utf-8', capture_output=True, timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(proc.stdout)['output']
            self.assertEqual(operation('folder_tags_bulk', dry_run=True)['pending'], 1)
            self.assertEqual(client.updates, [])
            self.assertEqual(operation('folder_tags_scene', hookContext={'id': '1', 'type': 'Scene.Create.Post'})['status'], 'updated')
            self.assertEqual(client.backups, 0)
            self.assertEqual(operation('folder_tags_scene', scene_id='1')['status'], 'unchanged')
            self.assertEqual(operation('folder_tags_bulk')['updated'], 0)
            self.assertEqual(len(client.updates), 1)
            client.rows['2'] = {'id': '2', 'files': [{'path': r'D:\library\온천\new.mp4'}], 'tags': [{'id': '99'}]}
            self.assertEqual(operation('folder_tags_bulk')['updated'], 1)
            self.assertEqual(client.backups, 1)
            self.assertEqual(operation('folder_tags_bulk')['updated'], 0)
            self.assertEqual(client.backups, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
