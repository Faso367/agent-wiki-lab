"""Summarize the four citation conditions from complete, source-checked grades."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics

CELLS = ('xerj_A', 'xerj_B', 'rovo_A', 'rovo_B')


def lower_exact(k, n):
    """One-sided Clopper-Pearson lower limit with alpha .0125."""
    if k == 0:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        p = (lo + hi) / 2
        tail = sum(math.comb(n, i) * p**i * (1 - p)**(n - i) for i in range(k, n + 1))
        if tail < 0.0125:
            lo = p
        else:
            hi = p
    return (hi + lo) / 2


def paired(left, right):
    n = len(left)
    wins = sum(a and not b for a, b in zip(left, right, strict=True))
    losses = sum(b and not a for a, b in zip(left, right, strict=True))
    return {'pairs': n, 'both_pass': sum(a and b for a, b in zip(left, right)),
            'left_only': wins, 'right_only': losses, 'difference_pp': 100 * (wins - losses) / n,
            'ci95_pp': [100 * (lower_exact(wins, n) - (1 - lower_exact(n - losses, n))),
                        100 * (1 - lower_exact(n - wins, n) - lower_exact(losses, n))]}


def summarize(tasks, attempts, references, grades):
    expected = {row['id'] for row in tasks}
    if (len(tasks) != len(expected) or set(attempts) != expected or set(grades) != expected
            or set(references) != expected
            or any(set(attempts[q]) != set(CELLS) or set(grades[q]) != set(CELLS) for q in expected)):
        raise ValueError('Every assigned task needs all four attempts and source-checked grades')
    report = {}
    for cohort in dict.fromkeys(q['cohort'] for q in tasks):
        ids = [q['id'] for q in tasks if q['cohort'] == cohort]
        cells, passes = {}, {}
        for cell in CELLS:
            outcomes, elapsed, rejects = [], [], Counter()
            submitted = timeouts = read_gold = repeated_reads = repeated_searches = 0
            retries = bad_meaning = bad_links = 0
            prompt = completion = no_usage = 0
            for q in ids:
                row, grade = attempts[q][cell], grades[q][cell]
                if type(grade['pass']) is not bool or type(grade['wrong_meaning_with_evidence']) is not bool:
                    raise ValueError('Grades require boolean verdicts')
                if grade['pass'] and (row['submission'] is None or row['error'] or not row['cited']
                                      or grade['links_ok'] is not True):
                    raise ValueError('Strict success requires a completed, source-checked answer')
                outcomes.append(grade['pass'])
                elapsed.append(row['ms'] / 1000)
                submitted += row['submission'] is not None
                timeouts += row['error'] == 'TimeoutError'
                bad_meaning += grade['wrong_meaning_with_evidence']
                bad_links += grade['links_ok'] is False
                usage = [record for record in row['usage'] if record is not None]
                prompt += sum(v['prompt_tokens'] for v in usage)
                completion += sum(v['completion_tokens'] for v in usage)
                missing_usage = len(row['model_request_ms']) - len(usage)
                if missing_usage < 0:
                    raise ValueError('More usage records than model calls')
                no_usage += missing_usage
                found, seen_reads, seen_search = set(), set(), set()
                finishes = 0
                for call in row['trace']:
                    result, args = call['result'], call.get('args', {})
                    if call['tool'] == 'read' and isinstance(result, dict) and result.get('references'):
                        found.add(result['doc'])
                        window = (result['doc'], tuple((r['block'], r['text']) for r in result['references']))
                        repeated_reads += window in seen_reads
                        seen_reads.add(window)
                    if call['tool'] == 'search' and isinstance(result, list):
                        page = (args['query'].strip(), args.get('page', 1))
                        repeated_searches += page in seen_search
                        seen_search.add(page)
                    if call['tool'] == 'finish':
                        finishes += 1
                        if 'error' in result:
                            rejects[result['error']] += 1
                retries += max(0, finishes - 1)
                read_gold += bool(found.intersection(references[q]['gold']))
            passes[cell] = outcomes
            success = sum(outcomes)
            cells[cell] = {'attempts': len(ids), 'submitted': submitted, 'strict_pass': success,
                           'rate': success / len(ids), 'p50_s': statistics.median(elapsed),
                           'p95_s': sorted(elapsed)[math.ceil(len(ids) * .95) - 1],
                           'prompt_tokens': prompt, 'completion_tokens': completion,
                           'tokens_per_success': (prompt + completion) / success if success else None,
                           'model_calls_without_usage': no_usage, 'timeouts': timeouts,
                           'read_any_gold': read_gold, 'wrong_meaning_with_evidence': bad_meaning,
                           'attempts_with_broken_links': bad_links, 'finish_rejections': dict(rejects),
                           'repeat_finish_calls': retries, 'repeat_read_windows': repeated_reads,
                           'repeat_search_pages': repeated_searches}
        pairs = {f'{a}-{b}': paired(passes[a], passes[b]) for a, b in (
            ('xerj_B', 'xerj_A'), ('rovo_B', 'rovo_A'), ('xerj_A', 'rovo_A'), ('xerj_B', 'rovo_B'))}
        report[cohort] = {'cells': cells, 'paired': pairs}
    return report


def adjudicate(candidates, key, grades, bindings):
    if set(key) != set(candidates) or set(key) != set(grades):
        raise ValueError('The blind sheet, key and semantic grades must cover the same candidates')
    rows = {}
    for label, decision in grades.items():
        ref = key[label]
        task, cell = ref['task'], ref['arm']
        if task != candidates[label]['task'] or cell in rows.setdefault(task, {}):
            raise ValueError('Invalid blind mapping')
        if type(decision['meaning_pass']) is not bool or not decision['reason'].strip():
            raise ValueError('Each semantic grade requires an explicit verdict and reason')
        binding = bindings[task][cell]
        sources = binding['sources']
        links_ok = None
        if sources:
            links_ok = binding['submission_consistent'] and all(
                s['offline'] is True and s.get('online') is True for s in sources.values())
        rows[task][cell] = {**decision, 'label': label, 'links_ok': links_ok,
                            'pass': decision['meaning_pass'] and links_ok is True}
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path, help='Directory with the trial and blinded grading files')
    args = parser.parse_args()

    def load(name):
        return json.loads((args.folder / name).read_text())

    grades = adjudicate(load('blind.json'), load('blind-key.json'), load('grades.json'), load('bindings-online.json'))
    result = summarize(load('tasks.json'), load('attempts.json'), load('grading.json'), grades)
    print(json.dumps(result, indent=2, ensure_ascii=False))
