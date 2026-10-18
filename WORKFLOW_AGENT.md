# Local job-search stage contract

Read DAILY_WORKFLOW_AGENT.md for current safety rules and standing authorization.
Explicit `workflow run search|prepare|mail --execute` commands run only that stage
and stop before Submit. `workflow daily` runs the authorized daily stages.
Check workflow status before mutations. Optum/UHG 2388999 is completed and Rejected;
never retry it or change its submitted attachment. Use the generic master resume.

Initial search routes, rotate terms/locations across daily runs:
- https://www.naukri.com/java-spring-boot-jobs-in-pune?k=java%20spring%20boot&l=pune
- https://www.linkedin.com/jobs/search/?keywords=Java%20Spring%20Boot&location=India&f_E=2%2C3
- https://www.foundit.in/srp/results?query=java%20spring%20boot&locations=India
- https://in.indeed.com/jobs?q=java+spring+boot&l=India
- https://cutshort.io/jobs/java-jobs
- Original company careers/ATS pages linked from relevant roles.

Metadata, PDF, visual inspection, history deduplication, form, certification and
submission-proof requirements are in DAILY_WORKFLOW_AGENT.md. Never treat a
configured server, filled form, saved draft or job alert as successful submission.
