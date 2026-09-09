"""Server-side map rendering.

Real MapLibre GL JS in headless Chromium, screenshotting the *page* rather
than the canvas — so legends, scale bars, and title blocks are the app's own
React components, one implementation, identical in both contexts
(`06-rendering.md` §1).

Render workers run in a network segment with egress permitted only to the
tile, glyph, sprite, and object-storage services. Not the database. Not the
API. Not the internet (`03-auth-security.md` §7.3).
"""

__version__ = "0.1.0"
