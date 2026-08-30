---
name: New reseller
about: Add a new available reseller.
title: "[RESELLER] New available reseller"
labels: reseller
assignees: Delmael

---

> [!NOTE]
> If you do not see your current reseller in the available list, you can ask to add it. We cannot ensure your device go through this platform.
> See [How to capture the values](https://github.com/ajtudela/smartbox/blob/main/api-notes.md#basic-auth-credential) — open your reseller's web app, watch the network tab, log in and read the `POST .../api/v2/client/token` request headers.

* [ ] I have checked with the `smartbox resellers` command that my reseller is not already present
* **Website url (mandatory)** (the `x-referer` header):
* Name of the reseller:
* API name (the `api-<...>` part of the request host, e.g. `api-foo`):
* Basic auth credential (the base64 string after `Basic ` in the `authorization` header):
* Serial id (the `x-serialid` header):
