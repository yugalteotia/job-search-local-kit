"""No real Chrome, network, upload, or application is used by these checks."""
import ast
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import naukri_handoff_inspect as inspector


class InspectorReadOnly(unittest.TestCase):
    def test_guard_blocks_native_show_answers_and_all_mutation_methods(self):
        for method,path in [('POST','/apply-workflow/v1/apply'),('GET','/apply-workflow/v1/apply'),
                            ('POST','/botapi/chat'),('GET','/botapi/chat'),
                            ('POST','/profile/resume/upload'),('PUT','/anything'),
                            ('PATCH','/anything'),('DELETE','/anything')]:
            with self.subTest(method=method,path=path):
                route=MagicMock();route.request.method=method
                route.request.url='https://www.naukri.com'+path
                inspector.inspector_readonly_route(route)
                route.abort.assert_called_once();route.continue_.assert_not_called()
        route=MagicMock();route.request.method='GET';route.request.url='https://www.naukri.com/myapply/historypage'
        inspector.inspector_readonly_route(route)
        route.continue_.assert_called_once();route.abort.assert_not_called()

    def test_inspection_installs_guard_before_profile_read_and_never_acts(self):
        context=MagicMock();page=MagicMock();context.pages=[page]
        @contextlib.contextmanager
        def fake_chrome(**options):
            self.assertEqual(options,{'headless':False,'read_only':True})
            yield context
        def status(_page):
            context.route.assert_called_once_with('**/*',inspector.inspector_readonly_route)
            return {'signed_in':True}
        db=MagicMock()
        def query(sql,values):
            cursor=MagicMock()
            cursor.fetchone.return_value=None if 'submission_attempts' in sql else {
                'id':values[0],'slug':str(values[0]),'requisition_id':str(values[0]),
                'canonical_url':'https://www.naukri.com/job-listings-example-'+str(values[0]),
                'employer':'Example','title':'Java Developer'}
            return cursor
        db.execute.side_effect=query
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'.workflow').mkdir()
            for job in (202,266):(root/'jobs'/str(job)).mkdir(parents=True)
            with patch.object(inspector,'ROOT',root),patch.object(inspector,'_chrome',fake_chrome),\
                 patch.object(inspector.job_db,'connect',return_value=db),patch.object(inspector,'_status',side_effect=status),\
                 patch.object(inspector,'current_filename',return_value=inspector.GENERIC.name),\
                 patch.object(inspector,'_listing',return_value={'applied':True}),\
                 patch.object(inspector,'_history',return_value={'full':True,'exact':1}):
                results=inspector.run()
                self.assertTrue(all(r['read_only'] and r['answers_sent']==0 for r in results))
                page.assert_not_called();self.assertEqual(page.mock_calls,[])
        # Keep application controls and upload helpers out of the inspector itself.
        tree=ast.parse(Path(inspector.__file__).read_text())
        forbidden={'click','fill','press','check','set_input_files','upload_profile','_reopen','_run_one','begin_attempt'}
        for node in ast.walk(tree):
            if isinstance(node,ast.Call):
                name=node.func.attr if isinstance(node.func,ast.Attribute) else node.func.id if isinstance(node.func,ast.Name) else ''
                self.assertNotIn(name,forbidden)


if __name__=='__main__':unittest.main()
