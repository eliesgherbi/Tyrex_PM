"""Framework recording: decision diagnostics and explanations for live, observation, replay.

Evidence classes (integration plan section 2):

* essential  - execution journal, settlement ledger, run summary (not handled here;
  synchronous and durable, never optional);
* operational - run evidence journal (``run_events``);
* decisions  - one record per evaluation (host input timing + strategy-declared fields);
* explain    - calculated state behind a decision (indicator ``explain()``), by trigger;
* raw inputs - warehouse ``RawEnvelope`` capture (``recording.raw_capture``).

Capture never serializes or blocks: it copies decision-time primitives into a bounded
queue; one background thread serializes and writes compressed segments with manifests.
"""

from tyrex_pm.recording.config import (
    ExplainTrigger,
    RecordingConfig,
    RecordingLevel,
    recording_config_from_mapping,
)
from tyrex_pm.recording.recorder import Recorder, StreamStatus

__all__ = [
    "ExplainTrigger",
    "Recorder",
    "RecordingConfig",
    "RecordingLevel",
    "StreamStatus",
    "recording_config_from_mapping",
]
