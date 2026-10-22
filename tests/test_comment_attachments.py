import copy
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from gitverse_cli.cli import execute, parser
from gitverse_cli.client import Client, GVError
from gitverse_cli.comment_attachments import prepare, render, asset_value, attachment_list
from gitverse_cli.pr_comment import discussion_comment
from test_pr_comment import Fake


def metadata(identity=1, name='a.png', size=3):
    return {'id': identity, 'uuid': f'00000000-0000-0000-0000-{identity:012d}', 'name': name,
            'size': size, 'browser_download_url': 'https://gitverse.ru/api/attachments/' + f'00000000-0000-0000-0000-{identity:012d}'}


class AttachedFake(Fake):
    def __init__(self):
        super().__init__()
        self.uploads = []
        self.break_upload = None

    def request(self, method, path, query=None, body=None, **kwargs):
        if path.endswith('/attachments'):
            self.calls.append((method, path, query, body, kwargs))
            if self.hook: self.hook(self, method, path)
            if self.break_upload: raise self.break_upload
            wire = kwargs['encoded_body']
            name = wire.split(b'filename="')[1].split(b'"')[0].decode()
            size = len(wire.split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0])
            row = metadata(len(self.uploads) + 1, name, size)
            self.uploads.append(row)
            self.rows[-1]['attachments'] = copy.deepcopy(self.uploads)
            return copy.deepcopy(self.uploads)
        result = super().request(method, path, query, body, **kwargs)
        if isinstance(result, dict) and '/comments' in path:
            result.setdefault('attachments', [])
        return result


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'a.png'
        self.file.write_bytes(b'png')
        self.client = AttachedFake()

    def call(self, *flags):
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--attach', str(self.file), *flags])
        args.number = 9
        return discussion_comment(self.client, 'o/r', args)

    def test_attach_only_creates_uploads_patches_and_independently_reads(self):
        row = self.call()
        self.assertEqual(row['body'], '![a](' + metadata()['browser_download_url'] + ')')
        self.assertEqual([c[0] for c in self.client.calls if c[0] != 'GET'], ['POST', 'POST', 'PATCH'])
        upload = next(c for c in self.client.calls if c[1].endswith('/attachments'))
        self.assertIn(b'name="attachment"; filename="a.png"', upload[4]['encoded_body'])
        self.assertIn(b'Content-Type: image/png\r\n', upload[4]['encoded_body'])
        self.assertEqual(upload[4]['expected_status'], 201)
        self.assertEqual(self.client.calls[-1][0], 'GET')
        self.assertEqual(len({c[4]['deadline'] for c in self.client.calls}), 1)
        self.assertTrue(all(c[4]['max_response_bytes'] == 33554432 for c in self.client.calls))

    def test_multiple_image_video_rewrite_append_alt_and_body_file(self):
        movie = Path(self.tmp.name) / 'demo.mp4'; movie.write_bytes(b'video')
        body = Path(self.tmp.name) / 'body.md'; body.write_text('![Body alt](./a.png)\n\nDone')
        row = self.call('--body-file', str(body), '--attach', str(movie))
        self.assertEqual(row['body'], '![Body alt](' + metadata()['browser_download_url'] + ')\n\nDone\n\n' + metadata(2)['browser_download_url'])
        self.assertEqual(len(row['attachments']), 2)

    def test_existing_alt_wins_and_unreferenced_flag_alt_is_escaped(self):
        asset = prepare([str(self.file) + '#A ] [ B\nC'], time.monotonic() + 10)
        url = ['https://asset.invalid/a']
        self.assertEqual(render('', asset, url), '![A \\] \\[ B C](https://asset.invalid/a)')
        self.assertEqual(render(f'![Written]({self.file})', asset, url), '![Written](https://asset.invalid/a)')

    def test_offline_preview_no_requests_no_upload_urls_and_known_hash_size(self):
        self.client.dry_run = True
        row = self.call('--body', 'text')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(row['attachments'][0]['size'], 3)
        self.assertIn('attachment.invalid', row['preview_with_placeholder_urls'])
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--attach', str(self.file), '--dry-run'])
        with patch.object(Client, 'request', side_effect=AssertionError('network')):
            self.assertTrue(execute(args)['dry_run'])

    def test_invalid_modes_public_boundary_no_network(self):
        for flags in (['--reply-to', '1'], ['--path', 'x', '--line', '1'], ['--commit', 'a']):
            args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--attach', str(self.file), *flags])
            with patch('gitverse_cli.cli.Client', return_value=self.client), self.assertRaises(GVError): execute(args)
        self.client.backend = 'web'
        with self.assertRaises(GVError): self.call()
        self.assertEqual(self.client.calls, [])

    def test_upload_denials_and_unknown_transport_never_retry_keep_ids(self):
        failures = [GVError('denied', 'http_error', s) for s in (401, 403, 404, 422, 500)]
        failures += [GVError('timeout', 'network_error'), OSError('private')]
        for failure in failures:
            self.client = AttachedFake(); self.client.break_upload = failure
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.details['comment_id'], 99)
            self.assertEqual(caught.exception.details['stage'], 'upload_attachment')
            self.assertTrue(caught.exception.details['mutation_attempted'])
            self.assertEqual(len([c for c in self.client.calls if c[1].endswith('/attachments')]), 1)
            self.assertNotIn('private', str(caught.exception))

    def test_malformed_upload_keeps_raw_recovery_id_and_stops(self):
        original = self.client.request
        def request(method, path, *a, **kw):
            row = original(method, path, *a, **kw)
            return [{'id': 17}] if path.endswith('/attachments') else row
        self.client.request = request
        with self.assertRaises(GVError) as caught: self.call()
        self.assertEqual(caught.exception.details['returned_attachment_ids'], [17])
        self.assertFalse(any(c[0] == 'PATCH' for c in self.client.calls))

    def test_second_upload_failure_preserves_completed_first_attachment(self):
        second = Path(self.tmp.name) / 'b.png'; second.write_bytes(b'png')
        def hook(c, method, path):
            if method == 'POST' and path.endswith('/attachments') and c.uploads:
                raise GVError('timeout', 'network_error')
        self.client.hook = hook
        with self.assertRaises(GVError) as caught: self.call('--attach', str(second))
        self.assertEqual(caught.exception.details['comment_id'], 99)
        self.assertEqual(caught.exception.details['attachment_ids'], [1])
        self.assertEqual(caught.exception.details['attachment_index'], 1)
        self.assertEqual(len([c for c in self.client.calls if c[1].endswith('/attachments')]), 2)
        self.assertFalse(any(c[0] == 'PATCH' for c in self.client.calls))

    def test_upload_wrong_name_size_and_duplicate_ids_do_not_reach_patch(self):
        for change in ({'name': 'different.png'}, {'size': 200}, {'id': True}):
            self.client = AttachedFake(); original = self.client.request
            def request(method, path, *a, **kw):
                row = original(method, path, *a, **kw)
                if path.endswith('/attachments'): row[0].update(change)
                return row
            self.client.request = request
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.details['comment_id'], 99)
            self.assertFalse(any(c[0] == 'PATCH' for c in self.client.calls))

    def test_wrong_readback_author_parent_attachment_and_body_refuse(self):
        for change in ({'body': 'concurrent'}, {'user': {'id': 4}},
                       {'issue_url': 'https://evil.invalid/parent'}, {'attachments': []}):
            self.client = AttachedFake(); original = self.client.request
            def request(method, path, *a, **kw):
                row = original(method, path, *a, **kw)
                if method == 'GET' and self.client.uploads and '/issues/comments/' in path:
                    row.update(change)
                return row
            self.client.request = request
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.details['attachment_ids'], [1])
            self.assertFalse(any(c[0] == 'PATCH' for c in self.client.calls))

    def test_final_concurrent_noop_patch_and_unknown_patch_fail_closed(self):
        for mode in ('concurrent', 'ignored', 'unknown'):
            self.client = AttachedFake(); self.client.ignore = mode == 'ignored'
            gets = 0
            def hook(c, method, path):
                nonlocal gets
                if '/issues/comments/' in path and method == 'GET' and c.uploads:
                    gets += 1
                    if mode == 'concurrent' and gets == 2: c.rows[-1]['updated_at'] = 'other'
                if mode == 'unknown' and method == 'PATCH': raise OSError('private')
            self.client.hook = hook
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.details['comment_id'], 99)
            self.assertEqual(caught.exception.details['attachment_ids'], [1])
            self.assertLessEqual(len([c for c in self.client.calls if c[0] == 'PATCH']), 1)

    def test_size_deadline_partial_limits_during_upload_stop_with_recovery(self):
        for code in ('deadline_exceeded', 'response_limit', 'incomplete_response'):
            self.client = AttachedFake(); self.client.break_upload = GVError('bounded', code)
            with self.assertRaises(GVError) as caught: self.call()
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.details['attachment_index'], 0)

    def test_file_errors_duplicates_hash_filename_and_size_bounds(self):
        deadline = time.monotonic() + 10
        for values in ([''], ['-'], [str(self.file)] * 51, [str(self.file)] * 2, [str(self.file) + '\0']):
            with self.assertRaises(GVError): prepare(values, deadline)
        movie = Path(self.tmp.name) / 'a.mp4'; movie.write_bytes(b'v')
        with self.assertRaises(GVError): prepare([str(movie) + '#alt'], deadline)
        link = Path(self.tmp.name) / 'link.png'; link.symlink_to(self.file)
        with self.assertRaises(GVError): prepare([str(link)], deadline)
        empty = Path(self.tmp.name) / 'empty.png'; empty.touch()
        with self.assertRaises(GVError): prepare([str(empty)], deadline)
        fifo = Path(self.tmp.name) / 'fifo.png'; os.mkfifo(fifo)
        with self.assertRaises(GVError): prepare([str(fifo)], deadline)
        hashed = Path(self.tmp.name) / 'a#b.png'; hashed.write_bytes(b'x')
        self.assertEqual(prepare([str(hashed) + '#alt#text'], deadline)[0]['alt'], 'alt#text')
        with patch('gitverse_cli.comment_attachments.TOTAL', 2), self.assertRaises(GVError): prepare([str(self.file)], deadline)
        with self.assertRaises(GVError): prepare([str(self.file)], time.monotonic() - 1)

    def test_malformed_metadata_identity_and_urls(self):
        for change in ({'id': True}, {'uuid': 'bad'}, {'size': True}, {'name': None},
                       {'browser_download_url': 'https://user:pass@evil.invalid/a'},
                       {'browser_download_url': 'http://evil.invalid/a'},
                       {'browser_download_url': 'https://evil.invalid/a)![inject](x'}):
            with self.assertRaises(GVError): asset_value({**metadata(), **change})
        for attachments in (None, [metadata(), metadata()], [metadata()] * 51):
            with self.assertRaises(GVError): attachment_list({'attachments': attachments})

    def test_real_transport_upload_redirect_never_forwards_pat_or_cookies(self):
        import json
        import urllib.error
        class Response(io.BytesIO): status = 200
        c = Client(dry_run=True); c.dry_run = False
        c.headers['Authorization'] = 'Bearer synthetic'
        c.opener = Mock()
        row = self.client.row(99, 3); row.update(body='text', attachments=[])
        data = [self.client.parent, {'id': 3, 'login': 'me'}, self.client.parent, row, row]
        c.opener.open.side_effect = [Response(json.dumps(r).encode()) for r in data] + [
            urllib.error.HTTPError(c.base, 302, 'refused', {'Location': 'https://evil.invalid/upload'}, None)]
        args = parser().parse_args(['pr', 'comment', '9', '-R', 'o/r', '--body', 'text', '--attach', str(self.file)])
        args.number = 9
        with self.assertRaises(GVError) as caught: discussion_comment(c, 'o/r', args)
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(caught.exception.details['comment_id'], 99)
        self.assertEqual(c.opener.open.call_count, 6)
        for call in c.opener.open.call_args_list:
            request = call.args[0]
            self.assertTrue(request.full_url.startswith(c.base + '/'))
            self.assertIsNone(request.get_header('Cookie'))

    def test_code_references_percent_paths_titles_definition_and_video(self):
        assets = prepare([str(self.file)], time.monotonic() + 10)
        url = ['https://asset.invalid/a']
        md = f'`![literal]({self.file})`\n\n```\n![literal]({self.file})\n```\n\n![yes]({self.file} "title")'
        result = render(md, assets, url)
        self.assertEqual(result.count('https://asset.invalid/a'), 1)
        self.assertIn('![yes](https://asset.invalid/a "title")', result)
        self.assertEqual(render('[a]: <./a.png> "title"\n![alt][a]', assets, url, str(self.file.parent/'body.md')), '[a]: https://asset.invalid/a "title"\n![alt][a]')
        movie = Path(self.tmp.name) / 'a.mp4'; movie.write_bytes(b'v')
        assets = prepare([str(movie)], time.monotonic() + 10)
        self.assertEqual(render(f'![demo]({movie})', assets, url), url[0])
        self.assertEqual(render(f'Inline ![demo]({movie}) text', assets, url), f'Inline [demo]({url[0]}) text')

    def movie_assets(self):
        movie = Path(self.tmp.name) / 'a.mp4'
        movie.write_bytes(b'video')
        return prepare([str(movie)], time.monotonic() + 10)

    def test_video_reference_links_full_collapsed_shortcut_and_forward_definition(self):
        assets = self.movie_assets()
        for usage in ('[Watch][clip]', '[clip][]', '[clip]', '[Watch][CLIP]'):
            text = usage + '\n\n[clip]: <./a.mp4> "Title"\n'
            self.assertEqual(render(text, assets, ['https://asset.invalid/v'], str(self.file)),
                             usage + '\n\n[clip]: https://asset.invalid/v "Title"\n')

    def test_video_reference_images_refuse_before_any_network_including_dry_run(self):
        movie = self.movie_assets()[0]['path']
        for usage in ('![Alt][clip]', '![clip][]', '![clip]'):
            for dry in (False, True):
                self.client.dry_run = dry
                with self.assertRaises(GVError) as caught:
                    self.call('--attach', movie, '--body', usage + f'\n\n[clip]: {movie}')
                self.assertEqual(caught.exception.code, 'usage_error')
                self.assertEqual(self.client.calls, [])

    def test_unused_video_definition_is_not_an_embed_and_appends(self):
        assets = self.movie_assets()
        text = '[unused]: ./a.mp4\n'
        self.assertEqual(render(text, assets, ['https://asset.invalid/v'], str(self.file)),
                         text.rstrip() + '\n\nhttps://asset.invalid/v')

    def test_video_reference_link_runs_complete_upload_and_verified_patch_pipeline(self):
        movie = self.movie_assets()[0]['path']
        text = '[Watch][clip]\n\n[clip]: ' + movie
        row = self.call('--attach', movie, '--body', text)
        self.assertEqual(row['body'], '[Watch][clip]\n\n[clip]: ' + metadata(2)['browser_download_url']
                         + '\n\n![a](' + metadata()['browser_download_url'] + ')')
        self.assertEqual(len(row['attachments']), 2)
        self.assertEqual(self.client.calls[-1][0], 'GET')

    def test_reference_labels_first_definition_casefold_whitespace_and_body_alt(self):
        assets = prepare([str(self.file)], time.monotonic() + 10)
        text = '![Body alt][A  B]\n\n[a b]: ./a.png\n[A B]: ./other.png\n'
        self.assertEqual(render(text, assets, ['https://asset.invalid/a'], str(self.file)),
                         '![Body alt][A  B]\n\n[a b]: https://asset.invalid/a\n[A B]: ./other.png\n')

    def test_video_literal_reference_images_in_code_and_escapes_do_not_refuse(self):
        assets = self.movie_assets()
        for literal in ('`![clip]`', '!\\[clip]', '    ![clip]', '~~~\n![clip]\n~~~'):
            text = literal + '\n\n[clip]: ./a.mp4'
            result = render(text, assets, ['https://asset.invalid/v'], str(self.file))
            self.assertEqual(result, text + '\n\nhttps://asset.invalid/v')
        # Escaping only ! leaves an ordinary reference link after the literal !.
        self.assertEqual(render('\\![clip]\n\n[clip]: ./a.mp4', assets,
                                ['https://asset.invalid/v'], str(self.file)),
                         '\\![clip]\n\n[clip]: https://asset.invalid/v')

    def test_video_line_in_multiline_paragraph_is_link_and_empty_alt_has_fallback(self):
        assets = self.movie_assets()
        for text in ('Before\n![](./a.mp4)', '![](./a.mp4)\nAfter'):
            self.assertEqual(render(text, assets, ['https://asset.invalid/v'], str(self.file)),
                             text.replace('![](./a.mp4)', '[a](https://asset.invalid/v)'))

    def test_literal_percent_filename_wins_over_decoding_and_invalid_url_stays_literal(self):
        literal = Path(self.tmp.name) / 'a%20b.png'; literal.write_bytes(b'x')
        decoded = Path(self.tmp.name) / 'a b.png'; decoded.write_bytes(b'y')
        assets = prepare([str(literal), str(decoded)], time.monotonic() + 10)
        text = '![Written](./a%20b.png)'
        result = render(text, assets, ['https://asset.invalid/literal', 'https://asset.invalid/decoded'], str(self.file))
        self.assertEqual(result, '![Written](https://asset.invalid/literal)\n\n![a b](https://asset.invalid/decoded)')
        self.assertIn('![x](https://[bad/path)', render('![x](https://[bad/path)', assets,
                      ['https://asset.invalid/literal', 'https://asset.invalid/decoded']))

    def rendered(self, text, video=False):
        assets = self.movie_assets() if video else prepare([str(self.file)], time.monotonic() + 10)
        return render(text, assets, ['https://asset.invalid/a'], str(self.file))

    def test_nested_labels_preserve_exact_alt_formatting_and_title(self):
        text = '![Outer [inner] **bold**](./a.png "title")'
        self.assertEqual(self.rendered(text), text.replace('./a.png', 'https://asset.invalid/a'))
        video = 'Before ![Outer [inner] **bold**](./a.mp4 "title") after'
        self.assertEqual(self.rendered(video, video=True),
                         video.replace('![Outer', '[Outer').replace('./a.mp4', 'https://asset.invalid/a'))

    def test_multiline_code_spans_exact_delimiter_runs_stay_literal(self):
        for text in ('`first\n![clip](./a.mp4)\nlast`',
                     '``first `\n![clip](./a.mp4)\nlast``',
                     '`first\n![clip]\nlast`\n\n[clip]: ./a.mp4'):
            self.assertEqual(self.rendered(text, video=True), text + '\n\nhttps://asset.invalid/a')

    def test_definitions_inside_multiline_code_spans_are_not_used(self):
        text = '`first\n[clip]: ./a.mp4\nlast`\n\n![clip]'
        self.assertEqual(self.rendered(text, video=True), text + '\n\nhttps://asset.invalid/a')

    def test_unmatched_backtick_and_different_length_runs_do_not_hide_references(self):
        for text in ('`first\n![alt](./a.png)', '``first ` ![alt](./a.png)'):
            self.assertEqual(self.rendered(text), text.replace('./a.png', 'https://asset.invalid/a'))

    def test_multiline_destination_definition_and_title_keep_formatting(self):
        for text in ('[clip]:\n  <./a.png> "title"\n\n![Body alt][clip]',
                     '[clip]: ./a.png\n  "two\nline title"\n\n![Body alt][clip]'):
            self.assertEqual(self.rendered(text), text.replace('<./a.png>', 'https://asset.invalid/a')
                             .replace('./a.png', 'https://asset.invalid/a'))

    def test_quote_and_list_fences_keep_literal_video_reference_images(self):
        for prefix in ('> ', '> > ', '- '):
            continuation = prefix if prefix != '- ' else '  '
            text = prefix + '```\n' + continuation + '![clip]\n' + continuation + '```\n\n[clip]: ./a.mp4'
            self.assertEqual(self.rendered(text, video=True), text + '\n\nhttps://asset.invalid/a')

    def test_quote_and_list_container_references_rewrite_preserving_markers(self):
        for text in ('> ![Outer [inner]](./a.png)', '- ![Outer [inner]](./a.png)',
                     '> ![Body][clip]\n>\n> [clip]:\n>   <./a.png>'):
            self.assertEqual(self.rendered(text), text.replace('<./a.png>', 'https://asset.invalid/a')
                             .replace('./a.png', 'https://asset.invalid/a'))

    def test_fence_ends_when_its_container_ends(self):
        for text in ('> ```\n> ![literal](./a.png)\n\n![real](./a.png)',
                     '- ```\n  ![literal](./a.png)\n\n![real](./a.png)'):
            self.assertEqual(self.rendered(text), text.replace('![real](./a.png)',
                                                              '![real](https://asset.invalid/a)'))

    def test_raw_html_code_blocks_do_not_rewrite_or_refuse(self):
        for text in ('<pre>\n![clip]\n</pre>\n\n[clip]: ./a.mp4',
                     '<!--\n![clip]\n-->\n\n[clip]: ./a.mp4'):
            self.assertEqual(self.rendered(text, video=True), text + '\n\nhttps://asset.invalid/a')

    def test_nested_video_reference_image_fails_before_comment_creation(self):
        movie = self.movie_assets()[0]['path']
        for text in ('![Outer [inner]][clip]\n\n[clip]:\n  ' + movie,
                     '> ![Outer [inner]][clip]\n>\n> [clip]: ' + movie):
            with self.assertRaises(GVError):
                self.call('--attach', movie, '--body', text)
            self.assertEqual(self.client.calls, [])

    def test_video_degraded_link_does_not_reform_image_with_preceding_bang(self):
        text = 'Before !![demo](./a.mp4) after'
        self.assertEqual(self.rendered(text, video=True), 'Before \\![demo](https://asset.invalid/a) after')

    def test_even_backslashes_before_video_image_leave_embed_active(self):
        text = r'Before \\![demo](./a.mp4) after'
        self.assertEqual(self.rendered(text, video=True), r'Before \\[demo](https://asset.invalid/a) after')

    def test_markdown_nesting_limits_refuse_offline_before_any_network(self):
        for text in ('![' + '[' * 128 + 'x' + ']' * 128 + '](./a.png)',
                     '![x](' + '(' * 33 + 'x' + ')' * 33 + ')'):
            with self.assertRaises(GVError) as caught:
                self.call('--body', text)
            self.assertEqual(caught.exception.code, 'usage_error')
            self.assertEqual(self.client.calls, [])


if __name__ == '__main__': unittest.main()
