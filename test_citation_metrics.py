import unittest

from citation_metrics import CELLS, adjudicate, paired, summarize


class CitationScoringTests(unittest.TestCase):
    def test_missing_usage_and_zero_success_survive_in_summary(self):
        case = {'usage': [dict(prompt_tokens=8, completion_tokens=3), None], 'model_request_ms': [5, 170000],
                'submission': None, 'error': 'TimeoutError', 'ms': 180000, 'trace': [], 'cited': {}}
        decision = {'pass': False, 'wrong_meaning_with_evidence': False, 'links_ok': None}
        tasks = [dict(id='q', cohort='holdout')]
        rows = {'q': dict.fromkeys(CELLS, case)}
        grades = {'q': dict.fromkeys(CELLS, decision)}
        report = summarize(tasks, rows, {'q': {'gold': ['d']}}, grades)['holdout']
        self.assertTrue(all(r['tokens_per_success'] is None and r['completion_tokens'] == 3
                            and r['model_calls_without_usage'] == 1 for r in report['cells'].values()))
        del rows['q']['xerj_B']
        with self.assertRaises(ValueError):
            summarize(tasks, rows, {'q': {'gold': ['d']}}, grades)

    def test_semantic_support_and_live_link_both_required(self):
        key = {'blind': {'task': 'q', 'arm': 'xerj_A'}}
        candidates = {'blind': {'task': 'q'}}
        grades = {'blind': {'meaning_pass': False, 'wrong_meaning_with_evidence': True,
                            'reason': 'The response says 45 seconds. Its source says 90 seconds.'}}
        check = {'offline': True, 'online': True}
        bindings = {'q': {'xerj_A': {'submission_consistent': True, 'sources': {'S1': check}}}}
        self.assertFalse(adjudicate(candidates, key, grades, bindings)['q']['xerj_A']['pass'])
        grades['blind'].update(meaning_pass=True, wrong_meaning_with_evidence=False, reason='Supported.')
        self.assertTrue(adjudicate(candidates, key, grades, bindings)['q']['xerj_A']['pass'])
        check['online'] = False
        self.assertFalse(adjudicate(candidates, key, grades, bindings)['q']['xerj_A']['pass'])

    def test_small_sample_uncertainty_and_direction(self):
        baseline = [False] * 16
        lo, hi = paired(baseline, baseline)['ci95_pp']
        self.assertLess(lo, 0)
        self.assertGreater(hi, 10)
        better = [True] * 2 + [False] * 14
        self.assertEqual(paired(better, baseline)['difference_pp'], 12.5)
        self.assertEqual(paired(baseline, better)['difference_pp'], -12.5)
