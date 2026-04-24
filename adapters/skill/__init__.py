"""Claude Code Skill adapter.

Runs ResearchOS inside a Claude Code Skill sandbox. Differs from SDK
runtime in that the host Claude Code itself IS the LLM — this adapter's
``run_agent`` inlines the agent definition into the host's conversation
rather than spawning a fresh LLM call.

Tool set is borrowed from the host (file_read, Bash, Grep, etc.); no
tool implementations ship here.
"""
