import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import itertools
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zipfile import ZipFile

import citation_experiment as experiment
from pilot_evidence import excerpt_ranges
from rovo_read_pilot import Sources, attempt, final_answer, source_site

DOC = 'confluence:dsid_fixture'
BODY = 'The gate opens at 10:00. The operator is Ada.'
TASK = {'id': 'qst_0150', 'query': 'When does the gate open and who is the operator?', 'cohort': 'diagnostic'}
SITE = 'private-fixture.atlassian.net'
PAGE = '123456789'


def model_client(variant, *, repeat=True, usage=True):
    claims = [{'part': 1, 'text': 'It opens at 10:00.', 'source': 'S1'},
              {'part': 2, 'text': 'The operator is Ada.', 'source': 'S1'}][:2 if repeat else 1]
    if variant == 'A':
        for claim, quote in zip(claims, ['10:00', 'Ada']):
            claim['quote'] = quote
    steps = iter([('search', {'query': 'gate', 'parts': ['opening time', 'operator']}),
                  ('read', {'doc': DOC}), ('finish', {'claims': claims, 'missing': []})])
    requests = []

    async def post(url, *, json):
        requests.append({**json, 'messages': list(json['messages'])})
        name, args = next(steps)
        message = {'role': 'assistant', 'tool_calls': [{'id': str(len(requests)), 'function': {
            'name': name, 'arguments': dumps(args)}}]}
        response = {'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}
        if usage:
            response['usage'] = {'prompt_tokens': 7, 'completion_tokens': 2}
        return Mock(json=lambda: response)

    return Mock(post=AsyncMock(side_effect=post)), requests


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


class ValidationTests(unittest.TestCase):
    def test_balanced_order(self):
        tasks = [{'id': f'q{i}'} for i in range(32)]
        plan = experiment.schedule(tasks)
        self.assertEqual(len(set(plan)), 128)
        self.assertEqual([cell for _, cell in plan[:4]], [cell for _, cell in plan[4:8]][::-1])
        for base in (0, 64):
            rows = list(itertools.batched([cell for _, cell in plan[base:base + 64]], 4))
            for column in zip(*rows):
                self.assertEqual(Counter(column), dict.fromkeys(experiment.CELLS, 4))
            self.assertEqual(Counter(pair for row in rows for pair in itertools.pairwise(row)),
                             dict.fromkeys(itertools.permutations(experiment.CELLS, 2), 4))

    def test_identical_visible_windows_include_precise_character_spans(self):
        text = '  ' + ('é α words\n' * 70) + ('x' * 505)
        chunks = list(excerpt_ranges(text))
        self.assertTrue(all(0 < end - start <= 400 for start, end in chunks))
        self.assertEqual(''.join(text[start:end].split()[i] for start, end in chunks
                                 for i in range(len(text[start:end].split()))), ''.join(text.split()))
        self.assertEqual(chunks, sorted(set(chunks)))

    def test_exact_quote_and_common_validation(self):
        excerpt = {**{'text': 'header\n`X-Gate` and Ada', 'doc': DOC, 'url': 'https://example.test'}, 'id': 'S1'}
        evidence = {'S1': excerpt}
        statement = {'source': 'S1', 'part': 1, 'text': 'Use `X-Gate`.'}
        def validate(variant, **changes):
            return final_answer({'claims': [{**statement, **changes}], 'missing': []}, evidence,
                                parts=['header'], variant=variant)
        self.assertIn('`X-Gate` and Ada', validate('A', quote='header\n`X-Gate`'))
        self.assertIn('`X-Gate` and Ada', validate('B'))
        for variant, changes, reason in (
            ('A', {'quote': 'header `X-Gate`'}, 'copy_exact_quote_from_source'),
            ('A', {'quote': 'Ada'}, 'copy_literal_from_quote'),
            ('B', {'text': 'Use `X-Unknown`.'}, 'copy_literal_from_quote'),
            ('B', {'quote': 'Ada'}, 'supply_excerpt_id_only'),
            ('B', {'source': 'S9'}, 'cite_read_ids'),
            ('B', {'part': True}, 'use_requested_part_number'),
            ('B', {'text': 'See [extra](https://example.test)'}, 'put_citations_in_sources'),
        ):
            with self.subTest(variant=variant, changes=changes), self.assertRaisesRegex(ValueError, reason):
                validate(variant, **changes)

    def test_resume_rejects_changed_settings_and_parallel_writer(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            self.assertEqual(experiment.checkpoint(folder, {'version': 1}, False), {})
            self.assertEqual(experiment.checkpoint(folder, {'version': 1}, True), {})
            with self.assertRaisesRegex(ValueError, 'differ'):
                experiment.checkpoint(folder, {'version': 2}, True)
            with experiment.writer(folder), self.assertRaisesRegex(ValueError, 'already holds'):
                with experiment.writer(folder):
                    pass


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_four_attempts_pass_offline_and_live_binding_checks(self):
        cloud_url = f'https://{SITE}/wiki/spaces/DEMO/pages/{PAGE}'
        cloud = Mock(read=AsyncMock(return_value={'id': PAGE, 'body': {'value': '<p>' + BODY + '</p>'}}),
                     url=Mock(return_value=cloud_url))
        hit = {'doc': DOC, 'title': 'Fixture', 'snippet': 'gate'}
        manifest = {'Fixture': {'xerj_id': DOC, 'page_title': 'Fixture', 'archive_path': 'fixture.txt'}}
        inventory = {'site': SITE, 'space': 'DEMO', 'pages': {DOC: PAGE}}
        tasks = [TASK]
        plan = experiment.schedule(tasks)
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            archive_path = folder / 'corpus.zip'
            manifest_path = folder / 'pages.csv'
            manifest_path.write_text('fixture')
            with ZipFile(archive_path, 'w') as archive:
                archive.writestr('fixture.txt', BODY)
            experiment.save(folder / 'tasks.json', tasks)
            experiment.save(folder / 'inventory.json', inventory)
            experiment.save(folder / 'settings.json', {
                'manifest_sha256': experiment.digest(manifest_path),
                'inventory_sha256': experiment.digest(folder / 'inventory.json'),
                'tasks_sha256': experiment.digest(folder / 'tasks.json'),
            })
            results = {}
            requests = {}
            with source_site(folder / 'sources') as origin:
                for sequence, (task, cell) in enumerate(plan, 1):
                    arm, variant = cell.split('_')
                    model, requests[cell] = model_client(variant, usage=(variant == 'A'))
                    sources = Sources(manifest, Mock(cloud=cloud), inventory, experiment.INDEX,
                                      folder / 'sources', origin, citation_excerpts=True)
                    sources.search = AsyncMock(return_value=[hit])
                    sources.local_body = AsyncMock(return_value=BODY)
                    result = await attempt(model, None, sources, arm, TASK, variant=variant)
                    self.assertIsNone(result['error'])
                    self.assertEqual(result['usage'][0], {'prompt_tokens': 7, 'completion_tokens': 2}
                                     if variant == 'A' else None)
                    self.assertEqual([ref['quote'] for ref in result['selected_excerpts']],
                                     ['10:00', 'Ada'] if variant == 'A' else [BODY, BODY])
                    self.assertIn('> ' + BODY, result['answer'])
                    self.assertEqual(result['cited']['S1']['id'], 'S1')
                    result['execution'] = {'sequence': sequence, 'planned_position': sequence,
                                           'order_deviation': False, 'expected_next': [task, cell]}
                    results[cell] = result
            for arm in ('xerj', 'rovo'):
                self.assertEqual(requests[arm + '_A'][-1]['messages'][-1],
                                 requests[arm + '_B'][-1]['messages'][-1])
            rows = {TASK['id']: results}
            experiment.save(folder / 'attempts.json', rows)
            experiment.blind_export(folder, rows, plan, inventory)
            blind = experiment.load(folder / 'blind.json')
            key = experiment.load(folder / 'blind-key.json')
            for label, candidate in blind.items():
                variant = key[label]['arm'][-1]
                self.assertEqual([c['quote'] for c in candidate['claims']],
                                 ['10:00', 'Ada'] if variant == 'A' else [BODY, BODY])
                self.assertNotIn(SITE, dumps(candidate))
                self.assertNotIn('127.0.0.1', dumps(candidate))
                self.assertNotIn('arm', candidate)
            with (patch.object(experiment, 'MANIFEST', manifest_path), patch.object(experiment, 'ARCHIVE', archive_path),
                  patch.object(experiment, 'rovo_client', return_value=Mock(cloud=cloud))):
                self.assertEqual(await experiment.verify_bindings(folder, tasks, manifest, inventory), 0)
                self.assertEqual(await experiment.verify_bindings(folder, tasks, manifest, inventory, online=True), 0)
                check = experiment.load(folder / 'bindings-online.json')
                for cell in results:
                    self.assertEqual(check[TASK['id']][cell]['sources']['S1'], {'offline': True, 'online': True})
                results['xerj_A']['cited']['S1']['start'] = 1
                experiment.save(folder / 'attempts.json', rows)
                self.assertEqual(await experiment.verify_bindings(folder, tasks, manifest, inventory), 1)

    async def test_identical_text_in_two_documents_has_separate_labels(self):
        other = 'confluence:dsid_other'
        client, _ = model_client('B', repeat=False)
        post = client.post.side_effect

        async def both(url, *, json):
            reply = await post(url, json=json)
            message = reply.json()['choices'][0]['message']
            if message['tool_calls'][0]['function']['name'] == 'read':
                message['tool_calls'].append({'id': 'second-read', 'function': {
                    'name': 'read', 'arguments': dumps({'doc': other})}})
            return reply

        def read(arm, doc, query, cursor):
            return {'doc': doc, 'references': [{'block': 0, 'start': 0, 'end': len(BODY), 'text': BODY,
                                                'url': f'https://example.test/{doc}'}]}

        result = await attempt(Mock(post=AsyncMock(side_effect=both)), None,
                               Mock(search=AsyncMock(return_value=[{'doc': DOC}, {'doc': other}]),
                                    read=AsyncMock(side_effect=read)), 'rovo', TASK, variant='B')
        reads = [turn['result']['references'][0] for turn in result['trace'] if turn['tool'] == 'read']
        self.assertEqual([ref['id'] for ref in reads], ['S1', 'S2'])

    async def test_cloud_outage_preserves_local_cells_and_resume_records_order(self):
        manifest = {'Fixture': {'xerj_id': DOC, 'page_title': experiment.CONTROL}}
        inventory = {'site': SITE}
        tasks = [TASK]
        outage = True

        @asynccontextmanager
        async def local(*args, **kwargs):
            yield None

        async def probe(sources, local, arm, control):
            if arm == 'rovo' and outage:
                raise experiment.InfrastructureError('rovo', 'rate_limited')

        def result(*args, **kwargs):
            return {'answer': '', 'requested_parts': [], 'submission': None, 'error': 'TimeoutError',
                    'ms': 180000, 'calls': {}, 'usage': [None], 'model_request_ms': [180000], 'trace': [],
                    'cited': {}, 'selected_excerpts': [], 'termination': 'TimeoutError', 'infrastructure_error': None}

        with TemporaryDirectory() as directory:
            folder = Path(directory)
            with (patch.object(experiment, 'local_session', side_effect=local),
                  patch.object(experiment, 'probe', side_effect=probe),
                  patch.object(experiment, 'attempt', side_effect=result) as mocked,
                  patch.object(experiment, 'rovo_client'), patch.object(experiment, 'fingerprint', return_value={})):
                args = SimpleNamespace(resume=False, platform='all', allow_order_deviation=True)
                self.assertEqual(await experiment.run(folder, tasks, manifest, inventory, args), 2)
                rows = experiment.load(folder / 'attempts.json')
                self.assertEqual(set(rows[TASK['id']]), {'xerj_A', 'xerj_B'})
                self.assertTrue(rows[TASK['id']]['xerj_B']['execution']['order_deviation'])
                self.assertEqual(experiment.load(folder / 'infrastructure.json')[0]['reason'], 'rate_limited')
                outage = False
                args.resume, args.platform = True, 'rovo'
                self.assertEqual(await experiment.run(folder, tasks, manifest, inventory, args), 0)
                self.assertEqual(mocked.await_count, 4)
                rows = experiment.load(folder / 'attempts.json')
                self.assertEqual(len(experiment.completed(rows, experiment.schedule(tasks))), 4)
