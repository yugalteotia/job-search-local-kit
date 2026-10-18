"""Employer-facing names, independent of internal resume revisions."""
import re
import unicodedata

def employer_filename(role, company='', requisition=''):
    title = unicodedata.normalize('NFKC', role)
    for value in (company, str(requisition or '')):
        if value:
            title = re.sub(r'(?<!\w)' + re.escape(value) + r'(?!\w)', ' ', title, flags=re.I)
    title = re.sub(r'\b(?:level|grade|band|requisition|req|job\s*id)\s*[:#-]?\s*[A-Z0-9]+\b', ' ', title, flags=re.I)
    title = re.sub(r'\b(?:I|II|III|IV|V|VI|VII|VIII|IX|L\d+|IC\d+|SDE\d+|v\d+|\d+)\b', ' ', title, flags=re.I)
    title = re.sub(r'\b(?:Tailored|Draft|Final|AI|Version)\b', ' ', title, flags=re.I)
    title = re.sub(r'\bfull[\s-]*stack\b', 'Full Stack', title, flags=re.I)
    title = re.sub(r'\bback[\s-]*end\b', 'Backend', title, flags=re.I)
    words = re.findall(r'[A-Za-z][A-Za-z0-9]*', title)
    if not words:
        raise ValueError('Posting needs an accurate role title before naming its resume')
    return 'Yugal_Teotia_' + '_'.join(w if w.isupper() and len(w)>1 else w.capitalize() for w in words) + '.pdf'
