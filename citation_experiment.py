"""Execute, blind, and verify the citation A/B experiment against the live paired stand."""
import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
from html import escape
import json
from pathlib import Path
import random
import re
from urllib.parse import urlsplit
from zipfile import ZipFile

import httpx

from corpus import STATE
from fetch_assets import digest
from pilot_evidence import blocks, excerpt_ranges
from retrieval import XERJ, json_request
from retry_cloud_search import read_manifest
from rovo_benchmark import CONTROL, SITE, SPACE, SPACE_ID, Rovo
from rovo_read_pilot import (
    EXPERIMENT_FINISH, EXPERIMENT_PROMPTS, FIRST_SEARCH, InfrastructureError, LLAMA, LLM,
    MAX_READS, MAX_SEARCHES, READ, SEARCH, TIMEOUT, Sources, asks_for_document_count, attempt,
    cloud_blocks, cloud_reference, evidence_call, excerpt_anchor, final_answer, infrastructure_reason,
    model_runtime, read_source_html, selected_excerpts, source_site, verify_index,
)
from scale_xerj import ARCHIVE, SHA256, save, verify_archive
from stage1_benchmark import local_call, local_session

FOLDER = Path('.state/citation-ab')
MANIFEST = Path('benchmarks/pages.csv')
INDEX = 'rovo-paired-512'
CELLS = ('xerj_A', 'rovo_B', 'rovo_A', 'xerj_B')
MODEL_WEIGHT = '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'
REQUIRED_RESULT = {'answer', 'requested_parts', 'submission', 'error', 'ms', 'calls', 'usage',
                   'model_request_ms', 'trace', 'cited', 'selected_excerpts', 'termination', 'execution'}
SOURCE_FILES = (
    'citation_experiment.py', 'rovo_read_pilot.py', 'pilot_evidence.py', 'rovo_benchmark.py',
    'atlassian_client.py', 'atlassian_oauth.py', 'stage1_benchmark.py', 'wiki_mcp.py',
    'retrieval.py', 'lexical_wiki.py', 'wiki.py', 'materials.py', 'services.py', 'retry_cloud_search.py',
)


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_tasks(path):
    tasks = load(path)
    if not isinstance(tasks, list) or len(tasks) != 32:
        raise ValueError('Expected 32 frozen tasks')
    if any(not isinstance(row, dict) or set(row) != {'id', 'query', 'cohort'}
           or not isinstance(row['id'], str) or not re.fullmatch(r'qst_\d+', row['id'])
           or not isinstance(row['query'], str) or not row['query'].strip() for row in tasks):
        raise ValueError('Expected task objects with id, query and cohort only')
    if (len({row['id'] for row in tasks}) != len(tasks)
            or [row['id'] for row in tasks[:2]] != ['qst_0150', 'qst_0259']
            or [row['cohort'] for row in tasks] != ['diagnostic'] * 16 + ['holdout'] * 16):
        raise ValueError('Unexpected frozen cohort or task order')
    return tasks


def schedule(tasks):
    # Williams design: each cell occupies every position and precedes every other cell equally.
    orders = [[CELLS[(offset + position) % 4] for position in (0, 1, 3, 2)] for offset in (0, 2, 1, 3)]
    return [(task['id'], cell) for i, task in enumerate(tasks) for cell in orders[i % len(orders)]]


def corpus_inputs(folder):
    manifest = read_manifest(MANIFEST)
    inventory = load(folder / 'inventory.json')
    pages, gold = inventory['pages'], inventory['gold_reads']
    gold_ids = {row['xerj_id'] for row in manifest.values() if row['is_gold'].lower() == 'true'}
    if (len(manifest) != 512 or set(pages) != {row['xerj_id'] for row in manifest.values()}
            or any(not isinstance(value, str) or not value.isdigit() for value in pages.values())
            or len(set(pages.values())) != len(pages) or inventory.get('archive_sha256') != SHA256
            or inventory.get('verified') is not True or set(gold) != gold_ids
            or any(ref['text_matches'] is not True or str(ref['page']) != pages[doc]
                   for doc, ref in gold.items())):
        raise ValueError('Inventory must match all 512 documents and verify the reference texts')
    return manifest, inventory


def rovo_client(folder, inventory):
    if ((SITE, SPACE) != (inventory['site'], inventory['space']) or
            {str(ref['space_id']) for ref in inventory['gold_reads'].values()} != {SPACE_ID}):
        raise ValueError('Source .state/citation-ab/env.sh and check the frozen cloud scope')
    return Rovo(folder / 'atl.sock', inventory)


def fingerprint(folder, tasks, plan, manifest):
    verify_archive()
    verify_index(INDEX, manifest)
    model = model_runtime('Qwen3-4B-Q4_K_M.gguf')
    props = json_request(LLAMA.split('/v1/')[0] + '/props', timeout=5)
    params = props['default_generation_settings']['params']
    if (model['model_sha256'] != MODEL_WEIGHT or props['total_slots'] != 1
            or params['reasoning_format'] != 'none' or params['reasoning_in_content']):
        raise ValueError('Expected the frozen weights, reasoning off, and one model slot')
    index_config = {name: json_request(f'{XERJ}/{INDEX}/_{name}', timeout=5)[INDEX]
                    for name in ('mapping', 'settings')}
    # These fields describe creation, while the manifest/archive and verify_index bind content.
    index_config['settings']['settings']['index'].pop('creation_date', None)
    index_config['settings']['settings']['index'].pop('uuid', None)
    prompts = {'system': EXPERIMENT_PROMPTS, 'finish': EXPERIMENT_FINISH,
               'first_search': FIRST_SEARCH, 'search': SEARCH, 'read': READ}
    return {
        'format': 1, 'tasks': [task['id'] for task in tasks], 'schedule': [list(row) for row in plan],
        'tasks_sha256': digest(folder / 'tasks.json'), 'manifest_sha256': digest(MANIFEST),
        'inventory_sha256': digest(folder / 'inventory.json'), 'archive_sha256': SHA256,
        'sources_sha256': {name: digest(Path(__file__).with_name(name)) for name in SOURCE_FILES},
        'prompts_sha256': hashlib.sha256(json.dumps(prompts, sort_keys=True).encode()).hexdigest(),
        'index': INDEX, 'index_config': index_config,
        'xerj_sha256': digest(STATE / 'bin' / 'xerj'),
        'runtime': {**model, 'slots': props['total_slots'], 'generation_defaults': params},
        'request_settings': {**LLM, 'max_tokens': 1000, 'tool_choice': 'required'},
        'max_searches': MAX_SEARCHES, 'max_reads': MAX_READS, 'timeout_s': TIMEOUT,
    }


@contextmanager
def writer(folder):
    with (folder / 'run.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('An experiment writer already holds the checkpoint lock') from exc
        yield


def checkpoint(folder, settings, resume):
    path = folder / 'settings.json'
    output = folder / 'attempts.json'
    if resume:
        if not path.exists() or load(path) != settings:
            raise ValueError('Checkpoint inputs or runtime differ from the frozen settings')
    elif path.exists() or output.exists():
        raise ValueError('The run already exists. Use --resume')
    else:
        save(path, settings)
    return load(output) if output.exists() else {}


def completed(rows, plan):
    positions = {pair: i for i, pair in enumerate(plan, 1)}
    ordered = {}
    for task, cells in rows.items():
        if not isinstance(cells, dict) or not cells:
            raise ValueError('Invalid checkpoint task')
        for cell, result in cells.items():
            pair = (task, cell)
            if (pair not in positions or not isinstance(result, dict) or not REQUIRED_RESULT <= result.keys()
                    or result.get('infrastructure_error')):
                raise ValueError('Invalid checkpoint cell')
            recorded = result['execution']
            sequence = recorded['sequence']
            if (type(sequence) is not int or sequence in ordered
                    or recorded['planned_position'] != positions[pair]):
                raise ValueError('Invalid checkpoint sequence')
            ordered[sequence] = pair
    done, pending = set(), iter(plan)
    expected = next(pending, None)
    for number in range(1, len(ordered) + 1):
        if number not in ordered:
            raise ValueError('Checkpoint execution sequence has gaps')
        task, cell = pair = ordered[number]
        recorded = rows[task][cell]['execution']
        if (recorded['expected_next'] != list(expected)
                or recorded['order_deviation'] is not (pair != expected or number != positions[pair])):
            raise ValueError('Checkpoint order metadata is inconsistent')
        done.add(pair)
        while expected in done:
            expected = next(pending, None)
    return done


def blind_export(folder, rows, plan, inventory):
    path = folder / 'blind-key.json'
    if path.exists():
        key = load(path)
        if (len(key) != len(plan) or {(ref['task'], ref['arm']) for ref in key.values()} != set(plan)):
            raise ValueError('The blind key does not match the frozen schedule')
    else:
        shuffled = list(plan)
        random.SystemRandom().shuffle(shuffled)
        key = {f'A{i:03}': {'task': task, 'arm': cell} for i, (task, cell) in enumerate(shuffled, 1)}
        save(path, key)
    private_url = re.compile(r'https?://' + re.escape(inventory['site']) + r'[^\s)`]*', re.I)

    def redact(text):
        return private_url.sub('[private address]', text)

    candidates = {}
    for label, slot in key.items():
        result = rows.get(slot['task'], {}).get(slot['arm'])
        if result is None:
            continue
        answer = result['answer']
        for source, ref in result['cited'].items():
            answer = answer.replace(f'[{source}]({ref["url"]})', f'[{source}]')
        claims = (result['submission'] or {}).get('claims', [])
        candidates[label] = {'task': slot['task'], 'answer': redact(answer),
                             'sources': {key: redact(ref['text']) for key, ref in result['cited'].items()},
                             'error': 'no_submission' if result['error'] else None,
                             'parts': [redact(part) for part in result['requested_parts']],
                             'claims': [{'part': claim['part'], 'text': redact(claim['text']),
                                         'source': claim['source'], 'quote': redact(ref['quote'])}
                                        for claim, ref in zip(claims, result['selected_excerpts'], strict=True)]}
    save(folder / 'blind.json', candidates)


def record_infrastructure(folder, entry):
    path = folder / 'infrastructure.json'
    events = load(path) if path.exists() else []
    save(path, [*events, {**entry, 'at': utc_now()}])


async def probe(sources, local, arm, control):
    async with asyncio.timeout(45):
        hits = await evidence_call('A', arm, sources.search, local, arm, CONTROL)
        if not any(hits):
            raise InfrastructureError(arm, 'no_mapped_semantic_results' if arm == 'rovo' else 'no_corpus_hits')
        read = await evidence_call('A', arm, sources.read, arm, control, CONTROL)
        if not any(ref['url'] for ref in read['references']):
            raise InfrastructureError(arm, 'control_has_no_citable_excerpt')


async def run(folder, tasks, manifest, inventory, args):
    plan = schedule(tasks)
    by_id = {task['id']: task for task in tasks}
    with writer(folder):
        settings = fingerprint(folder, tasks, plan, manifest)
        rows = checkpoint(folder, settings, args.resume)
        done = completed(rows, plan)
        blind_export(folder, rows, plan, inventory)
        control = next(row['xerj_id'] for row in manifest.values() if row['page_title'].startswith(CONTROL))
        disabled, checked = set(), set()
        if args.platform != 'all':
            disabled.add('rovo' if args.platform == 'xerj' else 'xerj')
        rovo = rovo_client(folder, inventory) if 'rovo' not in disabled else None
        async with httpx.AsyncClient(trust_env=False, timeout=None) as client:
            with source_site(folder / 'sources') as origin:
                for position, (task, cell) in enumerate(plan, 1):
                    if (task, cell) in done:
                        continue
                    arm, variant = cell.split('_')
                    if arm in disabled:
                        continue
                    expected = next(pair for pair in plan if pair not in done)
                    out_of_order = (task, cell) != expected or position != len(done) + 1
                    if out_of_order and not args.allow_order_deviation:
                        raise ValueError('The next cell would deviate from the frozen order. See --allow-order-deviation')
                    sources = Sources(manifest, rovo, inventory, INDEX, folder / 'sources', origin,
                                      citation_excerpts=True)
                    try:
                        if arm not in checked:
                            if arm == 'xerj':
                                async with local_session(folder / 'benchmark.sqlite', index=INDEX) as local:
                                    await probe(sources, local, arm, control)
                            else:
                                await probe(sources, None, arm, control)
                            checked.add(arm)
                            sources.local_text.clear()
                            sources.cloud_parts.clear()
                            sources.cloud_snapshots.clear()
                    except (InfrastructureError, TimeoutError) as exc:
                        reason = exc.reason if isinstance(exc, InfrastructureError) else 'probe_timeout'
                        record_infrastructure(folder, {'task': task, 'arm': cell, 'stage': 'preflight',
                                                        'service': arm, 'reason': reason})
                        if arm == 'rovo' and args.allow_order_deviation:
                            print(f'Rovo deferred ({reason})', flush=True)
                            disabled.add(arm)
                            continue
                        raise ValueError(f'{arm} preflight failed ({reason})') from None
                    execution = {'sequence': len(done) + 1, 'planned_position': position,
                                 'order_deviation': out_of_order, 'expected_next': list(expected),
                                 'started_at': utc_now()}
                    print(f'{task} {cell} started [{len(done) + 1}/{len(plan)}]', flush=True)
                    if arm == 'xerj':
                        async with local_session(folder / 'benchmark.sqlite', index=INDEX) as local:
                            result = await attempt(client, local, sources, arm, by_id[task], variant=variant)
                    else:
                        result = await attempt(client, None, sources, arm, by_id[task], variant=variant)
                    result['execution'] = {**execution, 'finished_at': utc_now()}
                    if issue := result['infrastructure_error']:
                        record_infrastructure(folder, {'task': task, 'arm': cell, 'stage': 'attempt',
                                                        **issue, 'result': result})
                        if issue['service'] == 'rovo' and args.allow_order_deviation:
                            print(f'Rovo deferred ({issue["reason"]})', flush=True)
                            disabled.add(arm)
                            continue
                        raise ValueError(f'{issue["service"]} attempt failed ({issue["reason"]})')
                    rows.setdefault(task, {})[cell] = result
                    save(folder / 'attempts.json', rows)
                    done.add((task, cell))
                    blind_export(folder, rows, plan, inventory)
                    print(f'{task} {cell} {result["termination"]} ({result["ms"] / 1000:.1f} s)', flush=True)
    print(f'{len(done)}/{len(plan)} completed cells', flush=True)
    return 0 if len(done) == len(plan) else 2


def check_range(ref, parts):
    block, start, end = (ref[k] for k in ('block', 'start', 'end'))
    return (all(type(value) is int for value in (block, start, end)) and 0 <= block < len(parts)
            and (start, end) in excerpt_ranges(parts[block]) and parts[block][start:end] == ref['text'])


def read_cloud_snapshot(folder, ref, page_id):
    stamp = ref['snapshot']
    if not re.fullmatch(r'[0-9a-f]{64}', stamp):
        raise ValueError('Invalid snapshot ID')
    payload = (folder / 'sources' / f'cloud-{stamp}.json').read_bytes()
    snapshot = json.loads(payload)
    if (hashlib.sha256(payload).hexdigest() != stamp or snapshot['doc'] != ref['doc']
            or snapshot['page_id'] != page_id):
        raise ValueError('Snapshot does not match its digest or document')
    return snapshot['html']


def local_target(ref, html):
    block, start, end = (ref[k] for k in ('block', 'start', 'end'))
    doc = ref['doc']
    name = doc.split(':', 1)[1] + '.html'
    url = urlsplit(ref['url'])
    anchor = excerpt_anchor(block, start, end)
    if (url.scheme != 'http' or not url.port or url.netloc != f'127.0.0.1:{url.port}'
            or not re.fullmatch(r'dsid_[a-zA-Z0-9_-]+\.html', name) or url.path != '/' + name
            or url.query or url.fragment != anchor
            or html.count(f'<span id="{anchor}">{escape(ref["text"])}</span>') != 1):
        raise ValueError('Invalid local link target')
    return name, url.port


def check_submission(result, variant, question):
    submission = result['submission']
    if submission is None:
        return not (result['answer'] or result['selected_excerpts'] or result['cited'])
    claims = submission['claims']
    refs = result['cited']
    if set(refs) != {claim['source'] for claim in claims}:
        return False
    if selected_excerpts(submission, refs, variant) != result['selected_excerpts']:
        return False
    finishes = [call for call in result['trace'] if call['tool'] == 'finish' and call['result'].get('accepted')]
    if len(finishes) != 1:
        return False
    sent = finishes[0]['args']
    expected = sent
    if variant == 'B':
        expected = {**sent, 'claims': [{**claim, 'quote': refs[claim['source']]['text']}
                                       for claim in sent['claims']]}
    if expected != submission:
        return False
    return result['answer'] == final_answer(sent, refs, parts=result['requested_parts'],
                                            document_count=asks_for_document_count(question), variant=variant)


async def verify_bindings(folder, tasks, manifest, inventory, *, online=False):
    rows = load(folder / 'attempts.json')
    settings = load(folder / 'settings.json')
    plan = schedule(tasks)
    questions = {row['id']: row['query'] for row in tasks}
    if any(settings[field] != digest(path) for field, path in (
            ('manifest_sha256', MANIFEST), ('inventory_sha256', folder / 'inventory.json'),
            ('tasks_sha256', folder / 'tasks.json'))):
        raise ValueError('Verification inputs differ from the checkpoint')
    done = completed(rows, plan)
    rovo = rovo_client(folder, inventory) if online and any(cell.startswith('rovo_') for _, cell in done) else None
    known = {row['xerj_id']: row for row in manifest.values()}
    checks, live_cloud, live_local, ports = {}, {}, {}, set()
    path = folder / ('bindings-online.json' if online else 'bindings-offline.json')
    with ZipFile(ARCHIVE) as archive, ExitStack() as servers:
        local_parts = {}
        for task, cell in plan:
            if (task, cell) not in done:
                continue
            arm, variant = cell.split('_')
            result = rows[task][cell]
            try:
                submitted = check_submission(result, variant, questions[task])
            except (KeyError, TypeError, ValueError, IndexError):
                submitted = False
            entry = {'submission_consistent': submitted, 'sources': {}}
            checks.setdefault(task, {})[cell] = entry
            for label, ref in result['cited'].items():
                status = {'offline': False}
                entry['sources'][label] = status
                try:
                    doc = ref['doc']
                    observed = any(call['tool'] == 'read' and isinstance(call['result'], dict)
                                   and call['result'].get('doc') == doc
                                   and ref in ({**item, 'doc': doc} for item in call['result'].get('references', []))
                                   for call in result['trace'])
                    if not observed or ref['id'] != label:
                        raise ValueError('The cited excerpt was not shown in a read')
                    if arm == 'rovo':
                        page = inventory['pages'][doc]
                        base = f'https://{inventory["site"]}/wiki/spaces/{inventory["space"]}/pages/{page}'
                        parts = cloud_blocks(read_cloud_snapshot(folder, ref, page))
                        if not check_range(ref, parts) or cloud_reference(base, ref['text'], parts) != ref['url']:
                            raise ValueError('Cloud link does not bind the recorded excerpt')
                    else:
                        if doc not in local_parts:
                            local_parts[doc] = blocks(archive.read(known[doc]['archive_path']).decode())
                        if not check_range(ref, local_parts[doc]):
                            raise ValueError('The stored excerpt differs from the original corpus')
                        name = doc.split(':', 1)[1] + '.html'
                        html = (folder / 'sources' / name).read_text(encoding='utf-8')
                        _, port = local_target(ref, html)
                    status['offline'] = True
                except (OSError, KeyError, ValueError, TypeError, IndexError):
                    status['error'] = 'binding_mismatch'
                if online:
                    status['online'] = None
                    if not status['offline']:
                        continue
                    try:
                        if arm == 'rovo':
                            if doc not in live_cloud:
                                page_data = await rovo.cloud.read(page, content_format='html')
                                if str(page_data['id']) != page:
                                    raise ValueError('wrong_page_id')
                                live_cloud[doc] = cloud_blocks(page_data['body']['value'])
                            parts = live_cloud[doc]
                            status['online'] = (check_range(ref, parts) and
                                                cloud_reference(base, ref['text'], parts) == ref['url'])
                        else:
                            if port not in ports:
                                servers.enter_context(source_site(folder / 'sources', port))
                                ports.add(port)
                            if (port, name) not in live_local:
                                live_local[port, name] = await asyncio.to_thread(
                                    read_source_html, f'http://127.0.0.1:{port}/{name}')
                            status['online'] = bool(local_target(ref, live_local[port, name]))
                    except (OSError, RuntimeError, KeyError, ValueError, TypeError, IndexError) as exc:
                        status['online_error'] = infrastructure_reason(exc)
            entry['ok'] = submitted and all(s['offline'] and (not online or s['online'] is True)
                                           for s in entry['sources'].values())
    save(path, checks)
    invalid = sum(not entry['ok'] for cells in checks.values() for entry in cells.values())
    citations = sum(len(entry['sources']) for cells in checks.values() for entry in cells.values())
    print(f'{len(done)} cells, {citations} citation bindings, {invalid} cells with verification errors', flush=True)
    return int(bool(invalid))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=FOLDER)
    sub = parser.add_subparsers(dest='command', required=True)
    launch = sub.add_parser('run', help='Run live cells and checkpoint after each one')
    launch.add_argument('--resume', action='store_true')
    launch.add_argument('--platform', choices=('all', 'xerj', 'rovo'), default='all')
    launch.add_argument('--allow-order-deviation', action='store_true',
                        help='Record out-of-order execution and continue XERJ if the cloud is down')
    sub.add_parser('blind', help='Rebuild the blind sheet from the checkpoint')
    verify = sub.add_parser('verify', help='Check recorded ranges, documents, and anchors')
    verify.add_argument('--online', action='store_true', help='Also fetch live cloud and local evidence URLs')
    args = parser.parse_args()
    folder = args.folder
    if not folder.resolve().is_relative_to(Path('.state').resolve()):
        parser.error('Experiment files must be inside .state')
    if args.command == 'run' and args.platform != 'all' and not args.allow_order_deviation:
        parser.error('A single-platform run requires --allow-order-deviation')
    try:
        tasks = read_tasks(folder / 'tasks.json')
        manifest, inventory = corpus_inputs(folder)
        if args.command == 'run':
            return asyncio.run(run(folder, tasks, manifest, inventory, args))
        if args.command == 'verify':
            return asyncio.run(verify_bindings(folder, tasks, manifest, inventory, online=args.online))
        with writer(folder):
            plan = schedule(tasks)
            rows = load(folder / 'attempts.json')
            completed(rows, plan)
            blind_export(folder, rows, plan, inventory)
            return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        parser.exit(1, f'Experiment stopped ({type(exc).__name__}). See the terminal log and .state checkpoint.\n'
                    if isinstance(exc, (OSError, KeyError)) else f'Experiment stopped: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
