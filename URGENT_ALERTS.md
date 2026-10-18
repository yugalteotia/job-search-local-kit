# Urgent application handoffs

`workflow needs-answer <slug> <packet.json>` marks only that verified High-fit
job **Needs Answer**. Packet fields are `kind` (`unknown_required_answer`,
`captcha`, `login`, or `expiring_high_fit`) and a stable lowercase hex
`blocker_id` (SHA256 of the blocker question/step; never store the question or
answer here). For an expiring listing, add an observed timezone-aware `deadline`
and workspace-relative `deadline_evidence` from the original JD/capture.
Unsupported extra fields are rejected. Missing required answers are batched in
the normal digest. Login/CAPTCHA or an evidenced deadline within 24 hours are
urgent. Other jobs continue in the same run.

Desktop test:

```bash
python3 workflow alerts test-desktop
```

The command sends a critical notification with no timeout and plays the local
warning sound. Exit success from the notification and sound tools shows that
the desktop accepted the request; the notification daemon controls display.
`workflow alert-ack <slug>` closes the saved notification ID where supported
and stops repeats. It does **not** answer the form question. Desktop repeats
are no more frequent than once per six hours per job/blocker.

After an answer or login is actually verified, save a redacted resolution packet
with `resolved: true`, the matching `blocker_id`, workspace-relative `evidence`
file and `next_status` (`Shortlisted` or `Prepared`). Run
`python3 workflow alert-resolve <slug> <resolution.json>`; the job resumes while
unrelated applications remain untouched.

Optional phone setup and test:

```bash
python3 workflow alerts phone-setup
python3 workflow alerts test-phone
python3 workflow alerts confirm-phone
```

The first command uses hidden terminal input for the Pushover application token
and user key; credentials are written only to
`~/.config/job-search-private/pushover.json` (0600 in a 0700 directory), outside
the project. The test sends one emergency-priority **test** with a five-minute
expiry and returns only queued/unverified status. Acknowledge that test on your
phone, then `confirm-phone` checks Pushover's receipt and enables future alerts.
API acceptance alone is not proof it reached the device. Normal urgent alerts
use Pushover emergency priority 2, a five-minute retry interval and 30-minute
expiry. Only a generic title, safe job link and generic action leave the laptop;
no personal answers, form contents or account secrets are sent. One emergency
message is queued per job/blocker; Pushover handles repeats and stops on device
acknowledgment. `alert-ack` also requests receipt cancellation; if network
failure prevents cancellation, the remote repeat expires on its own.

The four daily search times and submission proof gates are unchanged. Phone
delivery is disabled until credentials and an acknowledged test exist.

API details: https://pushover.net/api and https://pushover.net/api/receipts
