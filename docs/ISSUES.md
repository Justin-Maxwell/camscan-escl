# Open issues

No git remote on this repo, so this file is the issue tracker. Newest first.
Close an item by deleting it and saying so in the commit message.

---

## 1. NAPS2's device search ignores a valid eSCL advert — report upstream

**Status:** open, blocked on someone writing it up
**Affects:** NAPS2 8.3.2 (`naps2-8.3.2-1.x86_64`), which bundles `NAPS2.Mdns 1.0.1`
**Workaround:** Manual IP, which works

NAPS2's ESCL device search never contacts this daemon, though everything its
own code requires is present. Evidence, from `PacketTrace/`:

- `three.pcapng` — NAPS2 queries `_uscan._tcp` every 2 s over IPv4 and IPv6;
  we answer in ~30 ms with PTR + TXT + SRV + A. No connection to 8090 in 274
  frames.
- `four.pcapng` — after we began answering on IPv6 too, still nothing, in 371
  frames.
- `five.pcapng` — **the decisive one.** Records republished through Avahi's
  own responder, cache freshly restarted, no ghost instances. Avahi answers
  48 ms after NAPS2's query with six records in the *answers* section: PTR,
  TXT, SRV, two AAAA and an A. No connection to 8090 in 2572 frames.

Every condition in `NAPS2.Escl/Client/EsclServiceLocator.cs` is met:
`ServiceInstanceName.Labels[1] == "_uscan"`, a lowercase `uuid` TXT key, an
address record, and an SRV target and port that resolve. Our responses carry
the IP TTL of 255 that RFC 6762 §11 mandates; NAPS2's own queries carry
TTL 1, which suggests a hand-rolled stack.

Since a battle-tested responder publishing textbook records fails identically,
the fault is on the client side and nothing we publish can fix it.

**Before filing:** check whether a NAPS2 newer than 8.3.2 behaves differently,
and whether `NAPS2.Mdns` has moved past 1.0.1. This may already be fixed.

---

## 2. `rig.coverage_mm` is still a placeholder — scans are the wrong scale

**Status:** open, needs a ruler and five minutes
**Blocks:** every claim that output is correct

**Now visible**: open `/preview` and the crop marks show exactly what this
setting claims. With the placeholder the A4 mark fills the frame precisely
and Letter comes out 1316 px wide in a 1280 px frame, which is the placeholder
announcing itself.

`coverage_mm = [210.0, 297.0]` is a guess, not a measurement. Scans come out
at the right *pixel dimensions* because the units contract is enforced, but
the mapping from eSCL region to sensor area is wrong until the real area the
frame covers at rig height is measured. This is the last thing between the
daemon and trustworthy output — and it is silent, which is what makes it
dangerous.

---

## 3. Exposure lock is off, and needs a sweep at the final camera position

**Status:** open, waiting on the camera being mounted permanently
**See:** README "Calibrate", `[capture.exposure]`

The mechanism is built and verified against the hardware, but ships off
because the right values are rig-specific. Until it is on, every scan is at
the mercy of ambient light: a scan during this work came back with 89% of its
pixels at 250+ luma, from sun hitting the desk at the camera's angle.

---

## 4. The capabilities XML has never been validated against the Mopria spec

**Status:** open since the beginning; spec §13 calls it the first task

`escl.py`'s document reproduces the skeleton in spec §6, which that document
itself flags as written from recollection. It demonstrably satisfies
`sane-airscan` and NAPS2's Manual IP path, which is weaker evidence than it
sounds — two clients agreeing does not make a document conformant, and the
missing `scan:UUID` proved how a plausible-looking document can fail silently.
Validate against the Mopria eSCL Scan Technical Specification and against
AirSane's generated output.
