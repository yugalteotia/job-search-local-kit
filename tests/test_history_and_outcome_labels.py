"""Regression checks using saved data/mocks only; never launch a browser."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import naukri_native_worker as worker
import quick_status
import job_db


class HistoryAndOutcomeLabels(unittest.TestCase):
    def page(self,snapshots):
        page=MagicMock();source=MagicMock();source.filter.return_value=source;source.count.return_value=1
        more=MagicMock();more.is_visible.return_value=False
        rows=MagicMock();rows.count.return_value=20
        page.locator.side_effect=lambda s:{'.apply-filters li':source,'#loadMoreLnk':more,
            '.jdTuplesContainer .jdTupleContainer':rows}[s]
        page.evaluate.side_effect=snapshots
        return page

    def test_partial_history_never_claims_a_match_even_when_a_row_matches(self):
        page=self.page([{'rows':20,'total':221,'exact':1,'company_role':0,'more':False}])
        history=worker._history(page,'1234567890','Example','Java Developer')
        self.assertFalse(history['full']);self.assertEqual(history['exact'],0)
        self.assertEqual(history['observed_exact_rows_unverified'],1)
        self.assertFalse(worker.history_match(history))
        self.assertFalse(worker.history_match({'full':False,'exact':1}))

    def test_a_matching_row_does_not_stop_loading_before_completion(self):
        page=self.page([{'rows':20,'total':21,'exact':1,'company_role':0,'more':True},None,
                        {'rows':21,'total':21,'exact':1,'company_role':0,'more':False}])
        history=worker._history(page,'1234567890','Example','Java Developer')
        self.assertTrue(history['full']);self.assertTrue(worker.history_match(history))

    def test_exact_server_history_identity_requires_complete_history(self):
        for rows,expected in ((20,False),(21,True)):
            page=self.page([{'rows':rows,'total':21,'exact':0,'company_role':1,'more':False}])
            response=MagicMock();response.url='https://www.naukri.com/cloudgateway-apply/whtma-services/v0/applyapi/v5/history'
            response.request.method='GET';response.status=200
            response.json.return_value={'jobs':[{'jobId':'1234567890','jobTitle':'Java Developer','company':'Example'}]}
            page.goto.side_effect=lambda *a,**k:page.on.call_args.args[1](response)
            history=worker._history(page,'1234567890','Example','Java Developer')
            self.assertEqual(worker.history_match(history),expected)
            self.assertEqual(history['exact'],int(expected))

    def test_company_role_matches_require_full_native_other_id_coverage(self):
        for rows,total,ids,expected in ((21,21,['1111111111','2222222222'],True),
                (20,21,['1111111111','2222222222'],False),
                (21,21,['1111111111'],False),
                (21,21,['1111111111','1234567890'],False)):
            page=self.page([{'rows':rows,'total':total,'exact':0,'company_role':2,'more':False}])
            response=MagicMock();response.url='https://www.naukri.com/cloudgateway-apply/whtma-services/v0/applyapi/v5/history'
            response.request.method='GET';response.status=200
            response.json.return_value={'jobs':[{'jobId':id,'jobTitle':'Java Developer','company':'Example'} for id in ids]}
            page.goto.side_effect=lambda *a,**k:page.on.call_args.args[1](response)
            history=worker._history(page,'1234567890','Example','Java Developer')
            self.assertEqual(history['company_role_disambiguated'],expected)
            self.assertEqual(history['company_role'],0 if expected else 2)
            if expected:self.assertEqual(history['exact'],0)

    def test_confirmation_rejects_a_positive_claim_from_incomplete_history(self):
        db=MagicMock();db.execute.return_value.fetchone.return_value={'slug':'example','requisition_id':'123'}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'jobs/example';folder.mkdir(parents=True)
            (folder/'proof.json').write_text(json.dumps({'exact_history_id_match':True,'history_full':False}))
            with patch.object(job_db,'ROOT',root),patch.object(job_db,'require_writable_action'):
                with self.assertRaisesRegex(ValueError,'complete Applied Jobs history load'):
                    job_db.confirm_attempt(db,'existing',1,'jobs/example/proof.json','2026-10-13T07:00:00+00:00')

    def test_both_dashboard_rows_show_pending_reconciliation(self):
        rows=[{'id':314,'employer':'Infosys','title':'Java','requisition_id':'280926032117','canonical_url':'',
               'slug':'infosys','status_date':'2026-10-13'},
              {'id':266,'employer':'Luxoft','title':'Java','requisition_id':'290926917619','canonical_url':'',
               'slug':'luxoft','status_date':'2026-10-13'}]
        db=MagicMock()
        def execute(sql,args=()):
            result=MagicMock();data=rows if "WHERE state='Applied'" in sql else []
            result.__iter__.return_value=iter(data);result.fetchall.return_value=data
            return result
        db.execute.side_effect=execute
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for slug,extra in [('infosys',{'independently_reconciled':False}),('luxoft',{'workflow_worker_proven':False})]:
                folder=root/'jobs'/slug;folder.mkdir(parents=True)
                (folder/'application-outcome-audit.json').write_text(json.dumps({'retry_allowed':False,'listing_applied':True,**extra}))
            status=quick_status.collect(db,root)
            label='Applied on listing — reconciliation pending'
            self.assertEqual([x['outcome_label'] for x in status['applied']],[label,label])
            self.assertEqual(quick_status.render(status).count(label),2)
            self.assertFalse(any(call.args[0].lstrip().upper().startswith(('UPDATE','INSERT','DELETE')) for call in db.execute.call_args_list))


if __name__=='__main__':unittest.main()
