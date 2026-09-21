"""Provider drivers: StayOnDuty supervises third-party AI assistants.

StayOnDuty is a supervision/orchestration layer, not a model host. A
"provider" drives an existing assistant's API (Grok via xAI, Claude,
GPT, ...) to do long automatic tasks. BYOK: the user supplies the API
key; StayOnDuty never pays for inference.

All drivers are stdlib-only (urllib).
"""
