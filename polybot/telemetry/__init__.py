"""Engine telemetry: durable, per-mode persistence of everything the bot does.

Each trading engine (paper / live) owns *its own* SQLite database. The engine is
the only writer; the dashboard opens the same file read-only. WAL journalling
makes that safe and lock-free, and it means the dashboard keeps serving history
even while an engine is stopped, crashed or restarting.
"""
from polybot.telemetry.recorder import EngineRecorder, NullRecorder
from polybot.telemetry.schema import ENGINE_SCHEMA, connect, init_engine_db

__all__ = ["EngineRecorder", "NullRecorder", "ENGINE_SCHEMA", "connect", "init_engine_db"]
