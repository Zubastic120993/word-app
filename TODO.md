# TODO — iPad Deploy & Sync (`feature/ipad-deploy-sync`)

> **Goal**: Allow an iPad (iPadOS 15.8, Safari over LAN) to function as a
> learning-only client against the Mac-hosted Word App.  No native app, no
> database sync, no admin features on iPad — only study sessions and learning
> events that write back to the single Mac SQLite database in real time.

---

## 1. Networking & Access

- [ ] Verify the Mac firewall allows inbound TCP 8000 (System Settings →
      Network → Firewall).

---

## 2. iPad Client Identification & Permissions

- [ ] In non-debug (production) mode, resolve iPad identity from a cookie or
      HTTP header — never from a query parameter.

---

## 5. CSS / iPad UX Adjustments

- [ ] Add a `<meta name="viewport">` tag to `base.html` (if not already
      present) for proper scaling on iPadOS Safari.
- [ ] Add CSS media queries or touch-target adjustments in `style.css` for
      iPad-sized viewports (768 px–1024 px landscape / portrait).
- [ ] Increase tap-target sizes for answer buttons (Correct / Almost / Wrong)
      to at least 44×44 pt per Apple HIG.
- [ ] Ensure the recall text input is large enough and does not trigger
      unwanted Safari auto-zoom (font-size ≥ 16 px).
- [ ] Test virtual-keyboard behaviour: the study card area should remain visible
      when the on-screen keyboard is open (scroll into view / layout shift).
- [ ] Verify audio playback (`<audio>` / ElevenLabs) works on iPadOS Safari
      (autoplay restrictions, user-gesture requirement).
- [ ] Disable hover-dependent interactions (tooltips, hover menus) that do not
      translate to touch.

---

## 6. Configuration & Environment

- [ ] Ensure `WORD_APP_HOST` (or equivalent) can override the bind address so
      the user can switch between local-only and LAN-accessible modes.
