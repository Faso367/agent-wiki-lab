"""Freeze question-only inputs and a separate grading key before citation A/B trials."""
import argparse
import json
from pathlib import Path
import random

from corpus import DOWNLOADS, ROOT
from fetch_assets import digest
from retry_cloud_search import read_manifest
from rovo_benchmark import tasks
from rovo_read_pilot import freeze
from scale_xerj import SHA256, save

SEED = 20261123


def prepare(output, inventory, manifest):
    previous = freeze()  # Checks the release questions as well as the 64-task baseline.
    known = set(previous) | set(json.loads((ROOT / 'oracle-excerpts.json').read_text()))
    for path in (ROOT / 'results/agent-client-2026-11-23').glob('*/blind-key.json'):
        known.update(row['task'] for row in json.loads(path.read_text()).values())
    inv = json.loads(inventory.read_text())
    docs = {r['xerj_id'] for r in read_manifest(manifest).values()}
    if (len(docs) != 512 or set(inv['pages']) != docs or not inv['verified']
            or inv['archive_sha256'] != SHA256):
        raise ValueError('Invalid shared inventory')
    verified = {doc for doc, info in inv['gold_reads'].items()
                if info['text_matches'] and info['page'] == inv['pages'][doc]}
    rows = sorted(tasks(), key=lambda row: row['id'])
    candidates = [r for r in rows if r['id'] not in known and set(r['gold']) <= verified]
    rng = random.Random(SEED)
    held = [r for multi in (False, True) for r in rng.sample(
        [r for r in candidates if (len(r['gold']) > 1) == multi], 8)]
    rng.shuffle(held)
    first = ['qst_0150', 'qst_0259']
    ordered = [previous[k] for k in first + [k for k in previous if k not in first]] + held
    public = {r['question_id']: r for r in map(json.loads, (DOWNLOADS / 'questions.jsonl').read_text().splitlines())}
    key, questions = {}, []
    for row in ordered:
        source = public[row['id']]
        if (source['question'] != row['query']
                or set(row['gold']) != {'confluence:' + doc for doc in source['expected_doc_ids']}
                or not set(row['gold']) <= verified):
            raise ValueError('Unverified question or source')
        key[row['id']] = {k: source[k] for k in ('gold_answer', 'answer_facts')}
        key[row['id']]['gold'] = row['gold']
        questions.append({'id': row['id'], 'query': row['query'],
                          'cohort': 'diagnostic' if row['id'] in previous else 'holdout'})
    output.mkdir(parents=True, exist_ok=True)
    for name, data in [('tasks.json', questions), ('grading.json', key)]:
        path = output / name
        if path.exists() and json.loads(path.read_text()) != data:
            raise ValueError(f'Refusing to replace a different {name}')
        save(path, data)
    save(output / 'sample.json', {'seed': SEED, 'excluded_holdout': sorted(known),
         'eligible': len(candidates), 'manifest_sha256': digest(manifest),
         'questions_sha256': digest(DOWNLOADS / 'questions.jsonl'),
         'baseline_sha256': digest(ROOT / 'bench-questions.json'),
         'tasks_sha256': digest(output / 'tasks.json'), 'grading_sha256': digest(output / 'grading.json')})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'benchmarks/pages.csv')
    args = parser.parse_args()
    prepare(args.output, args.inventory, args.manifest)
