"""Build an explicitly partial preview from already captured Gmail metadata."""
import workflow as w
import tracker_views as tv

def main():
    source=w.read_json(w.ROOT/'captures/gmail-search-metadata.json',{})
    records=[]
    for row in source.get('rows',[]):
        records.append({'source':'Gmail saved search metadata','company':None,'role':None,'requisition_id':None,'date':row.get('date'),'kind':'alert' if 'Job Alerts' in row.get('sender','') else 'unknown','message_id':row.get('message_id'),'subject':row.get('subject'),'evidence':'captures/gmail-search-metadata.json'})
    coverage={'Gmail':'Partial: existing requisition/company search only; saved results contain alerts. Full acknowledgment/recruiter search pending Chrome connection.', **{name:'Not collected: read-only Chrome connection timed out; no claim of empty history' for name in ['Naukri','LinkedIn','Foundit','Indeed','Cutshort']}}
    path=w.ROOT/'captures/historical-backfill.json'
    w.write_json(path,{'records':records,'coverage':coverage})
    result=tv.preview(w,path)
    print({'proposed':sum(p['decision']=='proposed' for p in result['proposals']),'ignored':sum(p['decision']=='ignore' for p in result['proposals']),'preview':'views/historical-preview.md','coverage':'partial; live collection pending'})

if __name__=='__main__':main()
