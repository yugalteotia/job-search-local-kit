"""Concrete portal workers. They wrap the existing tested functions; behavior is unchanged."""
import job_db
from .base import Capability,PortalWorker

BAD_DISCOVERY={'Blocked','Failed','NoVisibleCards','BlankPage','CardsWithoutExactLinks'}

def _last_discovery(db,sources):
    """Most recent per-source coverage status from the latest run that checked it."""
    for source in sources:
        row=db.execute("""SELECT status,error_kind,checked_at FROM source_coverage WHERE source=?
            ORDER BY checked_at DESC LIMIT 1""",(source,)).fetchone()
        if row:return dict(row)
    return None

def _apply_flag(config,name):
    return bool(config['portal_workers'][name]['apply_enabled'])

class DiscoveryWorker(PortalWorker):
    """LinkedIn, Foundit, Indeed, Cutshort: portal search cards via the existing browser bridge."""
    def __init__(self,name):self.name=name
    def _sources(self):return [self.name]
    def _enabled(self,config):
        return any(config['sources'].get(n,{}).get('enabled') for n in self._sources())
    def capability(self,db,config):
        if not self._enabled(config):return Capability.BLOCKED,'Discovery is switched off in config'
        last=_last_discovery(db,self._sources())
        if last and last['status'] in BAD_DISCOVERY:
            return Capability.BLOCKED,f"Last discovery {last['status']}"+(f" ({last['error_kind']})" if last['error_kind'] else '')
        return Capability.DISCOVERY_ONLY,'No end-to-end application path proven for this portal'
    def discover(self,section,name,record,config,*,run_number=0,page_cap=None,query_cap=None,deadline=None,**_):
        import browser_portals
        return browser_portals.fetch_portal(name,record,config['search'],run_number=run_number,page_cap=page_cap,
                                            query_cap=query_cap,deadline=deadline)

class CutshortWorker(DiscoveryWorker):
    def verify(self,db,leads,**options):
        import portal_native
        return portal_native.verify_leads(db,leads,**options)

class LinkedInWorker(DiscoveryWorker):
    supports_apply=True
    def __init__(self):super().__init__('linkedin')
    def discover(self,section,name,record,config,**options):
        import linkedin_discovery
        return linkedin_discovery.fetch(record,config['search'],**{k:v for k,v in options.items() if k in ('run_number','page_cap','query_cap','deadline')})
    def capability(self,db,config):
        import job_config
        if not self._enabled(config):return Capability.BLOCKED,'LinkedIn discovery is disabled'
        if not job_config.live_provider_enabled(config,'linkedin'):return Capability.DISCOVERY_ONLY,'LinkedIn live lane is off'
        if not job_db.portal_path_verified(db,'linkedin'):return Capability.DISCOVERY_ONLY,'Easy Apply worker implemented; a new live proof is still required'
        return Capability.APPLY_READY,'Exact Easy Apply proof valid; independent new-attempt limit '+str(config['portal_workers']['linkedin']['max_submissions_per_run'])
    def apply_lane(self,db,config,*,test_mode,max_submissions,application_job_id=None):
        import linkedin_easy_apply
        return linkedin_easy_apply.run(db,config,test_mode=test_mode,max_submissions=max_submissions,application_job_id=application_job_id)

class AtsWorker(PortalWorker):
    """Public Lever / Greenhouse / Ashby boards (plain HTTP feeds) and original-page verification."""
    name='ats'
    def capability(self,db,config):
        boards=[b for b in config['employer_boards'].values() if b['enabled']]
        if not boards:return Capability.BLOCKED,'No employer board is enabled'
        return Capability.DISCOVERY_ONLY,'Public feeds are read-only; application forms are not implemented'
    def discover(self,section,name,record,config,**_):
        import source_adapters
        return source_adapters.fetch_board(name,record)

class NaukriWorker(DiscoveryWorker):
    """Discovery via the browser bridge; the application lane is naukri_native_worker."""
    supports_apply=True
    def __init__(self):super().__init__('naukri')
    def discover(self,section,name,record,config,**options):
        import naukri_discovery
        return naukri_discovery.fetch(record,config['search'],**{k:v for k,v in options.items() if k in ('run_number','page_cap','query_cap','deadline')})
    def verify(self,db,leads,**options):
        import portal_native
        return portal_native.verify_leads(db,leads,**options)
    def capability(self,db,config):
        from pathlib import Path
        import naukri_native_worker as lane
        if not self._enabled(config):return Capability.BLOCKED,'Discovery is switched off in config'
        if lane._marker().is_file():return Capability.BLOCKED,'A role resume may still be active; restore is pending'
        if not config['naukri_worker']['enabled']:return Capability.DISCOVERY_ONLY,'Naukri application lane is switched off (naukri_worker.enabled)'
        if not _apply_flag(config,'naukri'):return Capability.DISCOVERY_ONLY,'portal_workers.naukri.apply_enabled is false'
        import job_config
        if not job_config.live_naukri_enabled(config):return Capability.DISCOVERY_ONLY,'Read-only mode: live Naukri applications are off'
        if not job_db.portal_path_verified(db,'naukri'):return Capability.DISCOVERY_ONLY,'Proof marker for the direct Naukri path is missing or invalid'
        return Capability.APPLY_READY,'Proof marker valid; independent new-attempt limit '+str(config['portal_workers']['naukri']['max_submissions_per_run'])
    def apply_lane(self,db,config,*,test_mode,max_submissions,application_job_id=None):
        import naukri_native_worker
        return naukri_native_worker.run(db,config,test_mode=test_mode,max_submissions=max_submissions,application_job_id=application_job_id)
