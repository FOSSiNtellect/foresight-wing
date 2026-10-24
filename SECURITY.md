# Security

Foresight Wing handles private meeting audio, transcripts and files, so security reports matter a lot.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through GitHub instead:
[Report a vulnerability](https://github.com/FOSSiNtellect/foresight-wing/security/advisories/new).

Include what an attacker could do, the steps to reproduce, and the version or commit you tested. You'll get a reply as
soon as possible, and credit in the fix if you want it.

## Scope

The server is meant to be reachable only from the same PC. In scope:

- anything that lets another machine, a web page or another local user read or change Foresight Wing data
- bypasses of the `X-Foresight` header check, the `Host` header check or the WebSocket origin check
- anything that sends audio, transcripts, notes or files off the machine without the user starting it

Only the latest commit on `main` is supported.
