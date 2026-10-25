"""The replay engine release that produced a result.

Experiments record it and include it in their identity, so a saved experiment can be
replayed again after an engine change. Experiments saved before it existed read as
``replay_v1``. Raise it when a change alters decisions or economics on purpose.
"""

REPLAY_ENGINE = "replay_v2"
ENGINE_BEFORE_VERSIONING = "replay_v1"
