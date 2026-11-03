# Security Policy

## Supported versions

Only the latest release receives fixes. Until 1.0.0 that is the newest `0.x` tag.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.
Use GitHub's private reporting instead:
[Report a vulnerability](https://github.com/Bahrombekk/cloud-cam-viewer/security/advisories/new).

Include what you found, how to reproduce it, and the impact you expect. You should get
a first reply within 7 days.

## What this project handles

`cloudcam` stores secrets that grant access to real cameras. Reports in these areas are
especially welcome:

- cloud session tokens (`token.json`), verification codes (`cam_keys.json`) and the
  per-install terminal id (`feature_code.txt`) — written with `0600` permissions;
- secrets leaking into logs, the process list, crash output or child-process arguments;
- the local decrypting proxy and anything it binds to on the network;
- validation of the cloud domain returned by login (a malicious domain must not
  receive credentials).

## When filing any issue

Never paste real verification codes, tokens, passwords or full device serials into a
public issue. Replace them with placeholders such as `ABCDEF` and `BD1234567`.
