"""Render techne's bundled procedures for hosts that read the open Agent Skills
standard — opencode, Charm's crush, Block's goose, and anything else that loads a
``<name>/SKILL.md`` directory.

This is the second renderer, and its existence is the point: ``techne.skills`` owns
each procedure as one portable markdown file, and a platform package projects it
into the layout that host expects. A third host gets a third renderer, never a
third copy of the procedure.
"""
