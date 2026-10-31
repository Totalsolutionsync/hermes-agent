"""Kynver skill sync (Hermes parity H1-H3).

Kynver holds the master copy of every skill; ``~/.hermes/skills`` is a cache
Hermes keeps reading natively. Nothing in this package runs on the read path:

* ``refresher``  (H1) background pull into the cache (ETag/304 + cursor)
* ``outbox``     (H2) durable dirty set of locally edited slugs
* ``drain``      (H2) background push of the dirty set
* ``usage``      (H3) local usage buffer flushed in batches
* ``service``    starts/stops the background worker; its plugin hooks
                 (``on_skill_lifecycle``, ``post_tool_call``) feed the outbox

Everything is off unless ``kynver.skills.*`` flags are set in config.yaml.
"""
