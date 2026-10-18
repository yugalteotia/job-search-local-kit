import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import private_playwright as p

class PrivateTransportTests(unittest.TestCase):
    def test_rejects_public_or_symlink_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d)/'private';directory.mkdir(mode=0o700)
            secret=directory/'playwright.json'
            secret.write_text(json.dumps({'token':'test-value','profile':'Default'}))
            secret.chmod(0o600)
            with patch.object(p,'PRIVATE',directory), patch.object(p,'SECRET',secret):
                self.assertEqual(p.load_private()['profile'],'Default')
                secret.chmod(0o644)
                with self.assertRaises(ValueError): p.load_private()
                secret.unlink();target=Path(d)/'target';target.write_text('{}');secret.symlink_to(target)
                with self.assertRaises(ValueError): p.load_private()

    def test_secret_only_in_mcp_environment(self):
        with patch.object(p,'load_private',return_value={'token':'dummy-private-value','profile':'Profile 1'}), patch.object(os,'execvpe') as launch:
            p.launch()
            command,args,env=launch.call_args.args
            self.assertNotIn('dummy-private-value',str(args))
            self.assertEqual(env['PLAYWRIGHT_MCP_EXTENSION_TOKEN'],'dummy-private-value')
            self.assertEqual(env['PLAYWRIGHT_MCP_PROFILE_DIR_NAME'],'Profile 1')

if __name__=='__main__': unittest.main()
