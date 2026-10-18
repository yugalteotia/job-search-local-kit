import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from tab_lifecycle import OwnedTabs


class FakeClient:
    def __init__(self, ids=(2,)):
        self.ids = iter(ids)
        self.calls = []

    def tool(self, name, args, timeout=30):
        self.calls.append((name, dict(args)))
        if name == 'browser_tabs' and args.get('action') == 'new':
            tab_id = next(self.ids)
            return {'content': [{'type': 'text', 'text': f'- {tab_id}: (current) [tab](https://example.invalid/)'}]}
        return {'content': []}


class TabLifecycleTests(unittest.TestCase):
    def test_closes_only_recorded_session_id(self):
        client = FakeClient((7,))
        tabs = OwnedTabs(client, run_id='test-tab-ownership')
        tab_id = tabs.open('https://example.invalid/')
        self.assertEqual(tab_id, 7)
        with self.assertRaises(ValueError):
            tabs.reuse(1, 'https://personal.invalid/')
        summary = tabs.close_all()
        self.assertEqual(summary['created'], 1)
        self.assertEqual(summary['closed'], 1)
        self.assertEqual(summary['remaining_automation_tabs'], 0)
        closes = [args for name, args in client.calls if name == 'browser_tabs' and args.get('action') == 'close']
        self.assertEqual(closes, [{'action': 'close', 'index': 7}])

    def test_tab_cap_and_reuse_require_owned_id(self):
        client = FakeClient((3, 4, 5, 6))
        tabs = OwnedTabs(client, run_id='test-tab-cap', cap=1)
        tab_id = tabs.open('https://example.invalid/')
        with self.assertRaises(RuntimeError):
            tabs.open('https://another.invalid/')
        tabs.reuse(tab_id, 'https://example.invalid/next')
        self.assertEqual(tabs.created, 1)
        self.assertEqual(tabs.reused, 1)

    def test_missing_id_is_reported_and_not_closed(self):
        client = FakeClient()
        client.tool = lambda name, args, timeout=30: {'content': [{'type': 'text', 'text': '- current: tab'}]}
        tabs = OwnedTabs(client, run_id='test-tab-unknown')
        with self.assertRaises(RuntimeError):
            tabs.open('https://example.invalid/')
        summary = tabs.close_all()
        self.assertEqual(summary['closed'], 0)
        self.assertEqual(summary['remaining_automation_tabs'], 1)
        self.assertTrue(summary['remaining_tab_ids'])


if __name__ == '__main__':
    unittest.main()
