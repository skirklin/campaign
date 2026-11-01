# Working on this project

## Build the best version

This is a prototype we're still playing with. Always aim for the best
possible version of the product, not the cheapest change that works. How
much work something is doesn't decide the design; what makes the product
best does. When there's a choice between a quick patch and the right
model, build the right model, and redo what it touches.

There's no legacy to protect: no outside users, nothing to stay compatible
with. Don't keep old fields, compatibility shims, deprecated paths or
"expand, then contract" steps around for their own sake; change the schema
and the code to what they should be. The only data that matters is the
owner's own campaigns, so when it's cheap, a migration that carries that
data over is worth writing, but it never shapes the design.

## Checks

```bash
uv run pytest -q
```

Run the app and look at a change in a browser (`uv run campaign web`) when
it touches the UI; the static JS has no build step (`node --check` catches
syntax errors).
