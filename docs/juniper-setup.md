# Preparing your Juniper switch

The integration talks to the switch with **NETCONF over SSH** (TCP port 830). NETCONF is Juniper's own
management API: it is built into every Junos release, returns structured XML (no screen scraping), and
needs no extra licence or packages. Everything the integration reads is available to the built-in
**read-only** login class, so the account you give Home Assistant cannot change the configuration.

Tested on QFX5200-32C (Junos 21.2R2). Should work on any EX, QFX, SRX, MX or ACX running Junos or
Junos OS Evolved that supports `set system services netconf ssh` (Junos 10.x and later).

## 1. Enable NETCONF and create a read-only user

Log in to the switch (console or SSH), enter `cli`, then `configure`, and paste:

```
set system services netconf ssh
set system login user homeassistant class read-only
set system login user homeassistant authentication plain-text-password
commit and-quit
```

`plain-text-password` prompts twice for the password. Junos stores it hashed.

### Prefer an SSH key? (recommended)

On the computer you'll use to set up Home Assistant:

```bash
ssh-keygen -t ed25519 -N "" -C "home-assistant" -f ha_junos_key
```

This makes `ha_junos_key` (private, goes into Home Assistant) and `ha_junos_key.pub` (public, goes on the
switch). On the switch, instead of the password line:

```
set system login user homeassistant authentication ssh-ed25519 "ssh-ed25519 AAAA...rest-of-ha_junos_key.pub... home-assistant"
```

Junos before 15.1 doesn't know ed25519. Use `ssh-keygen -t rsa -b 4096` and
`authentication ssh-rsa "..."` instead.

## 2. (Optional) Lock it down

Restrict NETCONF to your Home Assistant host. Replace `192.0.2.10` with its address:

```
set system services netconf ssh connection-limit 5
set firewall family inet filter PROTECT-RE term netconf from source-address 192.0.2.10/32
set firewall family inet filter PROTECT-RE term netconf from protocol tcp
set firewall family inet filter PROTECT-RE term netconf from destination-port 830
set firewall family inet filter PROTECT-RE term netconf then accept
```

Only do this if you already run a loopback (RE protection) filter, here called `PROTECT-RE`. New terms
go to the end, so move this one above the filter's final discard term:
`insert firewall family inet filter PROTECT-RE term netconf before term <your-discard-term>`.
A wrong `lo0` filter can lock you out, so commit with `commit confirmed 5` and then `commit` again once
you've checked you can still log in.

To use a different port: `set system services netconf ssh port 8830`, and put the same port in the
integration.

## 3. Check it from a PC (optional)

```bash
ssh -i ha_junos_key -p 830 homeassistant@SWITCH -s netconf
```

You should see a `<hello>` message full of `<capability>` lines. Press Ctrl+C to leave. If you get
`subsystem request failed`, NETCONF isn't enabled (step 1).

For a full check, the repository includes a probe script that runs every RPC the integration uses and
prints what it parsed:

```bash
pip install asyncssh
python scripts/probe.py SWITCH --user homeassistant --key ha_junos_key
```

## 4. Nice-to-haves on the switch

| What | Why | Command |
| --- | --- | --- |
| Hostname | Becomes the device name in HA | `set system host-name core-switch` |
| Port descriptions | Shown as an attribute on every port entity and in the options list | `set interfaces xe-0/0/1 description nas` |
| LLDP | Fills the "LLDP neighbors" sensor | `set protocols lldp interface all` |
| Rescue config | Clears the common "Rescue configuration is not set" minor alarm | `request system configuration rescue save` (operational mode) |

## Undo

```
delete system login user homeassistant
delete system services netconf
commit
```
