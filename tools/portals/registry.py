"""Registry: the single place that maps configured sources to portal workers and computes capabilities."""
import job_db
from .base import Capability
from .workers import AtsWorker,CutshortWorker,DiscoveryWorker,NaukriWorker,LinkedInWorker

def workers():
    return {'naukri':NaukriWorker(),'linkedin':LinkedInWorker(),'foundit':DiscoveryWorker('foundit'),
            'indeed':DiscoveryWorker('indeed'),'cutshort':CutshortWorker('cutshort'),'ats':AtsWorker()}

def worker_for(section,name):
    table=workers()
    return table['ats'] if section=='employer_boards' else table[name]

def discover(section,name,record,config,**options):
    return worker_for(section,name).discover(section,name,record,config,**options)

def capabilities(db,config,persist=True):
    stamp=job_db.now();result={}
    for name,worker in workers().items():
        capability,reason=worker.capability(db,config)
        # A declaration of apply support never grants permission on its own.
        if capability is Capability.APPLY_READY and not worker.supports_apply:
            capability,reason=Capability.DISCOVERY_ONLY,'Worker has no application implementation'
        result[name]={'capability':capability.value,'reason':reason,'session':worker.session_status()['state']}
        if persist:
            db.execute('INSERT OR REPLACE INTO portal_capability VALUES(?,?,?,?,?)',(name,capability.value,reason,result[name]['session'],stamp))
    return result

def apply_lane_worker(db,config,name='naukri',allow_bootstrap=False):
    """The worker allowed to run its application lane, or None with the reason."""
    worker=workers()[name];capability,reason=worker.capability(db,config)
    if allow_bootstrap and name in ('naukri','linkedin') and worker.supports_apply:
        return worker,'Explicit manual exact-job proof run; unproven provider is never bootstrapped by timer'
    if capability is Capability.APPLY_READY and worker.supports_apply:return worker,reason
    return None,reason
