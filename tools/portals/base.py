"""Portal-worker interface. Workers only expose verbs; safety gates live in core (job_db, apply gates)."""
import enum

class Capability(str,enum.Enum):
    DISCOVERY_ONLY='DiscoveryOnly'
    APPLY_READY='ApplyReady'
    BLOCKED='Blocked'

class NotSupported(RuntimeError):
    pass

class PortalWorker:
    """Base class. `supports_apply` is a declaration, not a permission: see capability()."""
    name=''
    supports_apply=False

    def capability(self,db,config):
        raise NotImplementedError

    def session_status(self):
        """Cheap, non-browser health note. Real sign-in checks are explicit actions, never secrets."""
        return {'state':'unknown','detail':'No browser session check in status calls'}

    def discover(self,section,name,record,config,**options):
        raise NotSupported(self.name+' has no discovery')

    def verify(self,db,leads,**options):
        raise NotSupported(self.name+' has no portal-native verification')

    def apply_lane(self,db,config,*,test_mode,max_submissions):
        raise NotSupported(self.name+' has no application path; it stays discovery-only')

    def reconcile(self,db,attempt):
        raise NotSupported(self.name+' has no reconcile implementation yet')
