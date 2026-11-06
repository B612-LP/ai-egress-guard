"""Offline scout regression tests; no Clash process or user files are touched."""
import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('guard', ROOT / 'ai-egress-guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class FakeGuard(guard.Guard):
    def __init__(self, home, answers):
        super().__init__(home)
        self.members = ['current', 'a', 'b', 'c', 'd', 'e']
        self.answers = answers
        self.calls = []
        self.calls_lock = threading.Lock()

    def context(self):
        return 'epoch', self.members, 'current'

    def base_network_ok(self):
        return True

    def delay(self, node):
        with self.calls_lock:
            self.calls.append(node)
            response = self.answers.get(node, 10)
            if isinstance(response, list):
                return response.pop(0) if response else None
            return response

    def log(self, *args, **kwargs):
        pass


class ScoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.g = FakeGuard(self.tmp.name, {})

    def seed(self, nodes):
        now = time.time()
        guard.atomic(self.g.cache_path, dict(version=guard.CACHE_VERSION, epoch='epoch',
            candidates=[dict(node=n, delay_ms=10, started_at=now, success_at=now) for n in nodes],
            failed_at={}, next_refresh=0))

    def scout(self):
        with mock.patch.object(guard.time, 'sleep', return_value=None):
            self.g.scout(force=True)

    def test_full_standby_set_only_rechecks_standbys(self):
        self.seed(['a', 'b'])
        self.scout()
        self.assertCountEqual(self.g.calls, ['a', 'b'])
        self.assertEqual({x['node'] for x in guard.load(self.g.cache_path)['candidates']}, {'a', 'b'})

    def test_transient_failure_retested_before_removal(self):
        self.seed(['a', 'b'])
        self.g.answers['a'] = [None, 15]
        self.scout()
        self.assertEqual(self.g.calls.count('a'), 2)
        self.assertEqual(self.g.calls.count('b'), 1)
        self.assertEqual(set(self.g.calls), {'a', 'b'})
        self.assertEqual({x['node'] for x in guard.load(self.g.cache_path)['candidates']}, {'a', 'b'})

    def test_definitive_failure_retested_then_replenished(self):
        self.seed(['a', 'b'])
        self.g.answers['a'] = [None, None]
        self.scout()
        self.assertEqual(self.g.calls.count('a'), 2)
        self.assertEqual(self.g.calls.count('b'), 1)
        candidates = {x['node'] for x in guard.load(self.g.cache_path)['candidates']}
        self.assertNotIn('a', candidates)
        self.assertIn('b', candidates)
        self.assertEqual(len(candidates), 2)
        # A full pool scan for one missing standby is forbidden.
        self.assertLess(len(set(self.g.calls)), len(self.g.members) - 1)

    def test_explicit_cross_subscription_preferred(self):
        guard.atomic(self.g.subscriptions_path, {'current': 'A', 'a': 'A', 'b': 'B', 'c': 'B'})
        self.scout()
        candidates = {x['node'] for x in guard.load(self.g.cache_path)['candidates']}
        self.assertEqual(candidates, {'b', 'c'})
        self.assertNotIn('a', self.g.calls)

    def test_unknown_current_uses_group_order(self):
        guard.atomic(self.g.subscriptions_path, {'a': 'A', 'b': 'B'})
        self.scout()
        self.assertEqual({x['node'] for x in guard.load(self.g.cache_path)['candidates']}, {'a', 'b'})

    def test_invalid_mapping_uses_group_order(self):
        self.g.subscriptions_path.write_text('{invalid')
        self.scout()
        self.assertEqual({x['node'] for x in guard.load(self.g.cache_path)['candidates']}, {'a', 'b'})

    def test_mapping_change_invalidates_context_epoch(self):
        with mock.patch.object(guard.Guard, 'group', return_value={'now': 'current', 'all': self.g.members}), \
             mock.patch.object(self.g, 'network_identity', return_value=['network']):
            first = guard.Guard.context(self.g)[0]
            guard.atomic(self.g.subscriptions_path, {'current': 'A', 'b': 'B'})
            second = guard.Guard.context(self.g)[0]
        self.assertNotEqual(first, second)

    def test_failed_marker_rejects_older_success(self):
        now = time.time()
        record = {'node': 'a', 'delay_ms': 10, 'started_at': now - 5, 'success_at': now}
        self.assertEqual(self.g.ranked([record], self.g.members, 'current', {'a': now - 1}), [])

    def test_active_recovery_preempts_background(self):
        self.seed(['a', 'b'])
        with self.g.lock('ai-egress-recovery.lock', blocking=True) as acquired:
            self.assertTrue(acquired)
            self.scout()
        self.assertEqual(self.g.calls, [])

    def test_not_due_does_not_probe(self):
        self.seed(['a', 'b'])
        cache = guard.load(self.g.cache_path)
        cache['next_refresh'] = time.time() + guard.REFRESH / 2
        guard.atomic(self.g.cache_path, cache)
        self.g.scout()
        self.assertEqual(self.g.calls, [])

    def test_empty_cache_stops_after_two_candidates(self):
        self.seed([])
        self.scout()
        candidates = guard.load(self.g.cache_path)['candidates']
        self.assertEqual(len(candidates), 2)
        # At most a bounded batch can already be in flight when two are found.
        self.assertLess(len(self.g.calls), len(self.g.members) - 1)


if __name__ == '__main__':
    unittest.main()
