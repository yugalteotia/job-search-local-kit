"""Numeric and spelled-out experience ranges, preserving exact JD excerpts."""
import re

WORDS=('zero one two three four five six seven eight nine ten eleven twelve '
       'thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty').split()
NUMBER=r'(?:\d+(?:\.\d+)?|'+'|'.join(WORDS)+r')'
YEARS=re.compile(r'\b('+NUMBER+r')\s*(?:\+|(?:[-–]|to)\s*('+NUMBER+r'))?\s*(?:years?|yrs?)\b',re.I)

def number(value):
    text=str(value).casefold()
    return float(WORDS.index(text)) if text in WORDS else float(text)
