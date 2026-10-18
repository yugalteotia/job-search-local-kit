"""Documented, public, allowlisted ATS posting feeds; GET only."""
from dataclasses import asdict,dataclass
import datetime as dt
from html.parser import HTMLParser
import json
import re
from urllib.error import HTTPError,URLError
from urllib.parse import quote,urlsplit,urlunsplit
from urllib.request import HTTPRedirectHandler,Request,build_opener

DOCS={
 'lever':'https://github.com/lever/postings-api/blob/master/README.md',
 'greenhouse':'https://docs.greenhouse.io/job-board.html',
 'ashby':'https://developers.ashbyhq.com/docs/public-job-posting-api',
}
FEED_HOSTS={'lever':'api.lever.co','greenhouse':'boards-api.greenhouse.io','ashby':'api.ashbyhq.com'}
ORIGINAL_HOSTS={'lever':{'jobs.lever.co'},'greenhouse':{'job-boards.greenhouse.io','boards.greenhouse.io'},'ashby':{'jobs.ashbyhq.com'}}

def now():return dt.datetime.now(dt.timezone.utc).isoformat()
class Text(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.skip+=1
        if tag in ('p','br','li','h1','h2','h3','div'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style') and self.skip:self.skip-=1
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)
def plain_html(value):
    parser=Text();parser.feed(str(value or ''))
    return re.sub(r'\n{3,}','\n\n',re.sub(r'[ \t]+',' ',' '.join(parser.parts))).strip()

@dataclass(frozen=True)
class NormalizedJob:
    source:str
    employer:str
    title:str
    location:str|None
    requisition_id:str|None
    canonical_url:str
    discovered_at:str
    posted_at:str|None
    checked_at:str
    jd_text:str
    availability:str
    provenance:dict
    def validate(self,provider):
        if not self.employer or not self.title or not self.jd_text.strip():raise ValueError('Original JD fields missing')
        parsed=urlsplit(self.canonical_url)
        if parsed.scheme!='https' or parsed.hostname not in ORIGINAL_HOSTS[provider] or parsed.username or parsed.password:
            raise ValueError('Original URL outside documented ATS host allowlist')
        return self
    def record(self):return asdict(self)

class SameHostRedirect(HTTPRedirectHandler):
    def __init__(self,host):self.host=host
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        parsed=urlsplit(newurl)
        if parsed.scheme!='https' or parsed.hostname!=self.host:
            raise ValueError('Feed redirect escaped the allowlisted API host')
        return super().redirect_request(request,fp,code,msg,headers,newurl)

def _get_json(url,host,timeout=12):
    if urlsplit(url).scheme!='https' or urlsplit(url).hostname!=host:raise ValueError('Feed URL outside allowlist')
    opener=build_opener(SameHostRedirect(host))
    req=Request(url,headers={'Accept':'application/json','User-Agent':'job-search-local-kit/2 (public postings GET)'})
    with opener.open(req,timeout=timeout) as response:
        if response.status!=200:raise ValueError('Unexpected feed status')
        if 'json' not in response.headers.get('Content-Type','').lower():raise ValueError('ATS feed did not return JSON')
        body=response.read(6_000_001)
        if len(body)>6_000_000:raise ValueError('ATS feed exceeded 6 MB bound')
    return json.loads(body)

def canonical(url,provider):
    parsed=urlsplit(str(url or ''))
    if parsed.scheme!='https' or parsed.hostname not in ORIGINAL_HOSTS[provider] or parsed.username or parsed.password:
        raise ValueError('Employer posting URL outside provider allowlist')
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))

def _normal(provider,source,employer,board,item,feed_url,checked):
    if provider=='lever':
        req=str(item.get('id') or '') or None
        title=item.get('text') or ''
        location=(item.get('categories') or {}).get('location')
        url=item.get('hostedUrl') or item.get('applyUrl')
        description=item.get('descriptionPlain') or plain_html(item.get('description'))
        posted=None # Lever createdAt is not a documented posting date.
    elif provider=='greenhouse':
        req=str(item.get('id') or '') or None
        title=item.get('title') or ''
        location=(item.get('location') or {}).get('name')
        url=item.get('absolute_url')
        description=plain_html(item.get('content'))
        posted=None # updated_at is not a posting date.
    else:
        title=item.get('title') or ''
        location=item.get('location')
        url=item.get('jobUrl')
        part=urlsplit(str(url or '')).path.rstrip('/').split('/')[-1]
        req=part if re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}',part) else None
        description=item.get('descriptionPlain') or plain_html(item.get('descriptionHtml'))
        posted=item.get('publishedAt')
    if not url or not title or not description:return None
    original=canonical(url,provider)
    if not urlsplit(original).path.lstrip('/').lower().startswith(board.lower()+'/'):
        raise ValueError('Posting URL does not belong to configured public board')
    job=NormalizedJob(source,employer,title,location,req,original,checked,posted,checked,
                      f'Title: {title}\nLocation: {location or "Not stated"}\n\n{description}',
                      'Live',{'provider':provider,'board':board,'feed_url':feed_url,'documentation':DOCS[provider],'fetched_at':checked})
    return job.validate(provider)

def fetch_board(name,config,*,get_json=_get_json):
    if not config['enabled']:return {'source':name,'status':'Disabled','pages':[],'jobs':[],'error_kind':None,'checked_at':now()}
    provider=config['type'];board=config['board'];employer=config['employer']
    if provider not in FEED_HOSTS:raise ValueError('Unsupported public ATS adapter')
    checked=now();pages=[];jobs=[];status='Success';error=None
    try:
        if provider=='lever':
            for page in range(config['max_pages']):
                url=f'https://api.lever.co/v0/postings/{quote(board)}?skip={page*config["page_size"]}&limit={config["page_size"]}&mode=json'
                data=get_json(url,FEED_HOSTS[provider]);items=data if isinstance(data,list) else None
                if items is None:raise ValueError('Lever feed schema mismatch')
                pages.append({'page':page+1,'url':url,'items':len(items),'checked_at':now(),'status':'Success'})
                for item in items:
                    normalized=_normal(provider,name,employer,board,item,url,checked)
                    if normalized:jobs.append(normalized.record())
                if len(items)<config['page_size']:break
            else:status='Partial' # Could be another page; do not claim full sweep.
        else:
            url=(f'https://boards-api.greenhouse.io/v1/boards/{quote(board)}/jobs?content=true' if provider=='greenhouse'
                 else f'https://api.ashbyhq.com/posting-api/job-board/{quote(board)}')
            data=get_json(url,FEED_HOSTS[provider])
            items=data.get('jobs') if isinstance(data,dict) else None
            if not isinstance(items,list):raise ValueError('ATS feed schema mismatch')
            # Greenhouse exposes updated_at, not a posting date; use it only for recency ordering.
            if provider=='greenhouse':items=sorted(items,key=lambda x:x.get('updated_at') or '',reverse=True)
            selected=items[:config['page_size']]
            pages.append({'page':1,'url':url,'items':len(selected),'total_returned':len(items),'checked_at':now(),
                          'status':'Partial' if len(items)>len(selected) else 'Success'})
            if len(items)>len(selected):status='Partial'
            for item in selected:
                if provider=='ashby' and item.get('isListed') is False:continue
                normalized=_normal(provider,name,employer,board,item,url,checked)
                if normalized:jobs.append(normalized.record())
    except HTTPError as exc:
        status='Blocked' if exc.code in (401,403,429) else 'Failed'
        error=f'http_{exc.code}'
    except (URLError,TimeoutError,OSError,ValueError,json.JSONDecodeError) as exc:
        status='Failed';error=(type(exc).__name__+': '+str(exc))[:180]
    return {'source':name,'status':status,'pages':pages,'jobs':jobs,'error_kind':error,'checked_at':checked,
            'fetch_succeeded':bool(pages)}

def verify_original(job,*,timeout=10):
    """Check the exact hosted ATS posting; never follow a redirect off its host."""
    provider=job['provenance']['provider']
    url=canonical(job['canonical_url'],provider)
    host=urlsplit(url).hostname
    opener=build_opener(SameHostRedirect(host))
    request=Request(url,headers={'Accept':'text/html','User-Agent':'job-search-local-kit/2 (posting verification GET)'})
    try:
        with opener.open(request,timeout=timeout) as response:
            body=response.read(1_000_001)
            if response.status!=200 or len(body)>1_000_000:raise ValueError('Original page status or size invalid')
            content_type=response.headers.get('Content-Type','').lower()
            if 'html' not in content_type:raise ValueError('Original page is not HTML')
        title=re.sub(r'[^a-z0-9]+',' ',job['title'].lower()).strip()
        page=re.sub(r'[^a-z0-9]+',' ',plain_html(body.decode('utf-8','replace')).lower())
        if not title or title not in page:raise ValueError('Original page does not show the feed title')
        return {'verified':True,'checked_at':now(),'url':url,'error_kind':None}
    except HTTPError as exc:return {'verified':False,'checked_at':now(),'url':url,'error_kind':f'http_{exc.code}'}
    except (URLError,TimeoutError,OSError,ValueError) as exc:
        return {'verified':False,'checked_at':now(),'url':url,'error_kind':type(exc).__name__}

class PostingPage(HTMLParser):
    """Extract visible posting copy and public JobPosting metadata from one ATS page."""
    def __init__(self):
        super().__init__(convert_charrefs=True);self.visible=[];self.meta={};self.jsonld=[]
        self._script=False;self._jsonld=False;self._script_text=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta':
            key=(a.get('name') or a.get('property') or '').lower()
            if key in ('description','og:description','og:title','og:site_name') and a.get('content'):
                self.meta[key]=a['content']
        if tag=='script':
            self._script=True;self._jsonld=a.get('type','').lower()=='application/ld+json';self._script_text=[]
        elif tag in ('p','br','li','h1','h2','h3','h4','div','section'):
            self.visible.append('\n')
    def handle_endtag(self,tag):
        if tag=='script' and self._script:
            if self._jsonld:
                try:self.jsonld.append(json.loads(''.join(self._script_text)))
                except (ValueError,TypeError):pass
            self._script=False;self._jsonld=False;self._script_text=[]
    def handle_data(self,data):
        if self._script:
            if self._jsonld:self._script_text.append(data)
        else:self.visible.append(data)

def _jobposting(value):
    if isinstance(value,list):
        for part in value:
            found=_jobposting(part)
            if found:return found
    elif isinstance(value,dict):
        typ=value.get('@type')
        if typ=='JobPosting' or isinstance(typ,list) and 'JobPosting' in typ:return value
        graph=value.get('@graph')
        if graph:return _jobposting(graph)
    return None

def verify_direct_requisition(url,provider,*,timeout=12):
    """Read one exact public ATS requisition page; no arbitrary host or redirect."""
    exact=canonical(url,provider);host=urlsplit(exact).hostname
    opener=build_opener(SameHostRedirect(host))
    request=Request(exact,headers={'Accept':'text/html','User-Agent':'job-search-local-kit/2 (public posting verification GET)'})
    checked=now()
    try:
        with opener.open(request,timeout=timeout) as response:
            body=response.read(1_000_001)
            if response.status!=200 or len(body)>1_000_000:raise ValueError('original_page_status_or_size_invalid')
            content_type=response.headers.get('Content-Type','').lower()
            if 'html' not in content_type:raise ValueError('original_page_not_html')
        parser=PostingPage();parser.feed(body.decode('utf-8','replace'))
        visible=plain_html(''.join(parser.visible))
        metadata=_jobposting(parser.jsonld)
        description=plain_html(metadata.get('description','')) if metadata else ''
        # A JSON-LD JobPosting is direct source evidence. Otherwise use the exact
        # publicly rendered page text and require the substantive-JD gate below.
        title=str(metadata.get('title') or parser.meta.get('og:title') or '').strip() if metadata else str(parser.meta.get('og:title') or '').strip()
        location=''
        if metadata:
            place=metadata.get('jobLocation') or {}
            if isinstance(place,list):place=place[0] if place else {}
            address=place.get('address',{}) if isinstance(place,dict) else {}
            if isinstance(address,list):address=address[0] if address else {}
            if isinstance(address,dict):location=', '.join(str(address.get(k)) for k in ('addressLocality','addressRegion','addressCountry') if address.get(k))
        posted=(metadata or {}).get('datePosted')
        organization=(metadata or {}).get('hiringOrganization') or {}
        if isinstance(organization,list):organization=organization[0] if organization else {}
        employer=str(organization.get('name') or parser.meta.get('og:site_name') or '').strip() if isinstance(organization,dict) else ''
        if not title:
            title_match=re.search(r'<title[^>]*>(.*?)</title\s*>',body.decode('utf-8','replace'),re.I|re.S)
            title=plain_html(title_match.group(1)) if title_match else ''
        if not description:
            # Remove navigation/footer noise conservatively; substantive copy must
            # still contain a recognizable duties and qualifications section.
            description=visible
        page_low=(title+' '+visible[:1200]).lower()
        if re.search(r'position (?:is )?(?:no longer available|filled)|job (?:is )?closed|page not found|404 not found',page_low):
            return {'verified':False,'status':'Unavailable','checked_at':checked,'url':exact,'error_kind':'posting_closed_or_not_found'}
        if not title or len(title)<3:
            return {'verified':False,'status':'Unresolved','checked_at':checked,'url':exact,'error_kind':'posting_title_missing'}
        substantive=len(description)>=250 and bool(re.search(r'responsibilit|what you.?ll do|duties|qualifications|requirements|what we.?re looking for|about the role',description,re.I))
        if not substantive:
            return {'verified':False,'status':'LiveTitleOnly','checked_at':checked,'url':exact,'title':title,
                    'error_kind':'live_page_has_no_substantive_duties_and_qualifications','visible_chars':len(description)}
        req=urlsplit(exact).path.rstrip('/').split('/')[-1]
        return {'verified':True,'status':'VerifiedSubstantiveJD','checked_at':checked,'url':exact,
                'title':title,'employer':employer or None,'location':location or None,'requisition_id':req,'posted_at':posted,
                'jd_text':description,'evidence':{'http_status':200,'same_host':host,'title':title,
                'description_chars':len(description),'sections_detected':re.findall(r'responsibilit\w*|duties|qualifications|requirements|what you.?ll do|about the role',description,re.I)}}
    except HTTPError as exc:
        return {'verified':False,'status':'Unavailable' if exc.code==404 or exc.code==410 else 'Blocked' if exc.code in (401,403,429) else 'Failed',
                'checked_at':checked,'url':exact,'error_kind':f'http_{exc.code}'}
    except (URLError,TimeoutError,OSError,ValueError) as exc:
        return {'verified':False,'status':'Failed','checked_at':checked,'url':exact,'error_kind':str(exc)[:160] or type(exc).__name__}

def verify_linked_employer_requisition(url,source_host,rule,*,timeout=12):
    """Verify only an HTTPS job URL surfaced as an Apply link by an enabled portal."""
    checked=now();parsed=urlsplit(str(url or ''))
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.hostname==source_host:
        return {'verified':False,'status':'Unresolved','checked_at':checked,'url':str(url),'error_kind':'apply_link_not_a_distinct_https_employer_url'}
    if not re.search(rule['path_pattern'],parsed.path,re.I):
        return {'verified':False,'status':'Unresolved','checked_at':checked,'url':str(url),'error_kind':'apply_link_path_not_job_specific'}
    # Never persist likely session, authentication, or personal-data parameters.
    from urllib.parse import parse_qsl,urlencode
    pairs=parse_qsl(parsed.query,keep_blank_values=True)
    if any(re.search(r'token|auth|session|cookie|otp|email|password|state|code',key,re.I) for key,_ in pairs):
        return {'verified':False,'status':'Unresolved','checked_at':checked,'url':parsed.scheme+'://'+parsed.netloc+parsed.path,'error_kind':'sensitive_query_parameter_skipped'}
    exact=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(pairs),''));host=parsed.hostname
    opener=build_opener(SameHostRedirect(host));request=Request(exact,headers={'Accept':'text/html','User-Agent':'job-search-local-kit/2 (portal-linked employer posting GET)'})
    try:
        with opener.open(request,timeout=timeout) as response:
            body=response.read(1_000_001)
            if response.status!=200 or len(body)>1_000_000:raise ValueError('original_page_status_or_size_invalid')
            if 'html' not in response.headers.get('Content-Type','').lower():raise ValueError('original_page_not_html')
        parser=PostingPage();parser.feed(body.decode('utf-8','replace'));visible=plain_html(''.join(parser.visible));metadata=_jobposting(parser.jsonld)
        title=str(metadata.get('title') or parser.meta.get('og:title') or '').strip() if metadata else str(parser.meta.get('og:title') or '').strip()
        if not title:
            match=re.search(r'<title[^>]*>(.*?)</title\s*>',body.decode('utf-8','replace'),re.I|re.S);title=plain_html(match.group(1)) if match else ''
        jd=plain_html(metadata.get('description','')) if metadata else ''
        if not jd:jd=visible
        organization=(metadata or {}).get('hiringOrganization') or {};employer=str(organization.get('name') or parser.meta.get('og:site_name') or '').strip() if isinstance(organization,dict) else ''
        place=(metadata or {}).get('jobLocation') or {};place=place[0] if isinstance(place,list) and place else place
        address=place.get('address',{}) if isinstance(place,dict) else {};address=address[0] if isinstance(address,list) and address else address
        location=', '.join(str(address.get(k)) for k in ('addressLocality','addressRegion','addressCountry') if address.get(k)) if isinstance(address,dict) else ''
        if re.search(r'position (?:is )?(?:no longer available|filled)|job (?:is )?closed|page not found|404 not found',(title+' '+visible[:1000]).lower()):
            return {'verified':False,'status':'Unavailable','checked_at':checked,'url':exact,'error_kind':'posting_closed_or_not_found'}
        if not title:return {'verified':False,'status':'Unresolved','checked_at':checked,'url':exact,'error_kind':'posting_title_missing'}
        if len(jd)<250 or not re.search(r'responsibilit|what you.?ll do|duties|qualifications|requirements|what we.?re looking for|about the role',jd,re.I):
            return {'verified':False,'status':'LiveTitleOnly','checked_at':checked,'url':exact,'title':title,'error_kind':'live_page_has_no_substantive_duties_and_qualifications','visible_chars':len(jd)}
        segment=parsed.path.rstrip('/').split('/')[-1]
        req_match=re.fullmatch(r'(?:[0-9]{5,}|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|(?:req|job|position)[-_]([A-Za-z0-9_-]{5,}))',segment)
        req=(req_match.group(1) if req_match and req_match.lastindex else segment if req_match else None)
        return {'verified':True,'status':'VerifiedSubstantiveJD','checked_at':checked,'url':exact,'title':title,'employer':employer or None,
                'location':location or None,'requisition_id':req,
                'posted_at':(metadata or {}).get('datePosted'),'jd_text':jd,
                'evidence':{'http_status':200,'same_host_redirects_only':True,'portal_apply_source':source_host,
                            'direct_host':host,'title':title,'description_chars':len(jd)}}
    except HTTPError as exc:
        return {'verified':False,'status':'Unavailable' if exc.code in (404,410) else 'Blocked' if exc.code in (401,403,429) else 'Failed',
                'checked_at':checked,'url':exact,'error_kind':f'http_{exc.code}'}
    except (URLError,TimeoutError,OSError,ValueError) as exc:
        return {'verified':False,'status':'Failed','checked_at':checked,'url':exact,'error_kind':str(exc)[:160] or type(exc).__name__}
