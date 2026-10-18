import job_db
from portals import registry

def status(config):
    db=job_db.connect();job_db.initialize(db)
    try:return registry.capabilities(db,config)
    finally:db.close()
