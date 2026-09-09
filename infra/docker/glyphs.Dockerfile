# syntax=docker/dockerfile:1.7
#
# Builds the SDF glyph ranges MapLibre needs for map labels.
#
# **MapLibre does not use system fonts.** `text-font` names a *font stack*, and
# the renderer fetches signed-distance-field glyphs from the style's `glyphs`
# URL as `{fontstack}/{range}.pbf`. Without them, every label renders as
# nothing — silently, with no error in the console.
#
# Bold and italic are not properties either: each is its own stack, so
# "Oswald Regular" and "Oswald Bold" are separate glyph sets built from
# separate font files.
#
# `node:20-bookworm` rather than `-slim` on purpose. It is buildpack-deps
# based, so gcc, make and python3 are already present for fontnik's native
# build — and `apt-get` inside this network fails on a Let's Encrypt chain the
# slim image's CA bundle predates, so avoiding apt entirely is what makes this
# build work at all.
FROM node:20-bookworm

WORKDIR /build
RUN npm init -y >/dev/null && npm install fontnik@0.7.7

COPY build_glyphs.mjs /build/build_glyphs.mjs

# Fonts in, PBFs out. Both are bind-mounted by scripts/fetch_fonts.py.
ENTRYPOINT ["node", "/build/build_glyphs.mjs"]
