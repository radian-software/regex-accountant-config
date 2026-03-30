## Cash App fetcher notes

Sometimes the pages take a really long time to load for some reason.
The p90 latency is off the charts.

I've also (2023-09-21) started seeing an issue where after the pin
entry step, the profile request still gets a 401 response for some
reason, and you get redirected back to the start of the flow. There is
no error information in any of the XHR requests I looked at. From my
testing, this appeared to be an issue where they had denylisted
something about my existing cookies rather than anything about
fingerprinting the login flow. When I used `--force-new-session` this
worked around the issue. It might be a TTL issue.

Saw the same issue as above on 2024-01-04, confirmed the same
workaround is still appropriate.

I have looked at using their API directly instead of through the
browser, it is kind of a pain though. They are using some aggressive
Cloudflare anti-scraping fingerprinting on the http headers and
cookies, and furthermore the email registration request that starts
things off has a pretty intense rate limit, I think ~10 per day per
email, so it is hard to test.

## Fidelity fetcher notes

The Fidelity login sometimes errors out with "service not available
right now" or other such bullshit, when you hit a rate limit. This
happens a lot more frequently when operating within Selenium, although
I'm not sure why.

## PayPal fetcher notes

There are a SHIT TON of captchas. If the auth check fails immediately
after login, and the auth flow didn't have to enter any credentials,
it's likely a bad session for whatever reason, this seems to happen a
lot. Re-running with `--force-new-session` fixes it.

## Vanguard fetcher notes

They updated all the "sequence numbers" and changed a bunch of dates
and transaction ordering on me at some point in late 2023 it seems.
Not sure why that happened. Should I come up with a new UID scheme?

## Zelle fetcher notes

The `api_username`, `api_password`, and `sdk_unlock_key` don't appear
to change when they upgrade the app. But every so often (sometimes
surprisingly fast) they denylist the older app version. Usually it is
enough to bump the `app_version` and `app_version_code` in the
headers, but we will see.

There still seems to be something missing from my implementation,
because it gets a 404 on the `Connecting teid with user account
password` step, but then if I login via the app once, it stops getting
a 404 on that step. Even though there is no connection between the
credentials being used in the two clients. Something funny is going on
there where something is getting registered to, like, my phone number
or something in the db, and it is getting reused.

Whatever the thing is, though, it's got a TTL of at least a day,
because I checked back and did another login the next day, and that
worked fine. It seems to have a TTL of less than three days though,
because checking back again the day after, it is giving 404 again.

Note 2026-03-29 moved the Zelle integration to
<https://github.com/radian-software/zelle-app-notes>, symlink from
there if you want to use it but the app is dead anyway so probably
doesn't matter.
