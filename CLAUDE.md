# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## git workflow
do use git to manage changes, and commit often with clear messages. The main branch should always be deployable.
if several features are being developed in parallel, create separate branches and merge back to main when each is complete and tested.
rule of git commit messages: use prefix `feat:`, `fix:`, `refactor:`, `docs:`, `style:`, `test:`, or `chore:` to indicate the type of change and (optionally) the affected module like `feat(bot): add new AI engine`. This helps maintain a clear history.

## ui style
use constant colors and fonts defined in `frontend/src/styles/theme.css` for a cohesive look. 
