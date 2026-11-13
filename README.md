# UW–Madison Seat Checker

A small, single-user script that checks UW-Madison course availability and sends an
[ntfy](https://ntfy.sh/) notification when real seats become available.

It is designed to be run by cron. Each run checks the configured courses once, compares
the results with the previous saved snapshot, sends any needed notifications, and exits.

Built in a couple hours with OpenCode and Big Pickle.

## Requirements

- Python 3.11 or newer
- No third-party Python packages
- A phone or desktop client subscribed to an ntfy topic

## How it works

For each configured course, the script requests the course's public enrollment-package
data from the university's course search service. The response is a list of enrollment
packages, or sometimes a single package object. Each package describes one enrollment unit
for the course, such as a lecture or a lecture with its required discussion or lab.

The script saves a compact snapshot in `state.json`. On later runs it compares each
package's `packageEnrollmentStatus.availableSeats` with the saved value. A notification is
sent for a course when any package has real seats available and either:

- the package is new, or
- `availableSeats` increased since the previous check.

`availableSeats` is the only value that can trigger a notification. `openSeats` and the
package status are stored for context, and the package status appears in notifications.
Neither can trigger an alert. `openSeats` can include seats that are being offered to a
waitlist, so it is not treated as real availability.

The waitlist count is not an independent trigger. A full class whose waitlist drops to zero
does not produce a notification. If real seats become available, the notification includes
the waitlist change as context.

### First run

The first run with no saved state sends one setup notification so you can confirm that ntfy
is configured correctly. It then sends normal per-course notifications for any course that
already has real seats available. Later runs send only per-course seat notifications.

### Failures

If a course request fails, returns malformed data, or is blocked by a rate limit or web
application firewall, the previous snapshot for that course is preserved. Failures never
count as zero availability. HTTP 202, 403, and 429 responses are treated as failures, as is
a 200 response that is not valid JSON or does not contain a usable package.

If no courses can be checked successfully, the script exits with status 1. Partial failures
are logged and the successful courses continue to be processed.

After two consecutive runs in which no course can be checked, the script sends one failure
notification. It does not repeat that alert for the same failure streak; any successful
course check resets the streak.

## Quick start

```bash
cp config.example.toml config.toml
```

Edit `config.toml`:

- Set `ntfy_topic` to your ntfy topic.
- Set `term` to the term code you want to watch.
- Replace the example courses with the courses you want to watch.
- Set `NTFY_TOKEN` in the environment if your ntfy topic is protected.

Try a dry run first. It fetches courses and reports what would happen without sending
notifications or updating state:

```bash
python3 seat_checker.py --dry-run
```

Run normally:

```bash
python3 seat_checker.py
```

## Configuration

See `config.example.toml` for a complete example.

```toml
term = "1274"
state_file = "state.json"

ntfy_server = "https://ntfy.sh"
ntfy_topic = "replace-with-your-topic"
ntfy_token_env = "NTFY_TOKEN"
ntfy_priority = 4

request_delay_seconds = 1.0
timeout_seconds = 20

[[courses]]
label = "COMP SCI 542"
subject_code = "266"
course_id = "025893"
```

### Course identifiers

Each course needs the internal `term`, `subject_code`, and `course_id`. The catalog number
(for example `542`) is not the same as `course_id`. `label` is optional and is only used in
logs and notifications. If it is omitted, the script uses the course name from the response
when possible.

The example config uses these values:

| Course | term | subject_code | course_id |
| --- | --- | --- | --- |
| COMP SCI 542 | 1274 | 266 | 025893 |
| L I S 461 | 1274 | 544 | 025394 |

Keep `course_id` quoted so leading zeros are preserved. An unquoted numeric course ID is
also accepted and is zero-padded to six digits.

### Optional settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `state_file` | `state.json` | Snapshot file. Relative paths are resolved from the config file's directory. |
| `ntfy_server` | `https://ntfy.sh` | ntfy server base URL. |
| `ntfy_token_env` | `NTFY_TOKEN` | Environment variable containing an optional ntfy access token. |
| `ntfy_priority` | `4` | ntfy priority from 1 to 5. |
| `request_delay_seconds` | `1.0` | Delay between course requests. |
| `timeout_seconds` | `20` | HTTP timeout for course and ntfy requests. |
| `user_agent` | built in | User-Agent sent with requests. |

Put access tokens in environment variables, not in `config.toml`.

## ntfy setup

1. Install the ntfy app or open the ntfy web client.
2. Subscribe to a topic. Use a long, hard-to-guess topic name.
3. Put that topic in `ntfy_topic`.
4. If you use access control, create a token and expose it as `NTFY_TOKEN` when the script
   runs.

The script sends JSON to the configured ntfy server with one notification per changed
course. Seat notifications include the course label, section, previous and current seat
counts, status, waitlist context, and a link to the course search page.

## State file

`state.json` is required for detecting changes. Keep it on persistent storage and do not
edit it manually.

The script writes state atomically, so an interrupted run does not leave a partial file.
A lock file (`state.json.lock`) prevents overlapping cron runs from sending duplicate
notifications.

The state also records each course's last successful check time and the current consecutive
total-failure streak used for health alerts.

To intentionally reset the baseline, stop any running checks and delete `state.json`. The
next run will be treated as a first run and will send the setup notification again.

If the state file is present but invalid, the script exits with an error instead of silently
starting over.

## Cron

Run every thirty minutes:

```cron
*/30 * * * * cd /path/to/seat-checker && /usr/bin/python3 seat_checker.py >> seat-checker.log 2>&1
```

Use an absolute path to Python if cron's environment does not include the right interpreter.
If you use an ntfy token, define it in the cron environment or in a wrapper script.

The script requests courses sequentially and waits one second between requests by default.

## Diagnostics

Check the configuration and saved state without contacting the course search service:

```bash
python3 seat_checker.py --check-config
```

Show the last saved seat counts and failure status without fetching anything:

```bash
python3 seat_checker.py --status
```

Both commands accept `--config` and `--state` if you use non-default paths.

## Testing

The test suite uses only the Python standard library and does not make network requests:

```bash
python3 -m unittest discover -s tests -v
```

## Troubleshooting

- **`HTTP 202`, `HTTP 403`, or `HTTP 429`**: the course search service blocked or
  rate-limited the request. Wait and try again, and keep the polling interval reasonable.
- **`response was not valid JSON`**: the server returned an HTML or challenge page instead
  of the expected response. Treat this as a temporary failure.
- **`no usable availableSeats`**: a package was missing the field the script relies on. The
  previous snapshot for that package is preserved.
- **`another seat checker run is already active`**: a previous cron run is still in
  progress. The new run exits without sending anything.
- **No notifications after a change**: check `state.json`, the configured `term`, and the
  log. A course that is full or whose `availableSeats` is zero will not notify.

## Notes

This is an unofficial tool. Use a reasonable polling interval, keep your ntfy topic private,
and remember that term codes and course IDs change between semesters. Update `config.toml`
when you start watching a new term.
