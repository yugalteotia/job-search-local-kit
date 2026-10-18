#!/usr/bin/env python3
"""Render a tailored one-page resume PDF for Yugal Teotia.

Usage:
  render_resume.py --out <file.pdf> [--spec <spec.json>]

Requires an explicit master-derived spec. Fonts are embedded; body and bullets
are left aligned. Rendering alone never establishes visual readiness.

Spec format (JSON):
{
  "summary": "one-paragraph professional summary",
  "skills": [{"label": "Backend", "items": "Java, Spring Boot, ..."}, ...],
  "experience": [
    {"title": "...", "org": "...", "location": "...", "dates": "...",
     "bullets": ["<b>Lead-in.</b> rest of bullet ...", ...]}
  ],
  "projects": [
    {"title": "...", "note": "...", "bullets": ["..."]}
  ],
  "education": [
    {"title": "...", "org": "...", "location": "...", "dates": "..."}
  ]
}
Bullets support <b>...</b>. Tailoring = edit summary, skill order/items,
bullet emphasis and ordering — NEVER invent content. Keep everything to one page.
"""
from local_paths import ROOT
import argparse
import json
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                HRFlowable, Spacer)

def _contact():
    import candidate
    who = candidate.load()['identity']
    phone = who['phone']
    spaced = phone[:3] + ' ' + phone[3:]
    return (
        f"{escape(who['location'])} | <a href=\"tel:{phone}\">{escape(spaced)}</a> | "
        f"<a href=\"mailto:{who['email']}\">{escape(who['email'])}</a><br/>"
        f"<a href=\"{who['linkedin']}\" color=\"#24516b\">LinkedIn</a> | "
        f"<a href=\"{who['github']}\" color=\"#24516b\">GitHub</a>"
    )

for name, filename in [('ResumeSans','LiberationSans-Regular.ttf'),
                       ('ResumeSans-Bold','LiberationSans-Bold.ttf'),
                       ('ResumeSans-Italic','LiberationSans-Italic.ttf'),
                       ('ResumeSans-BoldItalic','LiberationSans-BoldItalic.ttf')]:
    pdfmetrics.registerFont(TTFont(name, str(ROOT / 'assets/fonts' / filename)))
pdfmetrics.registerFontFamily('ResumeSans', normal='ResumeSans', bold='ResumeSans-Bold',
                             italic='ResumeSans-Italic', boldItalic='ResumeSans-BoldItalic')

def markup(text):
    return escape(str(text)).replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')

GENERIC = {
    "summary": (
        "Software Engineer with 2+ years of hands-on Java development building enterprise SaaS "
        "products. Designs and implements REST and gRPC APIs, Spring Boot microservices, and data "
        "access layers backed by MongoDB, with strong testing discipline (JUnit/Mockito) and CI/CD "
        "automation. Experienced in large-scale framework migrations, API contract evolution, and "
        "production debugging across distributed systems."
    ),
    "skills": [
        {"label": "Backend",
         "items": "Java, Spring Boot (2.6 \u2192 3.5), Spring Security 6, REST APIs, gRPC, Protocol Buffers, JPA/Hibernate, Microservices"},
        {"label": "Data",
         "items": "MongoDB (indexing, aggregations, Mongock/Mongobee migrations), MySQL"},
        {"label": "Frontend",
         "items": "TypeScript, Ember.js, React (basic), Jetpack Compose, Kotlin"},
        {"label": "Tooling & Practices",
         "items": "JUnit, Mockito, Git, GitHub Actions, Gradle, Maven, Docker, Linux, OpenAPI, Flyway"},
    ],
    "experience": [
        {"title": "Software Engineer", "org": "IDeaS - A SAS Company",
         "location": "Pune, India", "dates": "Oct 2024 \u2013 Sep 2026",
         "bullets": [
             "<b>Framework migration.</b> Led upgrade of Spring Boot 2.6 \u2192 3.5, Spring Security 6 and Jakarta EE migration across 20 Java modules; resolved dependency conflicts, deprecated APIs, and behavioral changes while keeping the build green.",
             "<b>REST API design.</b> Designed and implemented REST APIs with pagination, filtering, and audit metadata; introduced optimistic concurrency controls to prevent conflicting updates.",
             "<b>gRPC contracts.</b> Defined backward-compatible gRPC service contracts using Protocol Buffers and maintained contract-style tests for cross-service compatibility.",
             "<b>MongoDB optimization.</b> Optimized data access with targeted indexes and bulk read patterns; batched audit retrieval to reduce database round trips and improve report generation times.",
             "<b>Full-stack delivery.</b> Built configuration and reporting workflows in Ember.js with TypeScript, adding reusable components and validation logic.",
             "<b>AI-assisted engineering.</b> Used GitHub Copilot and agent-assisted workflows to accelerate implementation, root-cause analysis, debugging, and test scenario design, while owning design decisions and validation.",
         ]},
        {"title": "Application Developer", "org": "Data Nova India Pvt. Ltd.",
         "location": "Noida, India", "dates": "Feb 2023 \u2013 May 2023",
         "bullets": [
             "Built and maintained ERP modules as an Android developer using Kotlin, MVVM architecture, and REST API integrations.",
             "Implemented reusable UI components and collaborated on feature delivery within Agile sprints.",
         ]},
    ],
    "projects": [
        {"title": "UNITY \u2013 Music Sync Prototype", "note": "Personal Project",
         "bullets": [
             "Built a multi-device music synchronization prototype in Kotlin with Jetpack Compose and Media3/ExoPlayer, syncing playback position and state across clients in real time.",
             "Designed the system architecture and synchronization protocol; implemented core flows with AI-assisted development, reviewing all generated code against the design.",
             "Implemented local networking over TCP/UDP with coroutines for low-latency device coordination.",
         ]},
        {"title": "Store REST API", "note": "In Progress",
         "bullets": [
             "Building a Spring Boot REST API for store management with MySQL, Spring Data JPA/Hibernate, Flyway migrations, and OpenAPI documentation.",
             "Implementing layered architecture with DTOs, validation, pagination, and global exception handling.",
         ]},
    ],
    "education": [
        {"title": "PG Diploma in Advanced Computing (PG-DAC)", "org": "C-DAC",
         "location": "Pune, India", "dates": "Mar 2024 \u2013 Aug 2024"},
        {"title": "B.Tech, Computer Science & Engineering",
         "org": "Dr. A.P.J. Abdul Kalam Technical University",
         "location": "", "dates": "2020 \u2013 2023"},
    ],
}

NAME = ParagraphStyle("name", fontName="ResumeSans-Bold", fontSize=18, leading=22,
                      alignment=1, spaceAfter=2)
CONTACT_S = ParagraphStyle("contact", fontName="ResumeSans", fontSize=9.3, leading=12.5,
                           alignment=1, spaceAfter=6, textColor="#333333")
HEADING = ParagraphStyle("heading", fontName="ResumeSans-Bold", fontSize=10.5, leading=14,
                         spaceBefore=8, spaceAfter=3, textColor="#111111", keepWithNext=True)
BODY = ParagraphStyle("body", fontName="ResumeSans", fontSize=9.5, leading=13,
                      spaceBefore=2, alignment=0)
SKILL = ParagraphStyle("skill", fontName="ResumeSans", fontSize=9.5, leading=13, spaceBefore=2)
JOBHEAD = ParagraphStyle("jobhead", fontName="ResumeSans", fontSize=9.5, leading=13, spaceBefore=5, keepWithNext=True)
ORG = ParagraphStyle("org", fontName="ResumeSans-Italic", fontSize=9.5, leading=13,
                    spaceBefore=0, spaceAfter=1)
BULLET = ParagraphStyle("bullet", parent=BODY, leftIndent=14, firstLineIndent=0,
                        bulletIndent=3, bulletFontName='ResumeSans', bulletFontSize=9.5, spaceBefore=2)


def heading(text):
    return [Paragraph(f"<b>{text}</b>", HEADING),
            HRFlowable(width="100%", thickness=0.6, color="#444444", spaceAfter=2)]


def job_header(title, dates, org=None, location=None):
    title, dates = markup(title), markup(dates)
    org = markup(org) if org else None
    location = markup(location) if location else None
    line = (f'<b>{title}</b> <font color="#555555">|</font> {dates}'
            if not org else f'<b>{title}</b>, {org} <font color="#555555">|</font> {dates}')
    if location:
        line += f' <font color="#555555">|</font> {location}'
    return Paragraph(line, JOBHEAD)


def build(spec, out):
    doc = BaseDocTemplate(out, pagesize=A4,
                          leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                          topMargin=0.42 * inch, bottomMargin=0.42 * inch,
                          title="Yugal Teotia - Resume",
                          author="Yugal Teotia", initialFontName='ResumeSans')
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame])])
    import candidate
    story = [Paragraph(markup(candidate.load()['identity']['name']), NAME),
             Paragraph(_contact(), CONTACT_S)]
    story += heading("Professional Summary")
    story.append(Paragraph(markup(spec["summary"]), BODY))
    story += heading("Technical Skills")
    for s in spec["skills"]:
        story.append(Paragraph(f"<b>{markup(s['label'])}:</b> {markup(s['items'])}", SKILL))
    story += heading("Professional Experience")
    for job in spec["experience"]:
        story.append(job_header(job["title"], job["dates"], job.get("org"), job.get("location")))
        for b in job["bullets"]:
            story.append(Paragraph(markup(b), BULLET, bulletText="\u2022"))
    story += heading("Projects")
    for p in spec["projects"]:
        note = f" <font color=\"#555555\">({markup(p['note'])})</font>" if p.get("note") else ""
        story.append(Paragraph(f"<b>{markup(p['title'])}</b>{note}", JOBHEAD))
        if p.get("stack"):
            story.append(Paragraph(markup(p["stack"]), ORG))
        for b in p["bullets"]:
            story.append(Paragraph(markup(b), BULLET, bulletText="\u2022"))
    story += heading("Education")
    for e in spec["education"]:
        story.append(job_header(e["title"], e["dates"], e.get("org"), e.get("location")))
    doc.build(story)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--spec", default=None)
    args = ap.parse_args()
    if not args.spec:
        raise SystemExit("Use workflow tailor: the legacy GENERIC constant is not the source of truth. --spec is required.")
    spec = {}
    if args.spec:
        with open(args.spec) as f:
            user = json.load(f)
        for k, v in user.items():
            spec[k] = v
    build(spec, args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
